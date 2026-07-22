import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import getseries
from frappe.utils import parse_addr

from inventive_helpdesk_backend.permissions import TEAM_ROLES


def _slug(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())[:3] or "XXX"


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
            poc = frappe.db.get_value("POC", {"email": addr}, ["poc_name", "client", "division"], as_dict=True)
            if poc:
                self.client = poc.client
                self.division = poc.division
                if not self.raised_by:
                    self.raised_by = poc.poc_name
            elif not self.raised_by:
                self.raised_by = addr

        # Portal client-authored tickets: a POC can POST to the REST resource API, so a
        # crafted payload could otherwise set privileged fields (status / assignee /
        # assignment_group) or inject staff-labelled conversation rows. Staff creations are
        # trusted and email intake owns its own fields above — clamp only client authorship.
        self._clamp_client_authored_fields()

    def _clamp_client_authored_fields(self):
        """Force safe values on a ticket a client POC creates directly, so a hand-crafted
        REST payload can't open it pre-Resolved, self-assign it, or forge a staff reply.
        No-op for staff, the Administrator/Guest system paths, and email intake.

        Email intake is recognised by WHO is inserting, never by what the payload says.
        This used to return early on any truthy `from_email` — but `from_email` is a
        permlevel-0 field and Support Client holds `create`, so a POC could set it in the
        REST payload and skip the whole clamp: self-assign into a real team's queue, open
        the ticket pre-Resolved, and append conversation rows as `kind="team"` to forge a
        staff reply in their own thread. Both real intake paths are covered by the user
        check below instead — production `on_communication` runs as Administrator, dev
        `receive_webhook` as Guest."""
        user = frappe.session.user
        if user in ("Administrator", "Guest") or set(frappe.get_roles(user)) & TEAM_ROLES:
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
        if self.client and self.division:
            cc = frappe.db.get_value("Client", self.client, "client_code") or _slug(self.client)
            dc = frappe.db.get_value("Division", self.division, "division_code") or _slug(self.division)
            prefix = f"{cc}-{dc}-"
        else:
            prefix = "INB-"
        self._ensure_series_floor(prefix)
        # _ensure_series_floor only sees pre-existing tickets on a prefix's FIRST
        # use. A ticket inserted later with an explicit name (Data Import, a manual
        # backfill) can still land above the counter, so the series can still catch
        # up to an already-taken number. Skip forward past any collision instead of
        # trusting the counter blindly — getseries' FOR UPDATE keeps each pull
        # atomic, so this stays race-safe under concurrent inserts.
        for _ in range(self._MAX_NAME_ATTEMPTS):
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
            try:
                floor = max(floor, int(nm.rsplit("-", 1)[1]))
            except (IndexError, ValueError):
                continue
        frappe.db.sql("insert ignore into `tabSeries` (name, current) values (%s, %s)", (prefix, floor))
