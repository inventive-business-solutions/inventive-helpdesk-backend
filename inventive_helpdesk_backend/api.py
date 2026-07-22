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

import frappe
from frappe import _
from frappe.model.rename_doc import rename_doc
from frappe.sessions import get_csrf_token
from frappe.utils import cint, now_datetime

from inventive_helpdesk_backend.permissions import MANAGER_ROLES, TEAM_ROLES


def _is_team(user: str | None = None) -> bool:
    return bool(set(frappe.get_roles(user or frappe.session.user)) & TEAM_ROLES)


def _is_manager(user: str | None = None) -> bool:
    return bool(set(frappe.get_roles(user or frappe.session.user)) & MANAGER_ROLES)


def _require_team():
    if not _is_team():
        frappe.throw(_("Only support staff can perform this action"), frappe.PermissionError)


def _require_manager():
    if not _is_manager():
        frappe.throw(_("Only a support manager can manage clients, members and teams"), frappe.PermissionError)


def _author() -> str:
    return frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user


def _norm_attachments(attachments) -> str:
    if attachments is None:
        return "[]"
    if isinstance(attachments, str):
        return attachments
    return json.dumps(attachments)


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
    poc = frappe.db.get_value("POC", {"user": user}, ["client", "division"], as_dict=True)
    if poc:
        d = frappe.db.get_value("Division", poc.division, ["division_name", "division_code"], as_dict=True) or {}
        ctx.update({
            "client": poc.client,
            "division": poc.division,
            "division_name": d.get("division_name") or poc.division,
            "division_code": d.get("division_code") or "",
        })
    return ctx


@frappe.whitelist(methods=["POST"])
def add_message(ticket: str, body: str, attachments=None):
    """Append a client-visible message. Allowed for the ticket's client POC or staff."""
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
    # A staff member's client-visible reply → email it to the client with a portal link so
    # email-only clients see it and can jump back in to continue the conversation.
    if team:
        from inventive_helpdesk_backend.email import notify_client_reply
        notify_client_reply(doc, body)
    return doc.name


@frappe.whitelist(methods=["POST"])
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


