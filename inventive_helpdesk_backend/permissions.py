"""Row-level tenant isolation + agent scoping for Inventive Helpdesk.

Support Ticket visibility has three tiers (see ticket_query / ticket_has_permission):
  - Managers (Support Manager / System Manager / Administrator): see every ticket.
  - Agents (Support Team, non-manager): scoped to their own work — assigned to them,
    raised by them, the shared triage inbox (unrouted tickets), their teams' queues,
    and tickets they collaborate on. Identity is the Team Member linked to their User.
  - Client contacts (matched via POC.user == session user): confined to their own Client,
    its Divisions, and the Support Tickets of the divisions they hold. A contact holds a
    SET of divisions (POC.divisions) — one for a division POC, several for a client Lead —
    and an empty set means no ticket access at all, which is how a Lead starts life.
Enforced server-side via permission_query_conditions (list/report) + has_permission
(direct get/save). Work notes are additionally hidden by permlevel 1 on the Support
Ticket `notes` table. (Client / Division stay staff-see-all — only tickets are scoped
per agent.)

Framework caveats (see docs: python-api/hooks):
- permission_query_conditions apply to ``frappe.get_list`` and the REST list
  endpoints, but NOT to ``frappe.get_all`` or raw ``frappe.db.sql``. Never serve
  client-facing data through those without filtering by the caller's scope.
- has_permission hooks can only DENY. Frappe invokes them with
  (doc, ptype, user, debug) and filters kwargs to each hook's signature.
"""
import frappe
from frappe.utils.caching import request_cache

# Canonical staff-role set for the app — api.py imports this so the two never
# diverge. "Administrator" is defensive: frappe.get_roles() reports it for the
# Administrator account, which also always holds System Manager.
TEAM_ROLES = {"System Manager", "Administrator", "Support Team"}

# Manager tier: staff who may manage org "master" data (clients, POCs, members,
# teams, products). System Manager/Administrator are always managers, so the owner
# can never be locked out even before "Support Manager" is granted to anyone.
MANAGER_ROLES = {"Support Manager", "System Manager", "Administrator"}


def _is_team(user: str) -> bool:
    return bool(set(frappe.get_roles(user)) & TEAM_ROLES)


def _is_manager(user: str) -> bool:
    return bool(set(frappe.get_roles(user)) & MANAGER_ROLES)


# ---- manager-only org management ------------------------------------------
_WRITE_PTYPES = {"create", "write", "delete", "submit", "cancel"}


def manager_write_gate(doc, ptype=None, user=None):
    """Deny-gate for org 'master' doctypes (Client, Division, POC, Product, Team
    Member, Assignment Group). Support Team "agents" keep READ access — they need it
    for ticket context and assignee lists — and full ticket work, but only managers
    may create/modify/delete masters. has_permission hooks can only deny, so this
    returns False to block a non-manager's mutation and True (no-op) otherwise. It's
    the server-side half of the Agent tier; the UI hides these sections too."""
    if ptype in _WRITE_PTYPES and not _is_manager(user or frappe.session.user):
        return False
    return True


@request_cache
def _poc(user: str):
    """POC scope for a portal user: their client, and the set of divisions they hold.

    A contact may hold several divisions — a division POC usually holds one, a client Lead
    holds the ones they oversee — so scope is a SET, and every portal user is filtered
    through the same rule. An EMPTY set is the normal state of a freshly created Lead and
    means no ticket access at all; callers must treat it as deny, never as "unscoped".

    get_all, not get_list: permission_query_conditions must not apply to the query that
    computes the caller's own scope, or it would recurse. Request-cached because list views
    run a permission check per row and a user's POC link can't change mid-request.
    """
    row = frappe.db.get_value("POC", {"user": user}, ["name", "client"], as_dict=True)
    if not row:
        return None
    divisions = frappe.get_all(
        "POC Division", filters={"parent": row.name, "parenttype": "POC"}, pluck="division"
    )
    return frappe._dict(
        {"name": row.name, "client": row.client, "divisions": frozenset(d for d in divisions if d)}
    )


@request_cache
def _member(user: str):
    """Team Member docname for a staff user (or None). Resolved by the User link, so
    it matches how tickets store `assignee`. Request-cached like _poc."""
    return frappe.db.get_value("Team Member", {"user": user}, "name")


