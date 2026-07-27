import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import getseries
from frappe.utils import now_datetime, parse_addr

from inventive_helpdesk_backend.permissions import TEAM_ROLES


def _slug(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())[:3] or "XXX"


# ---- activity log ---------------------------------------------------------
# Ticket fields whose changes are recorded, mapped to the label shown in the UI.
# Deliberately just the four that change hands — description/title edits would
# bury the handovers this log exists to surface.
_TRACKED_FIELDS = {
    "status": "Status",
    "priority": "Priority",
    "assignee": "Assignee",
    "assignment_group": "Team",
}
# Link fields read better as "Unassigned" than as a blank cell.
_EMPTY_LABEL = {"assignee": "Unassigned", "assignment_group": "Unassigned"}


def _actor() -> str:
    """Display name of whoever is making the change. Mirrors api._author(); kept
    local so the doctype doesn't depend on the API layer."""
    return frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user


def _display(fieldname: str, value) -> str:
    if isinstance(value, str):
        value = value.strip()
    return str(value) if value else _EMPTY_LABEL.get(fieldname, "—")


class SupportTicket(Document):
    def validate(self):
        # Cross-field integrity the frontend can't be trusted to enforce: a ticket's
        # division must belong to its client (REST/API callers could send any pair).
        if self.division:
            if not self.client:
                frappe.throw(_("A ticket with a division must also have its client set"))
            div_client = frappe.db.get_value("Division", self.division, "client")
            if div_client != self.client:
                frappe.throw(
                    _("Division {0} belongs to {1}, not {2}").format(self.division, div_client, self.client)
                )

        self._validate_product()

        # Assignment integrity: a member can only be assigned within a team (assign
        # to a team first, then to a member of it). Enforce only when the assignment
        # actually changes — or on insert — so tickets assigned before this rule stay
        # editable on their other fields. The UI mirrors this (member picker is locked
        # until a team is chosen); this is the server-side backstop for REST callers.
        if self.assignee and not self.assignment_group:
            before = self.get_doc_before_save()
            if before is None or before.assignee != self.assignee or before.assignment_group != self.assignment_group:
                frappe.throw(_("Assign the ticket to a team before assigning it to a member."))

        self._validate_collaborators()

    def _validate_product(self):
        """A ticket's product must be one the client actually runs, at this division.

        `product` is a single Link, so "one product per ticket" is a property of the shape,
        not a rule enforced here — there is no payload that can carry two. What this checks
        is that the one chosen is *legitimate*: a Client Product engagement covering this
        division, or one attached to the client as a whole (empty division table).

        Without it a REST caller — including a portal client, who may legitimately set this
        field and so is not covered by _clamp_client_authored_fields — could tag a ticket
        with any product in the catalogue, including one belonging to another client.

        Deliberately NOT reqd on the doctype: inbound email cannot know the product, so
        every intake insert would throw. Requiring it is the UI's job, where there is a
        person to ask."""
        if not self.product:
            return
        if not self.client:
            frappe.throw(_("A ticket with a product must also have its client set"))

        engagements = frappe.get_all(
            "Client Product",
            filters={"client": self.client, "product": self.product},
            pluck="name",
        )
        if not engagements:
            frappe.throw(
                _("{0} does not run {1}").format(self.client, self.product),
                title=_("Product not in use here"),
            )
        if not self.division:
            # No division to narrow by — any engagement of this product will do.
            return

        for name in engagements:
            divisions = frappe.get_all(
                "Client Product Division",
                filters={"parent": name, "parenttype": "Client Product"},
                pluck="division",
            )
            # An engagement with no divisions covers the whole client, so it satisfies any
            # division of it. Otherwise this division must be named explicitly.
            if not divisions or self.division in divisions:
                return

        frappe.throw(
            _("{0} does not run {1} at {2}").format(self.client, self.product, self.division),
            title=_("Product not in use here"),
        )

    def _validate_collaborators(self):
        """Each collaborator row names exactly one party matching its type, doesn't
        duplicate another row, and doesn't re-add the owner (assignee/team) who
        already has access — so the list stays meaningful. The api.add_collaborator
        method enforces the same up front; this is the REST-caller backstop."""
        seen = set()
        for row in (self.collaborators or []):
            if row.party_type == "Team":
                party = row.team
                if not party or row.member:
                    frappe.throw(_("A Team collaborator must name a team (and no member)."))
                if party == self.assignment_group:
                    frappe.throw(_("{0} already owns this ticket.").format(party))
            elif row.party_type == "Member":
                party = row.member
                if not party or row.team:
                    frappe.throw(_("A Member collaborator must name a member (and no team)."))
                if party == self.assignee:
                    frappe.throw(_("{0} is already assigned to this ticket.").format(party))
            else:
                frappe.throw(_("A collaborator must be a Team or a Member."))
            key = (row.party_type, party)
            if key in seen:
                frappe.throw(_("{0} is listed as a collaborator more than once.").format(party))
            seen.add(key)

    def before_insert(self):
        # Inbound-email intake: when a ticket arrives from email (from_email set,
        # no client yet), match the sender to a POC to auto-scope it. Inbound mail
        # is unclassified, so it lands as an unassigned Query.
        if self.from_email and not self.client:
            addr = (parse_addr(self.from_email)[1] or self.from_email).strip().lower()
            self.source = "Email"
            self.ticket_type = "Query"
            if not self.priority:
                self.priority = "Medium"
            poc = frappe.db.get_value("POC", {"email": addr}, ["name", "poc_name", "client"], as_dict=True)
            if poc:
                self.client = poc.client
                # A contact may hold several divisions (a Lead overseeing a few), and an
                # inbound email doesn't say which one it's about. Take the first only when
                # it's unambiguous; otherwise leave `division` blank so the ticket lands in
                # the shared triage inbox for an agent to route, rather than guessing wrong
                # and hiding it from the division that should see it.
                divisions = frappe.get_all(
                    "POC Division",
                    filters={"parent": poc.name, "parenttype": "POC"},
                    pluck="division",
                    order_by="idx",
                )
                self.division = divisions[0] if len(divisions) == 1 else None
                if not self.raised_by:
                    self.raised_by = poc.poc_name
            elif not self.raised_by:
                self.raised_by = addr

        # Portal client-authored tickets: a POC can POST to the REST resource API, so a
        # crafted payload could otherwise set privileged fields (status / assignee /
        # assignment_group) or inject staff-labelled conversation rows. Staff creations are
        # trusted and email intake owns its own fields above — clamp only client authorship.
        self._clamp_client_authored_fields()

    def before_save(self):
        """Record who changed what, in the same write as the change itself.

        This lives on the document rather than in the API layer because the frontend
        edits status/priority/assignment straight over the REST resource endpoint
        (store.setStatus and friends call updateDoc → PUT). There is no whitelisted
        method to instrument, and a desk edit, a script or a hand-rolled REST call
        would bypass one anyway — so the only place the log cannot be dodged is here.

        Appending in before_save (not on_update) matters twice over: get_doc_before_save
        is already loaded by check_if_latest, and the rows go down with the parent, so
        there is no second save() — no recursion, and no ticket can ever be saved with
        its change missing from the log.

        Ordering note: validate_higher_perm_levels() runs before this (document.py:476),
        which resets the permlevel-1 `activity` field to its stored value for anyone
        without permlevel-1 write. A client POC therefore cannot inject rows through a
        crafted payload, while rows appended here still persist.
        """
        # Who we are talking to, cached for list views. Derived, so it is recomputed on
        # every save rather than trusted from the payload — and `sender.classify` stays
        # authoritative for anything that must be right (see sender.py).
        self._refresh_sender_kind()

        # Bulk/system runs must not manufacture history. in_test is deliberately NOT
        # in this set — the tests assert on exactly this behaviour.
        if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_import:
            return

        author, when = _actor(), now_datetime()
        before = self.get_doc_before_save()
        if before is None:
            # New ticket. One row, so the log opens with an origin rather than a
            # burst of "changed from nothing" lines for every tracked field.
            self.append("activity", {
                "action": "Created",
                "new_value": _display("status", self.status),
                "author": author,
                "acted_on": when,
            })
            return

        for fieldname, label in _TRACKED_FIELDS.items():
            old, new = before.get(fieldname), self.get(fieldname)
            # Normalise "" / None so clearing an empty link isn't logged as a change.
            if (old or None) == (new or None):
                continue
            self.append("activity", {
                "action": label,
                "old_value": _display(fieldname, old),
                "new_value": _display(fieldname, new),
                "author": author,
                "acted_on": when,
            })

    def _refresh_sender_kind(self):
        """Recompute the cached sender classification.

        Import is function-local: sender.py is policy and support_ticket.py is the model,
        so the dependency only exists at call time and cannot become a cycle if sender.py
        ever needs to read a ticket.
        """
        from inventive_helpdesk_backend import sender

        kind, _email, reason = sender.classify(self)
        self.sender_kind = kind
        self.no_reply_reason = reason

    def _clamp_client_authored_fields(self):
        """Force safe values on a ticket a client POC creates directly, so a hand-crafted
        REST payload can't open it pre-Resolved, self-assign it, or forge a staff reply.
        No-op for staff, the Administrator system path, and email intake.

        Email intake is recognised by WHO is inserting, never by what the payload says.
        This used to return early on any truthy `from_email` — but `from_email` is a
        permlevel-0 field and Support Client holds `create`, so a POC could set it in the
        REST payload and skip the whole clamp: self-assign into a real team's queue, open
        the ticket pre-Resolved, and append conversation rows as `kind="team"` to forge a
        staff reply in their own thread. Email intake is covered by the user check below
        instead — `on_communication` runs as Administrator.

        Guest is deliberately NOT exempt. It used to be, for a dev-only inbound webhook that
        ran unauthenticated; that has been removed, so no legitimate path inserts a ticket
        as Guest and an exemption would only leave an unauthenticated insert unclamped."""
        user = frappe.session.user
        if user == "Administrator" or set(frappe.get_roles(user)) & TEAM_ROLES:
            return
        # The author is a portal client — pin everything they must not control.
        self.from_email = None
        self.source = "Portal"
        self.status = "New"
        self.assignee = None
        self.assignment_group = None
        poc_name = frappe.db.get_value("POC", {"user": user}, "poc_name")
        if poc_name:
            self.raised_by = poc_name
        for row in (self.conversation or []):
            row.kind = "client"
            row.role = "Client"
            if poc_name:
                row.author = poc_name

    # Bound on the retry loop below — real collision runs are 1-2 iterations;
    # this is only a backstop against a pathological/corrupt series.
    _MAX_NAME_ATTEMPTS = 1000

    def autoname(self):
        # CLIENT-DIV-#### per-division sequence, or INB-#### for un-scoped inbound.
        # The number comes from Frappe's tabSeries counter (getseries), which uses
        # SELECT ... FOR UPDATE — atomic under concurrent inserts, unlike a
        # max(existing)+1 scan. The counter increment shares the insert's
        # transaction, so a failed insert rolls the number back too.
        if self.name:
            return
        # A client with no divisions is a legitimate shape, and its tickets used to fall
        # through to INB- — the pool for mail we could not attribute to anyone. That made a
        # known client's ticket indistinguishable from an unidentified one, and made every
        # such client share one global counter. Fall back to the client on its own instead;
        # INB- now means only what its name says.
        if self.client:
            cc = frappe.db.get_value("Client", self.client, "client_code") or _slug(self.client)
            if self.division:
                dc = frappe.db.get_value("Division", self.division, "division_code") or _slug(self.division)
                prefix = f"{cc}-{dc}-"
            else:
                prefix = f"{cc}-"
        else:
            prefix = "INB-"
        self._ensure_series_floor(prefix)
        # _ensure_series_floor only sees pre-existing tickets on a prefix's FIRST
        # use. A ticket inserted later with an explicit name (Data Import, a manual
        # backfill) can still land above the counter, so the series can still catch
        # up to an already-taken number. Skip forward past any collision instead of
        # trusting the counter blindly — getseries' FOR UPDATE keeps each pull
        # atomic, so this stays race-safe under concurrent inserts.
        # Not `_`: that is frappe's translation function, imported at the top. Binding it
        # as the loop variable leaves it holding an int once the loop is exhausted, so the
        # throw below would die with "'int' object is not callable" instead of reporting
        # the exhausted series — an unreadable failure on the one path that has to explain
        # itself.
        for _attempt in range(self._MAX_NAME_ATTEMPTS):
            name = f"{prefix}{getseries(prefix, 4)}"
            if not frappe.db.exists("Support Ticket", name):
                self.name = name
                return
        frappe.throw(_("Could not allocate a unique ticket number for {0} — series may need attention").format(prefix))

    @staticmethod
    def _ensure_series_floor(prefix: str):
        """First use of a prefix only: seed its tabSeries counter at the highest
        existing suffix, so series-issued names never collide with tickets that
        predate the counter (seeded/backfilled IDs). INSERT IGNORE keeps a
        concurrent first-use race harmless — one insert wins, both then increment
        the same row atomically via getseries."""
        if frappe.db.sql("select name from `tabSeries` where name = %s", prefix):
            return
        rows = frappe.db.sql("select name from `tabSupport Ticket` where name like %s", prefix + "%")
        floor = 0
        for (nm,) in rows:
            # Only names that are this prefix followed by nothing but digits. LIKE alone is
            # too loose now that a client-only prefix exists: "MSFT-%" also matches
            # "MSFT-AZU-0001", and seeding the client's counter from its divisions' tickets
            # would make the numbering jump for no visible reason. Slicing by prefix length
            # (rather than rsplit) is also what keeps a 5-digit suffix countable past 9999.
            suffix = nm[len(prefix):]
            if suffix.isdigit():
                floor = max(floor, int(suffix))
        frappe.db.sql("insert ignore into `tabSeries` (name, current) values (%s, %s)", (prefix, floor))
