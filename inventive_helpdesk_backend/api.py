"""Public API for the Inventive Helpdesk frontend.

Clients have read+create on Support Ticket but NOT write — so they cannot change
status/priority/assignee via the raw REST API. Their only mutations go through the
controlled whitelisted methods below (reply, reopen), which enforce read-scope and
append atomically server-side (also fixing the child-table lost-update race).

Transactions: no method here commits manually. Frappe commits automatically at the
end of a successful POST/PUT request and rolls back on any exception, so every
method is atomic — including the multi-step rename flows (see update_poc).

Every mutating method writes with ignore_permissions=True; that is safe ONLY
because each one first authorizes explicitly (_require_team / _require_read).
Any new method added here MUST start with one of those guards.
"""
import json
from datetime import timedelta
from functools import wraps

import frappe
from frappe import _
from frappe.model.rename_doc import rename_doc
from frappe.rate_limiter import rate_limit
from frappe.sessions import get_csrf_token
from frappe.utils import add_days, cint, now_datetime
from frappe.utils.password import get_password_reset_limit

from inventive_helpdesk_backend.access import assert_user_unclaimed
from inventive_helpdesk_backend.permissions import MANAGER_ROLES, OWNER_ROLES, TEAM_ROLES


def _is_team(user: str | None = None) -> bool:
    return bool(set(frappe.get_roles(user or frappe.session.user)) & TEAM_ROLES)


def _is_manager(user: str | None = None) -> bool:
    return bool(set(frappe.get_roles(user or frappe.session.user)) & MANAGER_ROLES)


def _require_team():
    if not _is_team():
        frappe.throw(_("Only support staff can perform this action"), frappe.PermissionError)


def _is_owner(user: str | None = None) -> bool:
    """May this user DELEGATE admin access? Narrower than _is_manager on purpose."""
    return bool(set(frappe.get_roles(user or frappe.session.user)) & OWNER_ROLES)


def _require_owner():
    if not _is_owner():
        frappe.throw(
            _("Only a Lead Administrator can grant or revoke Administrator access"), frappe.PermissionError
        )


def _require_manager():
    if not _is_manager():
        frappe.throw(_("Only a support manager can manage clients, members and teams"), frappe.PermissionError)


def _author() -> str:
    return frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user


def _as_list(value) -> list:
    """Coerce a whitelisted endpoint's list argument to a real list.

    A JSON request body arrives already decoded, but the same call made as form data (or
    via `frappe.call`) delivers the array as a JSON *string* — so a bare `for x in value`
    would silently iterate the characters of "[\"a\"]" and write garbage rows. Anything
    unparseable is treated as empty rather than raising: these feed permission scopes, and
    an empty scope denies, which is the safe direction to fail."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _norm_attachments(attachments) -> str:
    if attachments is None:
        return "[]"
    if isinstance(attachments, str):
        return attachments
    return json.dumps(attachments)


# ---- per-user rate limits ------------------------------------------------
# frappe's own @rate_limit is IP-based by default, and that is the wrong axis here. Every
# agent sits behind one corporate NAT, so an IP limit on a working endpoint is a limit on
# the whole support team at once: one person pasting a long thread of replies would lock
# out their colleagues. The alternative the decorator offers, `key=`, reads a form field —
# which the caller controls and can simply vary, so it bounds an honest client and not an
# abusive one.
#
# So these key on the SESSION USER, which is the thing actually being limited. This is not
# a defence against a determined attacker with many accounts; it is a bound on one
# compromised login, a runaway retry loop, or a UI bug that fires on every keystroke —
# which is what the uncapped endpoints below were exposed to.
#
# Limits are set far above human use and far below abuse. Nobody writes 200 replies in an
# hour; a loop does it in a minute.
#
# frappe.in_test skips it, deliberately. The counters live in redis and outlive a test
# run, so a suite that exercises add_message dozens of times would start failing on its
# second run for reasons having nothing to do with the code under test. The mechanism is
# covered directly instead (tests/test_rate_limit.py), including a structural test that
# these decorators stay attached.
_RATE_LIMITS = {
    "message": (200, 3600),      # client-visible replies
    "note": (200, 3600),         # internal work notes
    "attachment": (100, 3600),   # 10 MB each, so this also bounds storage per user per hour
    "invite": (100, 3600),       # each one sends mail; a loop here gets the domain throttled
}


def _rate_limit_key(action: str, user: str) -> str:
    """Redis key for one user's budget for one action.

    The site is in the key by hand, as in email._ack_key and for the same reason: incr and
    expire come straight from redis.Redis and act on the RAW key, while frappe's own
    get_value/set_value prefix the site themselves. Mixing the two families addresses two
    different keys and neither works.
    """
    return f"helpdesk:rl:{frappe.local.site}:{action}:{user}"


def _enforce_rate_limit(action: str):
    """Count one call against the session user's budget, and throw once it is spent."""
    if frappe.in_test:
        return
    limit, seconds = _RATE_LIMITS[action]
    key = _rate_limit_key(action, frappe.session.user)
    cache = frappe.cache()
    count = cache.incr(key)
    if count == 1:
        cache.expire(key, seconds)
    if count > limit:
        frappe.throw(
            _("You have made too many requests. Please wait a few minutes and try again."),
            frappe.RateLimitExceededError,
        )


