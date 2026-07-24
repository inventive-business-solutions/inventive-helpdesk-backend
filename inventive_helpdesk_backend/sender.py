"""Who is on the other end of a ticket, and whether we can reach them.

Policy only — this module decides WHO a ticket belongs to and whether email can get to
them. It never sends anything. `email.py` owns transport and depends on this; the
dependency must not run the other way.

Four kinds, because the data model supports four and the obvious two-way split gets one of
them wrong:

  Registered     POC with an enabled portal login -> portal + email
  Known Contact  POC on file, never invited       -> email only, but attributable
  Unregistered   no POC matches the address       -> email only
  No Reply       an unmonitored mailbox           -> nothing we send will be read

"Known Contact" is the one that matters. A customer contact we know — right client, right
division — who was simply never invited has no login, so telling them to "sign in to track
your ticket" sends them to a door with no key.
"""
import re

import frappe

REGISTERED = "Registered"
KNOWN_CONTACT = "Known Contact"
UNREGISTERED = "Unregistered"
NO_REPLY = "No Reply"

# Layer 2 of no-reply detection: local-parts that are unmonitored BY CONSTRUCTION — the
# address itself announces that replies go nowhere. Matched on the WHOLE local part, so a
# real person at `noreply.patel@` is untouched.
#
# Deliberately narrow. `alerts@`, `notifications@`, `system@` and `mailer@` were here and
# have been removed: at a customer's own domain those are plausibly monitored shared
# mailboxes, and the cost of guessing wrong is not cosmetic. A No Reply classification also
# withholds the acknowledgement, so a false positive means the customer emails in and hears
# nothing at all — no ticket ID, no confirmation. Anything less than certain belongs in a
# No Reply Rule, where an operator who knows their customers decides.
#
# `mailer-daemon` and `postmaster` are absent for a different reason: they are bounce
# infrastructure, and email._is_bounce files their mail onto the originating ticket rather
# than letting it become one.
_NO_REPLY_LOCALPART = re.compile(
    r"^(?:no[-_]?reply|do[-_]?not[-_]?reply|bounces?|auto[-_]?mailer)$",
    re.I,
)


_CACHE_ATTR = "_helpdesk_no_reply_rules"


def _rules():
    """Operator-managed overrides (layer 1), cached for the current request.

    Hand-rolled rather than `@request_cache` on purpose: that decorator keys its cache on
    the *undecorated* function, so an invalidation helper holding the wrapper silently
    clears nothing — which is exactly the bug this replaced. `frappe.local` is per-request,
    and owning the key means clear_rule_cache() demonstrably works.
    """
    cached = getattr(frappe.local, _CACHE_ATTR, None)
    if cached is not None:
        return cached
    if not frappe.db.table_exists("No Reply Rule"):
        # Not migrated yet (a site mid-upgrade). Fall back to the built-in patterns rather
        # than failing a ticket read — and do NOT cache, so it self-heals after migrate.
        return []
    rules = frappe.get_all(
        "No Reply Rule", filters={"enabled": 1}, fields=["pattern", "match_type"], limit=500
    )
    setattr(frappe.local, _CACHE_ATTR, rules)
    return rules


def clear_rule_cache() -> None:
    """Drop the cached rule list. Called from No Reply Rule's on_update/on_trash so a rule
    edited and applied within one request does not read the pre-edit list."""
    setattr(frappe.local, _CACHE_ATTR, None)