@request_cache
def _member_teams(member: str) -> frozenset:
    """The Assignment Group names a member belongs to. Request-cached; uses get_all so
    it isn't itself filtered by permission_query_conditions."""
    if not member:
        return frozenset()
    rows = frappe.get_all("Assignment Group Member", filters={"member": member}, fields=["parent"])
    return frozenset(r.parent for r in rows)


# ---- Support Ticket -------------------------------------------------------
# Three visibility tiers: managers see everything; agents (Support Team, non-manager)
# are scoped to their own work — assigned-to-me ∪ tickets-I-raised ∪ the shared triage
# inbox (unrouted tickets any agent may route) ∪ my teams' queues ∪ tickets I collaborate
# on (directly or via one of my teams); client POCs stay division-scoped (unchanged).
def ticket_query(user: str | None = None) -> str:
    user = user or frappe.session.user
    if _is_manager(user):
        return ""
    if _is_team(user):
        esc_user = frappe.db.escape(user)
        m = _member(user)
        if not m:
            # Staff login with no Team Member record: only what they raised, plus the
            # shared triage inbox they can route.
            return (f"(`tabSupport Ticket`.owner = {esc_user} "
                    f"OR `tabSupport Ticket`.assignment_group IS NULL)")
        mq = frappe.db.escape(m)
        return f"""(
            `tabSupport Ticket`.assignee = {mq}
            OR `tabSupport Ticket`.owner = {esc_user}
            OR `tabSupport Ticket`.assignment_group IS NULL
            OR `tabSupport Ticket`.assignment_group IN (
                SELECT `parent` FROM `tabAssignment Group Member` WHERE `member` = {mq})
            OR EXISTS (
                SELECT 1 FROM `tabTicket Collaborator` tc
                WHERE tc.parent = `tabSupport Ticket`.name AND tc.parenttype = 'Support Ticket'
                  AND (
                    (tc.party_type = 'Member' AND tc.member = {mq})
                    OR (tc.party_type = 'Team' AND tc.team IN (
                        SELECT `parent` FROM `tabAssignment Group Member` WHERE `member` = {mq}))
                  ))
        )"""
    p = _poc(user)
    if not (p and p.divisions):
        # No POC record, or a contact with no divisions assigned yet (a Lead before anyone
        # scopes them). Deny outright — an empty scope must never widen to the whole table.
        return "1=0"
    divs = ", ".join(frappe.db.escape(d) for d in sorted(p.divisions))
    return f"`tabSupport Ticket`.division IN ({divs})"


def ticket_has_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
    # Mirrors ticket_query for direct get_doc/method reads (query conditions only cover
    # list/report). Frappe requires read to write, so this also gates agent writes.
    user = user or frappe.session.user
    if _is_manager(user):
        return True
    if _is_team(user):
        if doc.get("owner") == user:
            return True
        if not doc.get("assignment_group"):
            return True  # shared triage inbox — any agent may see/route unrouted tickets
        m = _member(user)
        if not m:
            return False
        if doc.get("assignee") == m:
            return True
        teams = _member_teams(m)
        if doc.get("assignment_group") in teams:
            return True
        for row in (doc.get("collaborators") or []):
            if row.party_type == "Member" and row.member == m:
                return True
            if row.party_type == "Team" and row.team in teams:
                return True
        return False
    p = _poc(user)
    return bool(p and p.divisions) and doc.get("division") in p.divisions


# ---- Client ---------------------------------------------------------------
def client_query(user: str | None = None) -> str:
    user = user or frappe.session.user
    if _is_team(user):
        return ""
    p = _poc(user)
    if not (p and p.client):
        return "1=0"
    return f"`tabClient`.name = {frappe.db.escape(p.client)}"


def client_has_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
    user = user or frappe.session.user
    if _is_team(user):
        return True
    p = _poc(user)
    return bool(p and p.client) and doc.get("name") == p.client


# ---- Division -------------------------------------------------------------
def division_query(user: str | None = None) -> str:
    user = user or frappe.session.user
    if _is_team(user):
        return ""
    p = _poc(user)
    if not (p and p.client):
        return "1=0"
    return f"`tabDivision`.client = {frappe.db.escape(p.client)}"


def division_has_permission(doc, ptype: str | None = None, user: str | None = None) -> bool:
    user = user or frappe.session.user
    if _is_team(user):
        return True
    p = _poc(user)
    return bool(p and p.client) and doc.get("client") == p.client