def rate_limited(action: str):
    """Decorate a whitelisted endpoint with a per-user budget.

    Goes INSIDE @frappe.whitelist(), matching how frappe applies its own rate_limit to
    request_password_reset below: whitelist has to register the outermost callable, and
    functools.wraps keeps the signature frappe introspects to map form fields to arguments.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            _enforce_rate_limit(action)
            return fn(*args, **kwargs)

        # An explicit marker, so the wiring can be asserted. The obvious check —
        # hasattr(fn, "__wrapped__") — proves nothing: frappe.whitelist wraps every
        # endpoint it registers, so that attribute is present on all of them, decorated or
        # not, and a test built on it passes just as happily once the decorator falls off.
        # This also records WHICH budget, so a rename cannot silently repoint an endpoint.
        wrapper._rate_limit_action = action
        return wrapper

    return decorator


def _require_read(ticket: str):
    doc = frappe.get_doc("Support Ticket", ticket)
    if not doc.has_permission("read"):
        frappe.throw(_("You are not permitted to access this ticket"), frappe.PermissionError)
    return doc


@frappe.whitelist()
def me():
    """Session context + CSRF token for the signed-in user."""
    user = frappe.session.user
    if user == "Guest":
        return {"user": "Guest", "role": None}

    ctx = {
        "user": user,
        "name": _author(),
        "role": "admin" if _is_team(user) else "client",
        # Within the staff app, managers manage the org; agents only work tickets.
        "manage": _is_manager(user),
        # Owners delegate; delegated managers do everything else. Sent so the
        # sidebar can hide a section the server would refuse anyway.
        "is_owner": _is_owner(user),
        "csrf_token": get_csrf_token(),
    }
    # Staff identity in Team-Member terms: the frontend uses `member` (the Team Member
    # docname) to match ticket.assignee and `teams` to build "my team's queue" views.
    # Resolve by the User link, never by display name — assignee stores the docname
    # while `name` above is the User's full name, and the two can differ.
    if _is_team(user):
        row = frappe.db.get_value("Team Member", {"user": user}, ["name", "title"], as_dict=True)
        member = row.name if row else None
        ctx["member"] = member
        # Job title (e.g. "Business Development Executive") — free text, often unset;
        # the frontend falls back to a generic label when it's blank.
        ctx["title"] = (row.title or "").strip() if row else ""
        ctx["teams"] = (
            [r.parent for r in frappe.get_all(
                "Assignment Group Member", filters={"member": member}, fields=["parent"])]
            if member else []
        )
        ctx["is_agent"] = not ctx["manage"]
    poc = frappe.db.get_value("POC", {"user": user}, ["name", "client", "is_lead"], as_dict=True)
    if poc:
        divisions = frappe.get_all(
            "POC Division",
            filters={"parent": poc.name, "parenttype": "POC"},
            pluck="division",
            order_by="idx",
        )
        # `divisions` is the real scope — a contact may hold several, and a Lead may hold
        # none at all (no ticket access yet). The singular keys below are the FIRST entry,
        # kept only so views that still read `session.division` keep working; they are not
        # the authority and must not be used for filtering.
        first = divisions[0] if divisions else None
        d = (frappe.db.get_value("Division", first, ["division_name", "division_code"], as_dict=True)
             if first else None) or {}
        ctx.update({
            "client": poc.client,
            "is_lead": bool(poc.is_lead),
            "divisions": divisions,
            "division": first,
            "division_name": d.get("division_name") or first,
            "division_code": d.get("division_code") or "",
        })
    return ctx


@frappe.whitelist(methods=["POST"])
@rate_limited("message")
def add_message(ticket: str, body: str, attachments=None, send_email=None):
    """Append a client-visible message. Allowed for the ticket's client POC or staff.

    `send_email` is the agent's "Send reply over email" toggle, and it is a REQUEST, not an
    instruction — sender.reply_plan has the final say. It is ignored entirely unless the
    ticket belongs to a registered user, because everyone else has no portal to read the
    reply in and switching email off would mean replying into a void.
    """
    body = (body or "").strip()
    if not body and not attachments:
        frappe.throw(_("Message cannot be empty"))
    doc = _require_read(ticket)
    team = _is_team()
    doc.append("conversation", {
        "kind": "team" if team else "client",
        "author": _author(),
        "role": "Team → Client" if team else "Client",
        "message_on": now_datetime(),
        "body": body,
        "attachments": _norm_attachments(attachments),
    })
    doc.last_activity_on = now_datetime()
    doc.save(ignore_permissions=True)
    # You are not "unread" on your own message.
    _mark_read(doc.name)
    # A staff member's client-visible reply. Whether it also goes out by email is policy,
    # not a caller decision — see sender.reply_plan.
    if team:
        from inventive_helpdesk_backend import sender
        from inventive_helpdesk_backend.email import notify_client_reply

        requested = None if send_email is None else bool(cint(send_email))
        send, kind, why = sender.reply_plan(doc, requested_email=requested)
        if send:
            notify_client_reply(doc, body, kind=kind)
            # Stamp on ANY reply email, not just the one-time notification: the exception
            # exists because the client has never had a reply by mail, and a forced or
            # requested one satisfies that just as well.
            if not doc.first_response_notified_on:
                doc.db_set("first_response_notified_on", now_datetime(), update_modified=False)
        # Returned so the UI can tell the agent what actually happened rather than leaving
        # them to infer it from a toggle they may not have looked at.
        return {"ticket": doc.name, "emailed": send, "detail": why}
    return {"ticket": doc.name, "emailed": False, "detail": "client message"}


@frappe.whitelist(methods=["POST"])
@rate_limited("note")
def add_note(ticket: str, body: str, attachments=None):
    """Append an internal work note. Staff only (never visible to clients)."""
    _require_team()
    body = (body or "").strip()
    if not body and not attachments:
        frappe.throw(_("Note cannot be empty"))
    # _require_read, not a bare get_doc: _require_team only proves the caller is staff,
    # and the agent tier is scoped (assigned-to-me / my teams' queues / triage). Without
    # this any agent could write an internal note onto any ticket by name — including
    # another team's — and bump its last_activity_on. Every sibling method here uses it.
    doc = _require_read(ticket)
    doc.append("notes", {
        "author": _author(),
        "note_on": now_datetime(),
        "body": body,
        "attachments": _norm_attachments(attachments),
    })
    doc.last_activity_on = now_datetime()
    doc.save(ignore_permissions=True)
    _mark_read(doc.name)
    return doc.name


@frappe.whitelist(methods=["POST"])
def reopen(ticket: str):
    """Let the owning client (or staff) reopen a resolved/closed ticket."""
    doc = _require_read(ticket)
    if doc.status not in ("Resolved", "Closed"):
        frappe.throw(_("Only resolved or closed tickets can be reopened"))
    doc.status = "Reopened"
    doc.save(ignore_permissions=True)
    return doc.name


# ---- unread markers -------------------------------------------------------
# A ticket is unread for an agent when something was SAID on it — a client message or an
# internal note — since that agent last opened it. Tracked per user rather than per ticket
# so one member reading a client reply doesn't clear the marker for the rest of the team.
def _mark_read(ticket: str, user: str | None = None):
    """Stamp (ticket, user) as read now. Upsert: one row per pair, ever."""
    user = user or frappe.session.user
    if user in ("Administrator", "Guest"):
        return
    name = frappe.db.get_value("Ticket Read Receipt", {"ticket": ticket, "user": user})
    if name:
        frappe.db.set_value("Ticket Read Receipt", name, "read_on", now_datetime())
    else:
        frappe.get_doc({
            "doctype": "Ticket Read Receipt",
            "ticket": ticket,
            "user": user,
            "read_on": now_datetime(),
        }).insert(ignore_permissions=True)


# The unread sweep is bounded on two axes. It runs on every agent's 30-second poll, so its
# cost is paid per agent per half-minute for the life of the site, and it was unbounded on
# both: every ticket ever given a `last_activity_on`, then a second query with all of those
# names in one IN clause. That is flat at a few hundred tickets and grows without limit.
#
# The window is what makes it bounded rather than merely large: an unread marker for
# something nobody has looked at in three months is not a signal an agent is going to act
# on, whereas anything recent is. Note it keys on ACTIVITY, not creation — a client
# replying today to a ticket resolved last year has today's timestamp, so the case that
# most needs the dot stays inside the window.
#
# The limit is a backstop for a site busy enough that even 90 days is a lot, and it is
# ordered so the truncation drops the oldest of the recent rather than an arbitrary slice.
_UNREAD_WINDOW_DAYS = 90
_UNREAD_LIMIT = 500


@frappe.whitelist()
def unread_tickets():
    """Ticket names with activity this agent hasn't seen. Staff only, scoped, and bounded.

    `get_list`, not `get_all`: get_all ignores permission_query_conditions, so it would
    report activity on every ticket on the site — including other teams' and other
    clients' — to any staff member who called it. Nothing visibly leaked, because the UI
    only draws a dot on rows it already fetched through the scoped list, but the endpoint
    is whitelisted and answers a direct REST call too. The agent tier is deliberately
    narrow everywhere else in this file; it has no business being wide here.

    Bounded to the last _UNREAD_WINDOW_DAYS days of activity, most recent first, capped at
    _UNREAD_LIMIT rows. Activity older than the window stops being reported as unread —
    a deliberate behaviour change, and the point of the bound.
    """
    _require_team()
    user = frappe.session.user
    # `>` rather than a separate ["is", "set"]: a NULL last_activity_on fails the
    # comparison, so this subsumes the old filter instead of stacking with it.
    rows = frappe.get_list(
        "Support Ticket",
        filters={"last_activity_on": [">", add_days(now_datetime(), -_UNREAD_WINDOW_DAYS)]},
        fields=["name", "last_activity_on"],
        order_by="last_activity_on desc",
        # `limit`, not `limit_page_length`: the latter is deprecated for removal in v17
        # (frappe/model/qb_query.py:153) and emits a warning on every call — and this one
        # runs on every agent's poll, so it would be the loudest source of it on the site.
        limit=_UNREAD_LIMIT,
    )
    if not rows:
        return []
    seen = {
        r.ticket: r.read_on
        for r in frappe.get_all(
            "Ticket Read Receipt",
            filters={"user": user, "ticket": ["in", [r.name for r in rows]]},
            fields=["ticket", "read_on"],
        )
    }
    # No receipt at all => never opened => unread.
    return [r.name for r in rows if not seen.get(r.name) or seen[r.name] < r.last_activity_on]


@frappe.whitelist(methods=["POST"])
def mark_ticket_read(ticket: str):
    """Called when an agent opens a ticket. Staff only."""
    _require_team()
    _require_read(ticket)
    _mark_read(ticket)
    return ticket


# ---- dashboard aggregates -------------------------------------------------
# Every figure on the dashboard was computed in the browser by filtering the full ticket
# array — which is why the array had to be the full ticket set in the first place, and why
# capping that fetch made the figures quietly too small. Counting here removes the reason
# the browser needed every row.
#
# These MUST mirror the frontend's own classification exactly (lib/helpers.ts), or the
# dashboard and the ticket list disagree about the same word. Kept adjacent and named the
# same so a change to one is an obvious prompt to change the other.
_ACTIVE_STATUSES = ("New", "Acknowledged", "In Progress", "Pending Client", "Reopened")
_RESOLVED_STATUSES = ("Resolved", "Closed")
# helpers.needsAttention: New, or Pending Client for this long, or an active SLA risk.
_PENDING_CLIENT_STALE_DAYS = 5


def _stat_count(filters) -> int:
    """One permission-scoped COUNT.

    `get_list`, never `get_all` or db.sql: permission_query_conditions apply to the former
    only, and an aggregate that ignores them reports OTHER TENANTS' totals — a smaller leak
    than listing their tickets but the same boundary. Verified against a two-client fixture
    in tests/test_ticket_stats.py rather than assumed.

    The dict form is required by v16 — a "count(name)" string is rejected outright — and
    the result key is the rendered SQL, so read the single value rather than name it.
    """
    rows = frappe.get_list("Support Ticket", filters=filters, fields=[{"COUNT": "name"}], limit=0)
    return int(next(iter(rows[0].values()))) if rows else 0


def _stat_group(field: str, filters=None) -> dict:
    """Permission-scoped COUNT grouped by one field. Empty/NULL groups key on ""."""
    rows = frappe.get_list(
        "Support Ticket",
        filters=filters or {},
        fields=[field, {"COUNT": "name"}],
        group_by=field,
        limit=0,
    )
    out = {}
    for row in rows:
        # The count column is whichever key is not the grouping field; its name is the
        # rendered SQL ("COUNT(`name`)") and is not worth depending on.
        count = next(v for k, v in row.items() if k != field)
        out[row.get(field) or ""] = int(count)
    return out


def _needs_attention_count(active_filter) -> int:
    """Tickets needing attention, counted without double counting.

    helpers.needsAttention is a disjunction — New, OR Pending Client and stale, OR an SLA
    risk that is still active — and the three overlap: a New ticket flagged at risk
    satisfies two of them and is ONE ticket. Frappe's or_filters cannot express a
    conjunction inside a disjunction anyway, so this counts four disjoint sets instead:

      New  +  (Pending Client & stale)  +  (at risk & neither of those)

    split in two because "neither of those" spans both a status set and a date.
    """
    cutoff = add_days(now_datetime(), -_PENDING_CLIENT_STALE_DAYS)
    # Active statuses that are neither New nor Pending Client — an SLA risk here is only
    # counted by this term.
    other_active = [s for s in _ACTIVE_STATUSES if s not in ("New", "Pending Client")]
    return (
        _stat_count({"status": "New"})
        + _stat_count({"status": "Pending Client", "creation": ["<=", cutoff]})
        + _stat_count({"sla_risk": 1, "status": ["in", other_active]})
        # A Pending Client ticket NOT yet stale, so not already counted above.
        + _stat_count({"sla_risk": 1, "status": "Pending Client", "creation": [">", cutoff]})
    )


def _trend(weeks: int) -> list:
    """Weekly created-vs-resolved buckets, anchored on the most recent ticket.

    Anchored on the latest ticket rather than today, matching the chart this replaces — on
    a quiet site an empty trailing week reads as an outage rather than a quiet week.

    This one reads rows rather than grouping, because the buckets are seven-day windows
    from a moving anchor and expressing that as SQL would tie the result to one database's
    date functions. It is bounded by the window: two columns for the tickets created in it,
    not every ticket ever.
    """
    latest = frappe.get_list(
        "Support Ticket", fields=["creation"], order_by="creation desc", limit=1
    )
    if not latest:
        return []
    anchor = latest[0]["creation"]
    start = add_days(anchor, -(weeks * 7 - 1))
    rows = frappe.get_list(
        "Support Ticket",
        filters={"creation": [">=", start]},
        fields=["creation", "status"],
        limit=0,
    )
    buckets = []
    for i in range(weeks - 1, -1, -1):
        end = add_days(anchor, -i * 7)
        begin = add_days(end, -6)
        in_week = [r for r in rows if begin <= r["creation"] <= end]
        buckets.append({
            "week": f"{begin.strftime('%b')} {begin.day}",
            "created": len(in_week),
            "resolved": sum(1 for r in in_week if r["status"] in _RESOLVED_STATUSES),
        })
    return buckets


@frappe.whitelist()
def ticket_stats(trend_weeks=8):
    """Dashboard figures, counted in the database and scoped to the caller.

    Not staff-gated: every query below goes through get_list, so a portal contact receives
    the same shape computed over their own divisions only. That is what makes it usable by
    the portal as well as the two dashboards.
    """
    weeks = max(1, min(cint(trend_weeks) or 8, 52))
    active = {"status": ["in", list(_ACTIVE_STATUSES)]}
    return {
        "counts": {
            "total": _stat_count({}),
            "active": _stat_count(active),
            "resolved": _stat_count({"status": ["in", list(_RESOLVED_STATUSES)]}),
            "needs_attention": _needs_attention_count(active),
            "sla_risk": _stat_count({**active, "sla_risk": 1}),
            "email": _stat_count({"source": "Email"}),
            # Assignment gaps among open tickets: no team at all (where emailed-in tickets
            # land) vs routed to a team but nobody has claimed it.
            # Both conditions, matching the frontend. SupportTicket.validate rejects an
            # assignee without a team, so "no team" should already imply "unassigned" —
            # but that rule only fires when the assignment changes, so a row predating it
            # can violate it, and this figure would then count a ticket somebody owns.
            "to_system": _stat_count(
                {**active, "assignment_group": ["is", "not set"], "assignee": ["is", "not set"]}
            ),
            "to_member": _stat_count(
                {**active, "assignment_group": ["is", "set"], "assignee": ["is", "not set"]}
            ),
        },
        # Status spans everything — it IS the pipeline. The rest describe open work only,
        # matching the dashboard's own labels ("Open by priority").
        "by_status": _stat_group("status"),
        "by_priority": _stat_group("priority", active),
        "by_type": _stat_group("ticket_type", active),
        "by_client": _stat_group("client", active),
        "by_assignee": _stat_group("assignee", active),
        "by_team": _stat_group("assignment_group", active),
        "trend": _trend(weeks),
    }


def _my_member() -> str:
    """The signed-in staff user's Team Member docname (or None). Resolved by the
    User link, matching how tickets store `assignee`."""
    return frappe.db.get_value("Team Member", {"user": frappe.session.user}, "name")


def _log_activity(doc, action: str, old=None, new=None):
    """Append a row to the ticket's activity log.

    Only for events the field diff in SupportTicket.before_save cannot see — i.e.
    collaborator add/remove, which are child-table changes rather than scalar fields.
    Scalar changes (status, priority, assignee, team) are recorded by the hook, so
    logging them here too would double up. The caller saves; this only appends, so
    the row lands in the same write as the change."""
    doc.append("activity", {
        "action": action,
        "old_value": old,
        "new_value": new,
        "author": _author(),
        "acted_on": now_datetime(),
    })


@frappe.whitelist(methods=["POST"])
def claim_ticket(ticket: str):
    """Let a team member pick up a ticket from their team's queue (self-assign).
    Team-first: the ticket must already be routed to a team, and the member must
    belong to that team. Bumps a New ticket to Acknowledged. Logs a work note."""
    _require_team()
    doc = _require_read(ticket)
    member = _my_member()
    if not member:
        frappe.throw(_("Only a team member can claim a ticket"))
    if not doc.assignment_group:
        frappe.throw(_("This ticket has no team yet — route it to a team first"))
    in_team = frappe.db.exists(
        "Assignment Group Member", {"parent": doc.assignment_group, "member": member})
    if not in_team and not _is_manager():
        frappe.throw(_("You can only claim tickets routed to your team"))
    if doc.assignee and doc.assignee != member and not _is_manager():
        frappe.throw(_("This ticket is already claimed by {0}").format(doc.assignee))
    doc.assignee = member
    if doc.status == "New":
        doc.status = "Acknowledged"
    # No explicit log line: before_save records the assignee (and status) change, and
    # the UI reads a self-assignment — author == the new assignee — as "claimed".
    doc.save(ignore_permissions=True)
    return doc.name


def _collab_key(row):
    """The party a collaborator row points at (team name or member name)."""
    return row.team if row.party_type == "Team" else row.member


@frappe.whitelist(methods=["POST"])
def add_collaborator(ticket: str, party_type: str, party: str):
    """Loop an additional team or member onto a ticket (a "Collaborator"). They gain
    read access and can post internal notes, without taking ownership. Staff only."""
    _require_team()
    doc = _require_read(ticket)
    if party_type not in ("Team", "Member"):
        frappe.throw(_("A collaborator must be a Team or a Member"))
    party = (party or "").strip()
    if not party:
        frappe.throw(_("Choose a team or member to add"))
    target_doctype = "Assignment Group" if party_type == "Team" else "Team Member"
    if not frappe.db.exists(target_doctype, party):
        frappe.throw(_("{0} {1} does not exist").format(party_type, party))
    # The owner already has access — keep the collaborator list meaningful.
    if party_type == "Team" and party == doc.assignment_group:
        frappe.throw(_("{0} already owns this ticket").format(party))
    if party_type == "Member" and party == doc.assignee:
        frappe.throw(_("{0} is already assigned to this ticket").format(party))
    if any(r.party_type == party_type and _collab_key(r) == party for r in (doc.collaborators or [])):
        frappe.throw(_("{0} is already a collaborator").format(party))
    field = "team" if party_type == "Team" else "member"
    doc.append("collaborators", {
        "party_type": party_type,
        field: party,
        "added_by": _author(),
        "added_on": now_datetime(),
    })
    _log_activity(doc, "Collaborator", new=party)
    doc.save(ignore_permissions=True)
    return doc.name


@frappe.whitelist(methods=["POST"])
def remove_collaborator(ticket: str, party_type: str, party: str):
    """Remove a looped-in collaborator. Staff only."""
    _require_team()
    doc = _require_read(ticket)
    party = (party or "").strip()
    rows = doc.collaborators or []
    remaining = [r for r in rows if not (r.party_type == party_type and _collab_key(r) == party)]
    if len(remaining) == len(rows):
        frappe.throw(_("{0} is not a collaborator on this ticket").format(party))
    doc.set("collaborators", remaining)
    _log_activity(doc, "Collaborator", old=party)
    doc.save(ignore_permissions=True)
    return doc.name


# Cap uploads defensively (Frappe's own `max_file_size` still applies on top).
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _attach_private_file(ticket_doc, filename, content, on_ticket=False):
    """Store `content` as a PRIVATE File attached to the ticket, so download permission
    derives from ticket access — a client can only fetch files on their own tickets (the
    File's `has_permission` defers to the attached Support Ticket, i.e. tenant isolation).
    Returns {name, url}. `on_ticket` also records the ref on the ticket's description-level
    `attachments` field (used when a ticket is raised with attachments)."""
    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_name": (filename or "file").strip() or "file",
        "attached_to_doctype": "Support Ticket",
        "attached_to_name": ticket_doc.name,
        "is_private": 1,
        "content": content,
    }).insert(ignore_permissions=True)
    ref = {"name": file_doc.file_name, "url": file_doc.file_url}
    if on_ticket:
        try:
            current = json.loads(ticket_doc.get("attachments") or "[]")
        except (ValueError, TypeError):
            current = []
        current.append(ref)
        ticket_doc.db_set("attachments", json.dumps(current), update_modified=False)
    return ref


@frappe.whitelist(methods=["POST"])
@rate_limited("attachment")
def upload_attachment(ticket, on_ticket=0):
    """Attach a multipart-uploaded file (form field `file`) to a ticket as a private file,
    after a tenant-scope read check (client → own ticket only; staff → any). Returns
    {name, url}. `on_ticket=1` also records it on the ticket's description-level list."""
    doc = _require_read(ticket)
    upload = frappe.request.files.get("file") if getattr(frappe, "request", None) else None
    if upload is None:
        frappe.throw(_("No file was received"))
    content = upload.read()
    if not content:
        frappe.throw(_("The uploaded file is empty"))
    if len(content) > _MAX_ATTACHMENT_BYTES:
        frappe.throw(_("Attachments must be 10 MB or smaller"))
    return _attach_private_file(doc, upload.filename, content, cint(on_ticket))


@frappe.whitelist()
def list_admins():
    """Only the members who currently hold admin — Administrators and Lead Administrators.

    Not every team member. This is the list of people WITH access, so it should answer
    "who can manage this org" at a glance; padding it with everyone who cannot turns the
    answer into something you have to search for. Promoting someone starts from
    admin_candidates instead.

    Owner-only: who holds admin is itself information about how the site is governed.
    """
    _require_owner()
    rows = frappe.get_all(
        "Team Member",
        fields=["name", "member_name", "email", "title", "status", "user"],
        limit_page_length=0,
        order_by="member_name asc",
    )
    out = []
    for r in rows:
        if not r.get("user"):
            continue
        roles = frappe.get_roles(r["user"])
        r["is_admin"] = "Support Manager" in roles
        r["is_owner"] = _is_owner(r["user"])
        r["can_delegate"] = True
        if r["is_admin"] or r["is_owner"]:
            out.append(r)
    return out


@frappe.whitelist()
def admin_candidates():
    """Members who could be promoted: linked account, not already an admin, not you.

    Exactly the set set_member_admin will accept, derived from the same conditions rather
    than restated — a picker offering someone the endpoint then refuses is worse than one
    that is simply shorter.
    """
    _require_owner()
    rows = frappe.get_all(
        "Team Member",
        fields=["name", "member_name", "email", "title", "status", "user"],
        limit_page_length=0,
        order_by="member_name asc",
    )
    return [
        r
        for r in rows
        if r.get("user")
        and r["user"] != frappe.session.user
        and not _is_owner(r["user"])
        and "Support Manager" not in frappe.get_roles(r["user"])
    ]


@frappe.whitelist(methods=["POST"])
def invite_admin(member_name, email, title=None):
    """Invite someone who is not on the team yet, straight in as an Administrator.

    An administrator is not necessarily an agent: the person running the org may never
    work a ticket, and making them join a team first is a step that exists only because
    the data model happens to hang staff logins off Team Member.

    So this composes the pieces rather than inventing new ones — create the Team Member,
    invite_member provisions the login and mails the set-password link, then the manager
    role goes on top. Idempotent on the email, so a second attempt re-invites rather than
    failing on a duplicate.
    """
    _require_owner()
    email = (email or "").strip().lower()
    member_name = (member_name or "").strip()
    if not email or "@" not in email:
        frappe.throw(_("Enter a valid email address"))
    if not member_name:
        frappe.throw(_("Enter the person's name"))

    existing = frappe.db.get_value("Team Member", {"email": email}, "name")
    if existing:
        member = existing
    else:
        member = frappe.get_doc(
            {
                "doctype": "Team Member",
                "member_name": member_name,
                "email": email,
                "title": (title or "").strip() or None,
                "status": "Not Invited",
            }
        ).insert(ignore_permissions=True).name

    # Provision only when there is no account yet. invite_member deliberately refuses an
    # address that already holds Support Manager — it guards against an administrator
    # being handed out as a directory invite — so calling it on someone this function
    # already promoted would fail on its own previous success.
    user = frappe.db.get_value("Team Member", member, "user")
    result = {"email_sent": False}
    if not user:
        result = invite_member(member)
        user = frappe.db.get_value("Team Member", member, "user")
    if user and not _is_owner(user):
        u = frappe.get_doc("User", user)
        if "Support Manager" not in {r.role for r in u.roles}:
            u.append("roles", {"role": "Support Manager"})
            u.save(ignore_permissions=True)
    frappe.get_doc("Team Member", member).add_comment(
        "Comment", _("Invited as Administrator by {0}").format(frappe.session.user)
    )
    return {"member": member, "email_sent": result.get("email_sent"), "user": user}


@frappe.whitelist(methods=["POST"])
def revoke_account(member):
    """Take someone out of the system entirely: disable the login and end their sessions.

    Removing a Team Member only deleted the record. The User stayed enabled and kept its
    roles, so the person could sign back in — landing in the app with no member link, which
    reads as a broken account rather than a closed one. Deleting the row is the bookkeeping;
    THIS is the part that actually removes access.

    Sessions are dropped rather than left to expire, because a revocation someone keeps
    using until their cookie ages out is not a revocation. Their next request 401s and the
    client's existing auth-loss handling bounces them to /login, where `enabled = 0` gives
    the "no longer has access" message rather than a credentials hint.

    Owner-only, and never yourself — locking yourself out is not an action worth offering.
    """
    _require_owner()
    row = frappe.db.get_value("Team Member", member, ["name", "member_name", "user"], as_dict=True)
    if not row:
        frappe.throw(_("That team member no longer exists"), frappe.DoesNotExistError)
    if row.user and row.user == frappe.session.user:
        frappe.throw(_("You cannot remove your own account"))
    if row.user and _is_owner(row.user):
        frappe.throw(_("{0} is a Lead Administrator — their account is not removed here").format(row.member_name))

    if not row.user:
        return {"member": member, "disabled": False, "sessions_cleared": 0}

    user = frappe.get_doc("User", row.user)
    user.enabled = 0
    # Strip app roles too: re-enabling later should be a deliberate re-grant, not a
    # silent restoration of whatever they held when they left.
    user.roles = [r for r in user.roles if r.role not in {"Support Manager", "Support Team"}]
    # And burn any outstanding invite / reset key. access.py's orphan path already did this
    # and explained why; THIS path — the deliberate "remove this person" button — did not,
    # so someone revoked with an unopened invite in their inbox kept a working link to an
    # account that had just been closed.
    #
    # Redundant now that set_password_with_key checks `enabled` at redemption, and kept
    # anyway: that check is the guarantee, this is the one that means there is no live
    # credential sitting in a mailbox waiting for the guarantee to be the only thing left.
    user.reset_password_key = ""
    user.save(ignore_permissions=True)

    cleared = frappe.db.count("Sessions", {"user": row.user})
    frappe.db.delete("Sessions", {"user": row.user})
    return {"member": member, "disabled": True, "sessions_cleared": cleared}


@frappe.whitelist(methods=["POST"])
def set_member_admin(member, admin):
    """Grant or revoke delegated admin (the Support Manager role) for one team member.

    Owner-only. A delegated admin gets the full manager surface but cannot reach this
    endpoint, so admin cannot spread on its own — escalation is prevented by who may call
    this, not by a UI that hides a button.

    Three refusals, each protecting a way of losing access to the site:
      - your own access, the fastest possible self-lockout;
      - an owner's, which this tier does not grant and so must not take away;
      - a member with no linked account, where there is no user to hold the role.
    """
    _require_owner()
    admin = bool(frappe.parse_json(admin) if isinstance(admin, str) else admin)

    row = frappe.db.get_value("Team Member", member, ["name", "member_name", "user"], as_dict=True)
    if not row:
        frappe.throw(_("That team member no longer exists"), frappe.DoesNotExistError)
    if not row.user:
        frappe.throw(
            _("{0} has not accepted their invite yet, so there is no account to grant access to").format(
                row.member_name
            )
        )
    if row.user == frappe.session.user:
        frappe.throw(_("You cannot change your own Administrator access"))
    if _is_owner(row.user):
        frappe.throw(_("{0} is a Lead Administrator — their access is not managed here").format(row.member_name))

    user = frappe.get_doc("User", row.user)
    has = "Support Manager" in {r.role for r in user.roles}
    if admin and not has:
        user.append("roles", {"role": "Support Manager"})
    elif not admin and has:
        user.roles = [r for r in user.roles if r.role != "Support Manager"]
    else:
        return {"member": member, "is_admin": admin, "changed": False}
    user.save(ignore_permissions=True)

    # Leaves a trail on the member record: granting someone the run of the org is worth
    # being able to answer "who did this, and when" about later.
    frappe.get_doc("Team Member", member).add_comment(
        "Comment",
        _("Admin access {0} by {1}").format(_("granted") if admin else _("revoked"), frappe.session.user),
    )
    return {"member": member, "is_admin": admin, "changed": True}


@frappe.whitelist(methods=["POST"])
def update_member(name, member_name=None, title=None, email=None):
    """Edit a team member. Renames the doc when member_name changes — `assignee`
    (Support Ticket) and `member` (Assignment Group) are Link fields, so Frappe
    cascades the rename to every reference automatically. Staff only."""
    _require_manager()
    doc = frappe.get_doc("Team Member", name)
    if title is not None:
        doc.title = title or None
    if email is not None:
        doc.email = email or None
    doc.save(ignore_permissions=True)
    new_name = (member_name or "").strip()
    if new_name and new_name != name:
        frappe.rename_doc("Team Member", name, new_name, force=True)
        name = new_name
    return name


# ---- client / POC administration (staff only) -----------------------------
@frappe.whitelist(methods=["POST"])
def update_client(name, client_name=None, client_code=None, since=None, status=None):
    """Edit a client, including a rename. `name` (autonamed from client_name) is a
    Link target on Support Ticket, Division and POC, so frappe.rename_doc cascades
    the new name to every reference.

    The `product` parameter is gone with the `Client.product` field it wrote. A client's
    products are Client Product engagements now — see create/update/delete_client_product.
    Keeping the writer would have quietly refilled the field that
    `clear_legacy_client_product` empties."""
    _require_manager()
    doc = frappe.get_doc("Client", name)
    if client_code is not None:
        doc.client_code = client_code
    if since is not None:
        doc.since = since or None
    if status:
        doc.status = status
    doc.save(ignore_permissions=True)
    new_name = (client_name or "").strip()
    if new_name and new_name != name:
        frappe.rename_doc("Client", name, new_name, force=True)
        name = new_name
    return name


@frappe.whitelist(methods=["POST"])
def update_product(name, product_name=None):
    """Rename a product. Product is autonamed by product_name and is a Link target on
    Client Product and Support Ticket, so rename_doc cascades the new name to every
    engagement and every ticket already raised against it."""
    _require_manager()
    new_name = (product_name or "").strip()
    if new_name and new_name != name:
        frappe.rename_doc("Product", name, new_name, force=True)
        name = new_name
    return name


@frappe.whitelist(methods=["POST"])
def update_group(name, group_name=None, lead=None):
    """Rename a team and/or set its lead, in that order and in one call.

    Order matters and is not interchangeable. Assignment Group is autonamed by
    `group_name`, so a rename changes the docname every other write has to address — doing
    the lead first and the rename second would write to a name that no longer exists by the
    time the caller sees the result. Both in one endpoint also means a failed rename cannot
    leave a lead applied to a team the caller believes was renamed.

    `rename_doc` cascades: Support Ticket.assignment_group and Ticket Collaborator.team both
    Link here, so every ticket already routed to this team follows the new name.

    `lead` distinguishes absent from empty. None means "leave it alone"; "" means "clear it".
    Without that split, any caller omitting the field would silently unset the lead — the
    Manage dialog always sends it, but a future caller updating only the name should not have
    to know that.

    A named lead is added to the team if they are not already in it. A team led by someone
    who is not on it is the state the UI would render as a contradiction, and this is the
    only place that can prevent it atomically.
    """
    _require_manager()
    new_name = (group_name or "").strip()
    if new_name and new_name != name:
        frappe.rename_doc("Assignment Group", name, new_name, force=True)
        name = new_name

    if lead is not None:
        doc = frappe.get_doc("Assignment Group", name)
        lead = (lead or "").strip()
        doc.lead = lead or None
        if lead and not any(row.member == lead for row in doc.members):
            doc.append("members", {"member": lead})
        doc.save(ignore_permissions=True)

    return name


@frappe.whitelist(methods=["POST"])
def delete_product(name):
    """Delete a product, naming the real reason when it cannot be.

    Frappe's own link check refuses the delete and reports whichever row it happened to
    hit first, which is how a product with no visible clients came back "linked with
    Client Amazon" — the blocker was the hidden legacy `Client.product` field. That field
    is gone, but the shape of the problem stays: the Products page can only see
    engagements, while the delete is governed by every Link field pointing at Product.

    So the check lives here, where all of them are visible, and says which one applies:

    - Tickets: permanent. Ticket history is meant to outlive the catalogue entry, so this
      is a refusal, not a "try again later" — the UI should not offer Delete at all.
    - Engagements: fixable. Naming the clients turns it into an instruction.
    """
    _require_manager()
    if not frappe.db.exists("Product", name):
        frappe.throw(_("{0} no longer exists.").format(name), title=_("Product not found"))

    tickets = frappe.db.count("Support Ticket", {"product": name})
    if tickets:
        frappe.throw(
            _(
                "{0} is on {1} ticket(s) and has to stay for that history to make sense. "
                "Rename it if it is no longer sold."
            ).format(name, tickets),
            title=_("Product is part of ticket history"),
        )

    engagements = frappe.get_all("Client Product", filters={"product": name}, fields=["client"])
    clients = sorted({e.client for e in engagements})
    if clients:
        frappe.throw(
            _("{0} is still run by {1}. Remove it from each before deleting.").format(
                name, ", ".join(clients)
            ),
            title=_("Product is still assigned"),
        )

    frappe.delete_doc("Product", name, ignore_permissions=True)
    return name


@frappe.whitelist(methods=["POST"])
def update_poc(name, poc_name=None, email=None, phone=None, divisions=None, is_primary=None, is_lead=None):
    """Edit a POC. POC is autonamed by `email`, so a changed email must rename the
    doc — and if the POC already has a portal login, the User is renamed too so the
    sign-in address stays in sync (that link is how me()/permissions scope them).

    NOT a single all-or-nothing transaction once a linked User exists: Frappe's
    ``User.after_rename`` calls ``clear_sessions``, which commits mid-flow (before
    rename_doc re-keys ``__Auth`` via rename_password). The User rename is therefore
    deliberately the LAST statement — nothing we could still roll back runs after that
    partial commit, so a later failure can't leave the User half-renamed."""
    _require_manager()
    doc = frappe.get_doc("POC", name)
    new_email = (email or "").strip()
    email_changed = bool(new_email) and new_email != doc.email
    # Renaming a User is destructive if the target address is already taken by a
    # different account — refuse up front with a clear message.
    if email_changed and doc.user and doc.user != new_email and frappe.db.exists("User", new_email):
        frappe.throw(_("Another user already signs in as {0} — the POC email must be unique").format(new_email))
    if poc_name is not None:
        doc.poc_name = poc_name
    if phone is not None:
        doc.phone = phone or None
    if is_primary is not None:
        doc.is_primary = cint(is_primary)
    if is_lead is not None:
        # Promoting on the way to an empty division set. Removing a contact's last division
        # strips every ticket they can see, so the UI offers "make them a client lead"
        # instead — which is this flag, and is the difference between a client-level contact
        # and one nobody has finished setting up.
        doc.is_lead = cint(is_lead)
    if divisions is not None:
        # Replace wholesale rather than merge: the caller sends the complete set, so a
        # division removed in the UI must actually lose access. Clearing the legacy single
        # column too, or POC.validate would silently re-append the old division.
        doc.division = None
        doc.set("divisions", [{"division": d} for d in _as_list(divisions) if d])
    if new_email:
        doc.email = new_email
    doc.save(ignore_permissions=True)
    if email_changed:
        linked_user = doc.user
        frappe.rename_doc("POC", name, new_email, force=True)
        name = new_email
        # Keep this LAST (see docstring). No follow-up writes needed: User.after_rename
        # already sets User.email = new, and rename_doc cascades the POC.user Link.
        if linked_user and linked_user != new_email and frappe.db.exists("User", linked_user):
            # The MODULE-level rename_doc, not frappe.rename_doc: the top-level wrapper
            # (frappe/__init__.py:804) has no ignore_permissions parameter and no **kwargs,
            # so passing it there is a hard TypeError — this whole branch used to die
            # before renaming the login. The bypass is needed on purpose: validate_rename
            # requires write on User, which a Support Manager without System Manager
            # doesn't have.
            rename_doc("User", linked_user, new_email, force=True, ignore_permissions=True)
    return name


@frappe.whitelist(methods=["POST"])
def create_contact(client, poc_name, email, phone=None, divisions=None, is_lead=0):
    """Create a client contact — a division POC or a client Lead, same record either way.

    `divisions` may be empty, and for a Lead usually is at creation: they are added during
    client onboarding, before any division exists. An empty set means no ticket access
    until someone assigns them, which is deliberate (see permissions._poc)."""
    _require_manager()
    doc = frappe.get_doc({
        "doctype": "POC",
        "client": client,
        "poc_name": poc_name,
        "email": (email or "").strip(),
        "phone": phone or None,
        "is_lead": cint(is_lead),
        "divisions": [{"division": d} for d in _as_list(divisions) if d],
    })
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist(methods=["POST"])
def set_contact_divisions(name, divisions):
    """Replace the divisions a contact can see. This IS the access-granting call — the set
    given here becomes their entire ticket scope, so anything omitted loses access."""
    _require_manager()
    doc = frappe.get_doc("POC", name)
    doc.division = None  # legacy column; POC.validate would otherwise re-append it
    doc.set("divisions", [{"division": d} for d in _as_list(divisions) if d])
    doc.save(ignore_permissions=True)
    return doc.name


# ---- client products (the "product" a client runs, per division or client-wide) ----
def _client_product_payload(client, product, dev_start, expected_completion, divisions):
    return {
        "client": client,
        "product": product,
        "dev_start": dev_start or None,
        "expected_completion": expected_completion or None,
        # Empty means "attached to the client as a whole" — the only shape available to a
        # client with no divisions, and a legitimate choice even when it has some.
        "divisions": [{"division": d} for d in _as_list(divisions) if d],
    }


@frappe.whitelist(methods=["POST"])
def create_client_product(client, product, dev_start=None, expected_completion=None, divisions=None):
    """Attach a product to a client, optionally scoped to specific divisions."""
    _require_manager()
    doc = frappe.get_doc({
        "doctype": "Client Product",
        **_client_product_payload(client, product, dev_start, expected_completion, divisions),
    })
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist(methods=["POST"])
def update_client_product(name, product=None, dev_start=None, expected_completion=None, divisions=None):
    _require_manager()
    doc = frappe.get_doc("Client Product", name)
    if product is not None:
        doc.product = product
    if dev_start is not None:
        doc.dev_start = dev_start or None
    if expected_completion is not None:
        doc.expected_completion = expected_completion or None
    if divisions is not None:
        doc.set("divisions", [{"division": d} for d in _as_list(divisions) if d])
    doc.save(ignore_permissions=True)
    return doc.name


@frappe.whitelist(methods=["POST"])
def delete_client_product(name):
    _require_manager()
    frappe.delete_doc("Client Product", name, ignore_permissions=True)
    return name


@frappe.whitelist(methods=["POST"])
def delete_poc(name):
    """Remove a POC. POC.on_trash disables the linked portal login (unless the same
    person still covers another division), so a removed contact can no longer sign in.
    The User is disabled, not deleted — it may own historical activity and Frappe blocks
    deleting a linked user."""
    _require_manager()
    frappe.delete_doc("POC", name, ignore_permissions=True)
    return name


# ---- login provisioning (shared by POC + Team Member invites) -------------
# Client vs staff identities must never mix on one login: a portal user (Support Client)
# that also holds a staff role would be treated as staff (_is_team) and see EVERY tenant's
# tickets — a tenant-isolation break. Provisioning refuses to cross an account over the line.
_CLIENT_ROLE = "Support Client"
_STAFF_ROLES = {"Support Team", "Support Manager", "System Manager", "Administrator"}
# Roles a directory invite must never hand over: provisioning only ever grants Support
# Team or Support Client, so an existing account holding one of these is out of scope for
# the invite flow entirely — re-using it would be a privilege escalation, not an invite.
_ELEVATED_ROLES = {"Support Manager", "System Manager", "Administrator"}


def _ensure_login_user(email, full_name, user_type, role, owner_doctype=None, owner_name=None):
    """Create or fetch the Frappe User for `email`, guarantee it holds `role` and is
    enabled, and return the saved doc. Re-using an existing account is deliberate — a
    resend must not mint a second login — EXCEPT where the account is not this record's
    to re-use:

    * across the client/staff line, so one identity can never straddle both sides of
      tenant isolation;
    * when another directory record already owns it (assert_user_unclaimed) — re-using it
      there means _send_invite_mail resets a DIFFERENT person's password and hands their
      account, and its roles, to the invitee;
    * when the account outranks what we are provisioning. `_require_manager` lets a
      Support Manager invite anyone, so without this a manager could type a System
      Manager's address, receive the set-password link, and take that account over.
      Directory invites only ever hand out Support Team / Support Client.

    The caller links the User to its record and sends the invite mail."""
    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
        existing_roles = {r.role for r in user.roles}
        provisioning_client = role == _CLIENT_ROLE
        if provisioning_client and (existing_roles & _STAFF_ROLES):
            frappe.throw(_("{0} already has a staff login, so it can't also be a client POC. Use a different email address.").format(email))
        if not provisioning_client and _CLIENT_ROLE in existing_roles:
            frappe.throw(_("{0} is already a client POC, so it can't also be given staff access. Use a different email address.").format(email))
        assert_user_unclaimed(user.name, owner_doctype, owner_name)
        if elevated := (existing_roles & _ELEVATED_ROLES):
            frappe.throw(
                _("{0} is an administrator account ({1}), so it can't be handed out as a directory invite. Use a different email address.").format(
                    email, ", ".join(sorted(elevated))
                )
            )
    else:
        first, _sep, last = (full_name or email).partition(" ")
        user = frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": first or email,
            "last_name": last or "",
            "user_type": user_type,
            "send_welcome_email": 0,  # we send the link explicitly below
        })
        user.insert(ignore_permissions=True)

    if role not in {r.role for r in user.roles}:
        user.append("roles", {"role": role})
    if not user.enabled:
        user.enabled = 1
    user.save(ignore_permissions=True)
    return user


