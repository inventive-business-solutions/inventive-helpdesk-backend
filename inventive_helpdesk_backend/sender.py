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

# Layer 2 of no-reply detection: local-parts that are unmonitored by convention. Matched
# on the WHOLE local part, so a real person at `noreply.patel@` is untouched.
#
# `mailer-daemon` and `postmaster` are deliberately absent — those are bounce
# infrastructure, and email._is_bounce files their mail onto the originating ticket rather
# than letting it become one.
_NO_REPLY_LOCALPART = re.compile(
    r"^(?:no-?reply|do-?not-?reply|donotreply|noreply|bounce|bounces|automated|auto-?mailer"
    r"|notification|notifications|alerts?|mailer|system)$",
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

    Deliberately advisory. Nothing here stops a ticket being created or read — a false
    positive costs a warning badge, never a lost request.
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
        email = frappe.db.get_value("POC", {"division": div, "poc_name": raised_by}, "email")
        if email:
            return email.strip().lower()
    if div:
        email = frappe.db.get_value("POC", {"division": div, "is_primary": 1}, "email")
        if email:
            return email.strip().lower()
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


def refresh_for_email(email: str) -> int:
    """Re-classify every ticket whose reply address is `email`. Returns the count.

    Bounded by one contact's tickets, so it is safe to run inline from the invite path.
    """
    email = (email or "").strip().lower()
    if not email:
        return 0
    names = frappe.get_all("Support Ticket", filters={"from_email": email}, pluck="name", limit=2000)
    for name in names:
        refresh(name)
    return len(names)