@frappe.whitelist()
def unread_tickets():
    """Ticket names with activity this agent hasn't seen. Staff only.

    Read scope still applies: this only reports names, and the ticket bodies come from the
    normal permission-checked list fetch, so a name here can never expose a ticket the
    caller couldn't already read."""
    _require_team()
    user = frappe.session.user
    rows = frappe.get_all(
        "Support Ticket",
        filters={"last_activity_on": ["is", "set"]},
        fields=["name", "last_activity_on"],
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
def update_client(name, client_name=None, client_code=None, since=None, product=None):
    """Edit a client, including a rename. `name` (autonamed from client_name) is a
    Link target on Support Ticket, Division and POC, so frappe.rename_doc cascades
    the new name to every reference. `product` is a Product docname ("" clears it)."""
    _require_manager()
    doc = frappe.get_doc("Client", name)
    if client_code is not None:
        doc.client_code = client_code
    if since is not None:
        doc.since = since or None
    if product is not None:
        doc.product = product or None
    doc.save(ignore_permissions=True)
    new_name = (client_name or "").strip()
    if new_name and new_name != name:
        frappe.rename_doc("Client", name, new_name, force=True)
        name = new_name
    return name


@frappe.whitelist(methods=["POST"])
def update_product(name, product_name=None):
    """Rename a product. Product is autonamed by product_name and is a Link target on
    Client.product, so rename_doc cascades the new name to every client running it."""
    _require_manager()
    new_name = (product_name or "").strip()
    if new_name and new_name != name:
        frappe.rename_doc("Product", name, new_name, force=True)
        name = new_name
    return name


@frappe.whitelist(methods=["POST"])
def update_poc(name, poc_name=None, email=None, is_primary=None):
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
    if is_primary is not None:
        doc.is_primary = cint(is_primary)
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


def _ensure_login_user(email, full_name, user_type, role):
    """Create or fetch the Frappe User for `email`, guarantee it holds `role` and is
    enabled, and return the saved doc. Re-using an existing account is deliberate — the
    same person may already sign in (e.g. a POC for two divisions) — EXCEPT across the
    client/staff line: we refuse to give a client POC a staff login or vice versa, so one
    identity can never straddle both sides of tenant isolation. The caller links the User
    to its record and sends the invite mail."""
    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
        existing_roles = {r.role for r in user.roles}
        provisioning_client = role == _CLIENT_ROLE
        if provisioning_client and (existing_roles & _STAFF_ROLES):
            frappe.throw(_("{0} already has a staff login, so it can't also be a client POC. Use a different email address.").format(email))
        if not provisioning_client and _CLIENT_ROLE in existing_roles:
            frappe.throw(_("{0} is already a client POC, so it can't also be given staff access. Use a different email address.").format(email))
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


def _invite_email_html(user, link):
    """Branded set-password email body. Indigo accent matches the app's design system."""
    name = frappe.utils.escape_html(user.first_name or user.email)
    return f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:520px;margin:0 auto;color:#1e2230;">
  <h2 style="font-size:20px;font-weight:700;margin:0 0 6px;">Welcome to Inventive Helpdesk</h2>
  <p style="font-size:14px;line-height:1.6;color:#464b5c;margin:0 0 18px;">
    Hi {name}, an account has been created for you. Set a password to activate it and sign in.
  </p>
  <p style="margin:0 0 22px;">
    <a href="{link}" style="display:inline-block;background:#4f46e5;color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:11px 20px;border-radius:9px;">Set your password</a>
  </p>
  <p style="font-size:12.5px;line-height:1.6;color:#6b7182;margin:0 0 6px;">Or paste this link into your browser:</p>
  <p style="font-size:12.5px;word-break:break-all;margin:0 0 22px;"><a href="{link}" style="color:#4f46e5;">{link}</a></p>
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    If you weren't expecting this, you can safely ignore this email.
  </p>
</div>""".strip()


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


@frappe.whitelist(methods=["POST"])
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

    user = _ensure_login_user(email, doc.poc_name, "Website User", "Support Client")

    # Link the account and (re)stamp the invite time. invited_on is the reference the
    # UI compares last_login against: a sign-in *after* this moment marks the POC Active.
    # Restamping on every (re)send resets that clock, so a stale Resend can't read Active
    # off a login that predates it — and a re-used pre-existing User (whose last_login may
    # already be set) correctly stays Invited until they sign in for this invite.
    doc.user = user.name
    doc.invited_on = now_datetime()
    doc.save(ignore_permissions=True)

    # Their existing tickets were classified "Known Contact" — no login, so email-only.
    # Granting the login changes that answer without touching the tickets themselves, so
    # the cached column has to be refreshed here or it stays wrong until each ticket is
    # next saved. Bounded by one contact's tickets.
    from inventive_helpdesk_backend import sender

    sender.refresh_for_poc(doc.name)

    return {"user": user.name, "email_sent": _send_invite_mail(user, "POC portal")}


@frappe.whitelist(methods=["POST"])
def invite_member(member):
    """Provision (or re-notify) a team member's staff login. Creates a System User with
    the Support Team role, links it via Team Member.user, marks the member Invited and
    emails a set-password link. The member flips to Active automatically the first time
    they sign in (see activate_member_on_login). Idempotent: safe to call to resend."""
    _require_manager()
    doc = frappe.get_doc("Team Member", member)
    email = (doc.email or "").strip()
    if not email:
        frappe.throw(_("This member has no email address to invite"))

    user = _ensure_login_user(email, doc.member_name, "System User", "Support Team")

    # Link the account and reset the member to Invited. There is no timestamp compare
    # here (unlike POCs): activation is event-driven via the on_login hook, which only
    # fires on a real sign-in *after* this — so a re-used account with an old last_login
    # correctly stays Invited until they actually log in again for this invite.
    doc.user = user.name
    doc.status = "Invited"
    doc.save(ignore_permissions=True)

    return {"user": user.name, "email_sent": _send_invite_mail(user, "Team member")}


def activate_member_on_login(login_manager):
    """on_login hook: the moment an invited team member actually signs in, mark them
    Active — that is how staff onboarding completes. Runs on every login; it's a cheap
    no-op for the Administrator, portal/POC users, and already-active members. (POC
    portal activation is derived separately from last_login vs POC.invited_on.)"""
    user = getattr(login_manager, "user", None) or frappe.session.user
    if not user or user in ("Guest", "Administrator"):
        return
    frappe.db.set_value("Team Member", {"user": user, "status": "Invited"}, "status", "Active")