def _action_email_html(user, link, heading, intro, cta):
    """Branded one-button email body. Indigo accent matches the app's design system.
    Shared by the invite and the password reset so both land on our own /set-password
    page and read as the same product."""
    name = frappe.utils.escape_html(user.first_name or user.email)
    return f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:520px;margin:0 auto;color:#1e2230;">
  <h2 style="font-size:20px;font-weight:700;margin:0 0 6px;">{heading}</h2>
  <p style="font-size:14px;line-height:1.6;color:#464b5c;margin:0 0 18px;">
    Hi {name}, {intro}
  </p>
  <p style="margin:0 0 22px;">
    <a href="{link}" style="display:inline-block;background:#4f46e5;color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:11px 20px;border-radius:9px;">{cta}</a>
  </p>
  <p style="font-size:12.5px;line-height:1.6;color:#6b7182;margin:0 0 6px;">Or paste this link into your browser:</p>
  <p style="font-size:12.5px;word-break:break-all;margin:0 0 22px;"><a href="{link}" style="color:#4f46e5;">{link}</a></p>
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    If you weren't expecting this, you can safely ignore this email.
  </p>
</div>""".strip()


def _invite_email_html(user, link):
    return _action_email_html(
        user, link,
        heading=_("Welcome to Inventive Helpdesk"),
        intro=_("an account has been created for you. Set a password to activate it and sign in."),
        cta=_("Set your password"),
    )


def _reset_email_html(user, link):
    return _action_email_html(
        user, link,
        heading=_("Reset your password"),
        intro=_("we received a request to reset your Inventive Helpdesk password. Choose a new one below — the link works once and expires."),
        cta=_("Choose a new password"),
    )


def _send_invite_mail(user, context):
    """Email a set-password link so the invitee can activate their account. Prefers a
    branded link into our own app (app_url + /set-password) so onboarding stays in the
    product and lands them in the right place by role; falls back to Frappe's built-in
    welcome mail if app_url isn't configured. Best-effort: a site with no outgoing mail
    account shouldn't fail the invite — the account still exists and can be signed in
    once mail is configured. Returns whether the mail was sent."""
    app_url = (frappe.conf.get("app_url") or "").rstrip("/")
    try:
        if app_url:
            # _reset_password() mints a one-time key (stored hashed) and returns a URL
            # holding the raw key; we retarget that key at our own /set-password page.
            key = user._reset_password().split("key=", 1)[1]
            frappe.sendmail(
                recipients=[user.email],
                subject=_("Set up your Inventive Helpdesk access"),
                message=_invite_email_html(user, f"{app_url}/set-password?key={key}"),
                now=True,
            )
        else:
            user.send_welcome_mail_to_user()
        return True
    except (frappe.OutgoingEmailError, frappe.ValidationError):
        frappe.log_error(title=f"{context} invite email failed")
        return False


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=get_password_reset_limit, seconds=60 * 60)
def request_password_reset(user):
    """Email a password-reset link that lands on OUR app, not the Frappe desk.

    frappe.core.doctype.user.user.reset_password builds its link with get_url(), which
    resolves to the backend host — so "Forgot password" mailed people a link to
    helpdeskfrappe…/update-password, Frappe's own desk page. Wrong product, wrong
    branding, and a portal client has no business on the desk at all. The invite flow
    already retargets the key at app_url + /set-password; this does the same for resets.

    Enumeration-safe, mirroring the upstream contract (CWE-204): the response is identical
    whether the address exists, is disabled, or is the Administrator, and every failure is
    swallowed into that same answer rather than surfacing as a distinguishable error.
    Rate-limited on the same budget as upstream, since this endpoint sends mail to an
    address chosen by an unauthenticated caller."""
    try:
        user_doc = frappe.get_doc("User", user)
        if user_doc.name != "Administrator" and user_doc.enabled:
            user_doc.validate_reset_password()
            app_url = (frappe.conf.get("app_url") or "").rstrip("/")
            if app_url:
                # Mints a fresh one-time key (stored hashed) and returns a URL holding the
                # raw key; we keep the key and drop Frappe's URL.
                key = user_doc._reset_password().split("key=", 1)[1]
                frappe.sendmail(
                    recipients=[user_doc.email],
                    subject=_("Reset your Inventive Helpdesk password"),
                    message=_reset_email_html(user_doc, f"{app_url}/set-password?key={key}"),
                    now=True,
                )
            else:
                # No app_url configured: better a desk link than no email at all.
                user_doc._reset_password(send_email=True)
    except frappe.DoesNotExistError:
        frappe.clear_messages()
    except frappe.OutgoingEmailError:
        frappe.clear_messages()
        frappe.log_error(title="Password reset email could not be sent", message=frappe.get_traceback())
    except Exception:
        frappe.clear_messages()
        frappe.log_error(title="Password reset failed unexpectedly", message=frappe.get_traceback())

    frappe.msgprint(
        msg=_("If this email is registered with us, we have sent password reset instructions to it. Please check your inbox."),
        title=_("Password Reset"),
    )


# ---- set-password links: lifetime, revocation, redemption -------------------
#
# Frappe mints ONE kind of key (User.reset_password_key, stored sha256-hashed, single-use)
# and gives it ONE lifetime, System Settings.reset_password_link_expiry_duration. That is a
# problem, because the two things we send it for want opposite windows:
#
#   * An INVITE is opened whenever the person next reads their mail. A window measured in
#     minutes means most invites are dead on arrival, and the account it activates holds
#     nothing yet — 24h is the normal, defensible choice.
#   * A RESET works against a live account with real data behind it. Every extra hour it
#     sits in a mailbox is another hour a compromised or shared inbox is a way in. An hour
#     is generous.
#
# So the global is set wide enough for the invite and the tighter reset window is enforced
# here. Which kind a key is comes from `User.last_password_reset_date`: an account that has
# never had a password set is being invited, one that has is resetting. No new field, no
# cache entry that a Redis restart would turn into a wall of dead invite links, and the
# answer self-corrects — the moment someone sets a password, their next key is a reset.
INVITE_LINK_TTL_HOURS = 24
RESET_LINK_TTL_HOURS = 1

# What the client is told about a key. Deliberately coarse: never the address it belongs to,
# never whether an account exists at that address.
LINK_VALID = "valid"
LINK_EXPIRED = "expired"
LINK_REVOKED = "revoked"
LINK_INVALID = "invalid"


def _resolve_password_key(key):
    """(user_doc, status) for a raw key. Never consumes it.

    NOTE for callers: unpack the first value into a NAMED variable, never `_`. In this
    module `_` is frappe's gettext alias, so `_, status = ...` rebinds the translator to a
    User document and the next `_("…")` raises `'User' object is not callable`. It cost a
    red release pipeline to learn.

    Not consuming matters more than it looks: corporate mail security (Outlook Safe Links,
    Defender ATP) fetches every URL in a message before the human sees it. If arriving at
    the page burned the key, scanned invites would be dead by the time they were opened,
    and the failure would look like the invite system being broken.
    """
    # frappe.utils re-exports this via `from frappe.utils.data import *`, but Frappe's own
    # code imports it from data directly — the star re-export is an implementation detail,
    # and an `__all__` added upstream would silently take it away.
    from frappe.utils.data import get_datetime, sha256_hash

    key = (key or "").strip()
    if not key:
        return None, LINK_INVALID

    name = frappe.db.get_value("User", {"reset_password_key": sha256_hash(key)}, "name")
    if not name:
        # Used, cleared, or never real. One answer for all three — distinguishing them
        # would say whether a key had ever existed.
        return None, LINK_INVALID

    user = frappe.get_doc("User", name)

    # THE check this whole section exists for. Frappe's update_password never looks at
    # `enabled` and calls login_as() on success, so a still-valid key on a disabled account
    # is a working way back in. Testing it here, at redemption, rather than clearing keys
    # at each of the several places that can disable someone, is what makes that safe: a
    # new disable path added later is covered without knowing this code exists.
    if not user.enabled:
        return user, LINK_REVOKED

    issued = user.last_reset_password_key_generated_on
    if not issued:
        # A key with no issue time cannot be aged, so it cannot be trusted. Fail closed.
        return user, LINK_EXPIRED

    hours = RESET_LINK_TTL_HOURS if user.last_password_reset_date else INVITE_LINK_TTL_HOURS
    # get_datetime rather than using the value as-is: the field comes back as a datetime
    # through the ORM but as a string from some paths, and comparing a str to a datetime
    # raises rather than returning a wrong answer — which would take the page down instead
    # of expiring a link.
    if now_datetime() > get_datetime(issued) + timedelta(hours=hours):
        return user, LINK_EXPIRED

    return user, LINK_VALID


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=30, seconds=60 * 60)
def password_link_status(key):
    """Is this set-password link still good? Checked when the page loads, before the form.

    Without this, /set-password renders the whole form, the person chooses a password, types
    it twice, submits — and only then learns the link died. The answer is knowable on
    arrival, so it should be given on arrival.

    Returns a coarse status and nothing else: no email address, no name, no indication that
    an account exists at any particular address. `expired` and `invalid` are distinguished
    because that is the difference between "ask for a new one" and "check you copied the
    whole link", and because guessing a key is not a threat worth trading that away for —
    they are high-entropy and single-use. Rate-limited regardless, since this is reachable
    without signing in.
    """
    _user, status = _resolve_password_key(key)
    return {"status": status, "support_inbox": _support_inbox_address()}


def _support_inbox_address():
    """The address to tell someone to write to when their link is dead. Shared with the
    email module's sender resolution so the two cannot drift apart."""
    from inventive_helpdesk_backend.email import _support_inbox

    return _support_inbox()


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def set_password_with_key(key, new_password):
    """Redeem a set-password link. Wraps Frappe's update_password with our own gate.

    The gate has to be here and not only in the UI, because the UI is not a security
    boundary: the page's pre-flight check is a courtesy to the person, and this is the thing
    that actually decides. Both call _resolve_password_key, so they cannot disagree.

    Delegates the rest — password strength, the hashing, session creation, clearing the key —
    rather than reimplementing it. update_password signals failure by setting a 410 and
    RETURNING a message string instead of raising, so that has to be turned back into a
    throw or a caller would read "The reset password link has expired" as success.
    """
    from frappe.core.doctype.user.user import update_password

    user, status = _resolve_password_key(key)
    if status != LINK_VALID:
        # PermissionError, and no hand-set status code. 410 Gone would describe an expired
        # link better, but frappe.throw derives the response code from the exception type
        # and overwrites anything set beforehand — writing 410 here would look deliberate
        # and do nothing. The message carries the distinction; the page has already asked
        # password_link_status and shown the specific wording before reaching this.
        frappe.throw(
            {
                LINK_EXPIRED: _("This link has expired. Ask your administrator for a new invite."),
                LINK_REVOKED: _("This account no longer has access. Please contact your administrator."),
            }.get(status, _("This link has already been used or is not valid.")),
            frappe.PermissionError,
        )

    result = update_password(new_password=new_password, key=key)
    # update_password returns a STRING in BOTH outcomes, so the return type says nothing:
    #   refusal -> sets response.http_status_code = 410 and returns the message
    #   success -> returns a post-login redirect path ("/desk", the portal home, ...)
    # The status code is the only discriminator. Testing `isinstance(result, str)` instead
    # threw frappe.PermissionError on every SUCCESSFUL activation, and because app.py
    # rolls the request back on any exception, everything update_password wrote after its
    # own mid-request commit was discarded — most importantly reset_password_key, which
    # stayed populated and left the invite link redeemable again and again. The person
    # meanwhile saw the raw redirect path, or "Logged In", in the form's error slot.
    if frappe.local.response.http_status_code == 410:
        frappe.throw(result, frappe.PermissionError)
    # .name, not the doc — _resolve_password_key hands back a User document.
    _mark_activated(user.name)
    return {"ok": True}