def no_reply_reason(email: str) -> str | None:
    """Why this address should not be replied to, or None if it looks reachable.

    Returns a human sentence, not a boolean, so the UI badge can explain itself and an
    operator can tell a configured rule from a built-in guess.

    A match never stops a ticket being created or read. It does two things: it badges the
    ticket, and it withholds the automatic acknowledgement — so a false positive DOES cost
    the sender their ticket ID. That asymmetry is why the built-in patterns below are
    narrow and the operator rules exist.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")

    # Layer 1: an operator rule always wins, in either direction.
    for rule in _rules():
        pattern = (rule.get("pattern") or "").strip().lower()
        if not pattern:
            continue
        kind = rule.get("match_type") or "Exact"
        hit = (
            (kind == "Exact" and email == pattern)
            or (kind == "Prefix" and local.startswith(pattern))
            or (kind == "Domain" and domain == pattern.lstrip("@"))
        )
        if kind == "Regex":
            try:
                hit = bool(re.search(pattern, email, re.I))
            except re.error:
                hit = False  # a bad pattern must not break intake
        if hit:
            return f"matches the configured no-reply rule “{rule.get('pattern')}”"

    # Layer 2: convention.
    if _NO_REPLY_LOCALPART.match(local):
        return f"“{local}@” is an unmonitored mailbox by convention"
    return None


def _division_poc_email(division, poc_name=None, leads=None) -> str | None:
    """Email of a contact who holds `division`, oldest first so the pick is deterministic.

    Contacts hold divisions through the POC Division child table, so this joins rather than
    matching a column. `poc_name` narrows to a named person; `leads` selects only Leads
    (True) or only division POCs (False), or either when None.
    """
    if not division:
        return None
    filters = {"parenttype": "POC", "division": division}
    rows = frappe.get_all("POC Division", filters=filters, pluck="parent")
    if not rows:
        return None
    poc_filters = {"name": ["in", rows]}
    if poc_name:
        poc_filters["poc_name"] = poc_name
    if leads is not None:
        poc_filters["is_lead"] = 1 if leads else 0
    email = frappe.get_all(
        "POC", filters=poc_filters, pluck="email", order_by="creation asc", limit=1
    )
    return email[0].strip().lower() if email and email[0] else None


def reply_address(ticket) -> str | None:
    """The address a reply to this ticket would go to.

    Email tickets carry `from_email`; portal tickets are owned by the raising POC (their
    login IS their email); agent-raised tickets fall back to the POC named on the ticket,
    then the division's primary POC.
    """
    if getattr(ticket, "from_email", None):
        return (ticket.from_email or "").strip().lower() or None
    owner = getattr(ticket, "owner", None)
    if owner and owner not in ("Administrator", "Guest") and frappe.db.exists("POC", {"user": owner}):
        return owner.strip().lower()
    div, raised_by = getattr(ticket, "division", None), getattr(ticket, "raised_by", None)
    if div and raised_by:
        email = _division_poc_email(div, poc_name=raised_by)
        if email:
            return email
    if div:
        # `is_primary` used to pick the addressee here. It was retired when Leads arrived,
        # so fall back by role instead: a division's own POC first, then a Lead who oversees
        # that division. Ordered by creation so the answer is stable rather than whichever
        # row the DB happens to return.
        email = _division_poc_email(div, leads=False) or _division_poc_email(div, leads=True)
        if email:
            return email
    return None


def classify(ticket):
    """Return (kind, reply_address, no_reply_reason) for a ticket.

    DERIVED, never authoritative. `Support Ticket.sender_kind` caches this for list views,
    but a POC being invited changes the answer without touching the ticket — so anything
    that must be correct reads this, not the cached column.
    """
    email = reply_address(ticket)
    if not email:
        # An agent-logged ticket with no contact on file. Not unreachable in principle,
        # just not addressed to anyone yet.
        return UNREGISTERED, None, None

    reason = no_reply_reason(email)
    if reason:
        return NO_REPLY, email, reason

    poc = frappe.db.get_value("POC", {"email": email}, ["name", "user"], as_dict=True)
    if not poc:
        return UNREGISTERED, email, None
    if poc.user and frappe.db.get_value("User", poc.user, "enabled"):
        return REGISTERED, email, None
    # On file, but with no usable login — a disabled account counts as no login, because
    # the portal is equally unreachable either way.
    return KNOWN_CONTACT, email, None


def can_receive_email(ticket) -> bool:
    """False when replying by email is pointless: no address, or an unmonitored one."""
    kind, email, _ = classify(ticket)
    return bool(email) and kind != NO_REPLY


# ---- reply policy ---------------------------------------------------------
# What happens to a staff reply, decided in one place. Each value is also the `kind`
# recorded in Ticket Email Log, so the audit trail says WHY a mail went out.
FORCED = "Forced Reply"  # no portal exists for them — email is the only channel
REQUESTED = "Reply"  # the agent asked for it
FIRST_RESPONSE = "First Response"  # one-time, sent even though the toggle was off
INTERNAL = None  # saved to the thread, not emailed
UNREACHABLE = "unreachable"  # nothing we send would be read


def reply_plan(ticket, *, requested_email: bool | None):
    """Decide whether a staff reply is emailed. Returns (send, kind, explanation).

    Enforced server-side rather than by the frontend hiding a toggle: the rule that an
    unregistered sender's ticket cannot be answered internal-only has to hold against a
    REST caller too, the same way _clamp_client_authored_fields does.

    `requested_email` is the toggle, and it is only consulted for a registered user. For
    everyone else there is no portal to read the reply in, so honouring an "off" toggle
    would mean the agent replies into a void — which is the actual defect this phase fixes.
    """
    kind, address, no_reply = classify(ticket)

    if not address:
        return False, UNREACHABLE, "no contact address on this ticket"
    if kind == NO_REPLY:
        return False, UNREACHABLE, no_reply or "an unmonitored mailbox"

    if kind in (UNREGISTERED, KNOWN_CONTACT):
        return True, FORCED, f"{address} has no portal access — email is the only channel"

    # Registered: the toggle applies.
    if requested_email:
        return True, REQUESTED, f"emailed to {address} as requested"
    if not getattr(ticket, "first_response_notified_on", None):
        # The exception in the spec. Their first reply would otherwise sit unread in a
        # portal they may never have opened, so it goes out once — with a pointer telling
        # them where the rest of the conversation lives. Only once.
        return True, FIRST_RESPONSE, f"first reply on this ticket — emailed to {address} once"
    return False, INTERNAL, "saved to the thread; the client has already been pointed at the portal"


def refresh(ticket_name: str) -> None:
    """Recompute the cached classification for one ticket.

    Called when a POC is invited, which upgrades Known Contact -> Registered without the
    ticket itself changing. Written with db_set/update_modified=False so re-classifying
    does not look like someone edited the ticket.
    """
    doc = frappe.get_doc("Support Ticket", ticket_name)
    kind, _email, reason = classify(doc)
    if doc.sender_kind != kind or (doc.no_reply_reason or None) != reason:
        frappe.db.set_value(
            "Support Ticket",
            ticket_name,
            {"sender_kind": kind, "no_reply_reason": reason},
            update_modified=False,
        )


def refresh_for_poc(poc_name: str) -> int:
    """Re-classify every ticket whose reply address resolves to this POC. Returns the count.

    Two sets, because `reply_address` has a fallback chain and only the first leaves a
    trace on the ticket:

    1. Tickets that name the address directly in `from_email` — email intake.
    2. Tickets in the POC's division with NO `from_email` — agent-logged ones, whose reply
       address is resolved through `raised_by` or the division's primary POC. Filtering on
       `from_email` alone missed every one of these, so inviting a division's primary
       contact left their agent-logged tickets showing a stale "Known Contact" badge.

    Both sets are bounded (one contact, one division), so this is safe inline on invite.
    """
    poc = frappe.db.get_value("POC", poc_name, ["name", "email"], as_dict=True)
    if not poc or not poc.email:
        return 0
    email = poc.email.strip().lower()

    names = set(frappe.get_all("Support Ticket", filters={"from_email": email}, pluck="name", limit=2000))
    # A contact can hold several divisions now, so sweep all of them — bounded by one
    # contact's divisions rather than one division, but still bounded.
    divisions = frappe.get_all(
        "POC Division", filters={"parent": poc.name, "parenttype": "POC"}, pluck="division"
    )
    if divisions:
        names.update(
            frappe.get_all(
                "Support Ticket",
                filters={"division": ["in", divisions], "from_email": ["in", ["", None]]},
                pluck="name",
                limit=2000,
            )
        )
    for name in names:
        refresh(name)
    return len(names)