def _mark_activated(user: str) -> None:
    """Record that this login has finished activation by choosing a password.

    Deliberately NOT left to the on_login hook. Activation and signing in are different
    facts, and the admin-facing chip is meant to answer the first one: "has this person
    picked a password yet, or is that invite still outstanding?" It only ever looked right
    by accident — update_password calls login_as internally, so on_login fired during
    activation and Frappe's session-creation commit happened to persist it. Nothing in the
    flow intended that, and any change to how activation signs someone in would have
    silently taken the chip with it.

    Both shapes are stamped because a login can be either, and never both: a Team Member
    is staff, a POC is a client contact.
    """
    frappe.db.set_value("Team Member", {"user": user}, "status", "Active", update_modified=False)
    for poc in frappe.get_all("POC", filters={"user": user}, pluck="name"):
        frappe.db.set_value("POC", poc, "activated_on", now_datetime(), update_modified=False)


@frappe.whitelist(methods=["POST"])
@rate_limited("invite")
def invite_poc(poc):
    """Provision (or re-notify) a POC's portal login. Creates a Website User with the
    Support Client role, links it back via POC.user, and emails a set-password / sign-in
    link. Idempotent: safe to call again to resend. Email delivery is best-effort so a
    dev site with no outgoing mail account still creates the account."""
    _require_manager()
    doc = frappe.get_doc("POC", poc)
    email = (doc.email or "").strip()
    if not email:
        frappe.throw(_("This POC has no email address to invite"))

    user = _ensure_login_user(email, doc.poc_name, "Website User", "Support Client", "POC", doc.name)

    # Link the account, (re)stamp the invite time, and drop any previous activation.
    # Clearing activated_on is what gives Resend its meaning: the new link has to be
    # redeemed before this contact reads Active again, so a resend is a real "prove
    # yourself again" rather than a mail that changes nothing on screen. It also keeps a
    # re-used pre-existing User honest — inheriting someone's earlier activation would
    # show Active for an invite nobody has answered.
    doc.user = user.name
    doc.invited_on = now_datetime()
    doc.activated_on = None
    doc.save(ignore_permissions=True)

    # Their existing tickets were classified "Known Contact" — no login, so email-only.
    # Granting the login changes that answer without touching the tickets themselves, so
    # the cached column has to be refreshed here or it stays wrong until each ticket is
    # next saved. Bounded by one contact's tickets.
    from inventive_helpdesk_backend import sender

    sender.refresh_for_poc(doc.name)

    return {"user": user.name, "email_sent": _send_invite_mail(user, "POC portal")}


@frappe.whitelist(methods=["POST"])
@rate_limited("invite")
def invite_member(member):
    """Provision (or re-notify) a team member's staff login. Creates a System User with
    the Support Team role, links it via Team Member.user, marks the member Invited and
    emails a set-password link. The member flips to Active when they redeem that link and
    choose a password (see _mark_activated). Idempotent: safe to call to resend."""
    _require_manager()
    doc = frappe.get_doc("Team Member", member)
    email = (doc.email or "").strip()
    if not email:
        frappe.throw(_("This member has no email address to invite"))

    user = _ensure_login_user(email, doc.member_name, "System User", "Support Team", "Team Member", doc.name)

    # Link the account and reset the member to Invited. Same rule as a POC resend: the new
    # link has to be redeemed before they read Active again. Nothing promotes them on
    # sign-in any more, so a re-used account with an old password cannot climb back.
    doc.user = user.name
    doc.status = "Invited"
    doc.save(ignore_permissions=True)

    return {"user": user.name, "email_sent": _send_invite_mail(user, "Team member")}
