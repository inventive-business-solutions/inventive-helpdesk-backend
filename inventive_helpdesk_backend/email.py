"""Inbound-email intake + outbound client notifications for Inventive Helpdesk.

Inbound (two transports, one intake):
- Production: a real incoming Email Account (IMAP/POP3) creates a Communication per
  received email; ``on_communication`` opens a ticket from it.
- Local dev: Mailpit POSTs a webhook per captured message; ``receive_webhook`` opens one.
Both ignore our own outgoing mail (From = the support inbox), so there's no feedback loop.

Outbound (to the client):
- ``send_ticket_ack`` (Support Ticket after_insert) — acknowledges every CLIENT-initiated
  ticket (emailed in or raised in the portal) with its ID; agent-logged tickets are skipped.
- ``notify_client_reply`` (from api.add_message) — emails the client a staff member's
  client-visible reply, with a link back to the portal to continue the conversation.

All outbound mail is QUEUED (now=False + retry) so a busy mail server can't drop it, and is
addressed to the client (never the support inbox), so it never loops back into a new ticket.
"""
import json

import frappe
from frappe.utils import parse_addr, strip_html

from inventive_helpdesk_backend.permissions import TEAM_ROLES


# ---- addressing -----------------------------------------------------------
def _support_inbox():
    """Address clients email to raise tickets. Configurable via site_config
    ``support_inbox``; defaults to the default-outgoing account so a fresh site works."""
    return (
        frappe.conf.get("support_inbox")
        or frappe.db.get_value("Email Account", {"default_outgoing": 1}, "email_id")
        or ""
    ).strip().lower()


def _ticket_contact_email(ticket):
    """The client's email for acks/replies. Email tickets carry ``from_email``; portal
    tickets are owned by the raising POC (their login IS their email); agent-raised tickets
    fall back to the POC named on the ticket, then the division's primary POC."""
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


def _portal_ticket_url(ticket_name):
    """Deep link to a ticket in the client portal (empty if app_url isn't configured)."""
    app_url = (frappe.conf.get("app_url") or "").rstrip("/")
    return f"{app_url}/portal/tickets/{ticket_name}" if app_url else ""


def _queue_mail(recipient, subject, html, context):
    """Queue a client email (now=False + retry) so a transient failure can't drop it and it
    can never roll back the ticket action that triggered it. Skips our own support address."""
    recipient = (recipient or "").strip().lower()
    if not recipient or recipient == _support_inbox():
        return
    try:
        frappe.sendmail(recipients=[recipient], subject=subject, message=html, now=False, retry=3)
    except (frappe.OutgoingEmailError, frappe.ValidationError):
        frappe.log_error(title=f"{context} email failed")


# ---- inbound: email → ticket ----------------------------------------------
def _open_ticket_from_email(sender, subject, body):
    """Create a Support Ticket from an inbound email. Returns the ticket name, or None when
    the message is our own outgoing mail (which must never loop into a ticket). The
    acknowledgement is sent by the Support Ticket after_insert hook, not here."""
    sender = (parse_addr(sender or "")[1] or sender or "").strip().lower()
    if not sender or sender == _support_inbox():
        return None
    doc = frappe.get_doc({
        "doctype": "Support Ticket",
        "title": (subject or "").strip()[:140] or "(no subject)",
        "description": (strip_html(body or "").strip() or "—")[:100000],
        "status": "New",
        "from_email": sender,  # before_insert matches the POC + scopes the ticket
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _backfill_description(ticket, content):
    if (frappe.db.get_value("Support Ticket", ticket, "description") or "").strip():
        return
    body = strip_html(content or "").strip()
    if body:
        frappe.db.set_value("Support Ticket", ticket, "description", body[:100000], update_modified=False)


def on_communication(doc, method=None):
    """Real incoming Email Account path. An inbound email becomes a ticket; if Frappe
    already linked it to a ticket (Email Account 'Append To'), just backfill the body."""
    if doc.sent_or_received != "Received":
        return
    if doc.reference_doctype == "Support Ticket" and doc.reference_name:
        _backfill_description(doc.reference_name, doc.content)
        return
    if doc.reference_doctype:
        return  # linked to some other doctype — not ours
    name = _open_ticket_from_email(doc.sender, doc.subject, doc.content)
    if name:
        doc.db_set("reference_doctype", "Support Ticket", update_modified=False)
        doc.db_set("reference_name", name, update_modified=False)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_webhook():
    """Local dev intake: Mailpit POSTs here for every captured message. Turn inbound support
    emails (addressed to the support inbox, not from us) into tickets. Guest-callable because
    Mailpit is a local, unauthenticated sink — a DEV convenience; production uses a real Email
    Account + on_communication, not this endpoint."""
    # Guest-callable and DEV-ONLY: Mailpit is a local, unauthenticated sink. Outside
    # developer mode this must be inert — production intake is a real Email Account +
    # on_communication. Left open it lets an unauthenticated caller inject a ticket into
    # any client's scope (before_insert scopes by the attacker-supplied From address).
    if not frappe.conf.get("developer_mode"):
        raise frappe.PermissionError("receive_webhook is available only in developer mode")
    raw = frappe.request.get_data(as_text=True) if frappe.request else "{}"
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {"created": []}
    msgs = data if isinstance(data, list) else [data]
    inbox = _support_inbox()
    created = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        recipients = [(a.get("Address") or "").lower() for a in (m.get("To") or []) + (m.get("Cc") or [])]
        if inbox and inbox not in recipients:
            continue  # not addressed to our support inbox — ignore
        sender = ((m.get("From") or {}).get("Address") or "").strip()
        body = _fetch_mailpit_body(m.get("ID")) or m.get("Snippet") or ""
        name = _open_ticket_from_email(sender, m.get("Subject"), body)
        if name:
            created.append(name)
    if created:
        frappe.db.commit()
    return {"created": created}


def _fetch_mailpit_body(message_id):
    """Full plain-text body of a captured Mailpit message (the webhook payload only carries a
    snippet). Best-effort; falls back to the snippet on any error."""
    if not message_id:
        return ""
    try:
        import requests
        base = (frappe.conf.get("mailpit_api") or "http://localhost:8025").rstrip("/")
        r = requests.get(f"{base}/api/v1/message/{message_id}", timeout=5)
        if r.ok:
            return (r.json() or {}).get("Text") or ""
    except Exception:
        frappe.log_error(title="Mailpit body fetch failed")
    return ""


@frappe.whitelist(methods=["POST"])
def send_test_email(from_email, subject=None, body=None, from_name=None):
    """DEV-ONLY, staff-only: inject a test email into Mailpit via its send API so it flows
    through the REAL inbound pipeline (Mailpit captures it -> fires the webhook ->
    receive_webhook -> ticket). Lets the team watch a ticket arrive from an actual email,
    unlike the direct in-app simulator. Not for production (there is no Mailpit there)."""
    if not frappe.conf.get("developer_mode"):
        raise frappe.PermissionError("send_test_email is available only in developer mode")
    if not (set(frappe.get_roles()) & TEAM_ROLES):
        raise frappe.PermissionError("Only support staff can send a test email")
    sender = (from_email or "").strip()
    if not sender:
        frappe.throw("A sender email address is required")
    to = _support_inbox() or "helpdesk@inventivebizsol.com"
    base = (frappe.conf.get("mailpit_api") or "http://localhost:8025").rstrip("/")
    payload = {
        "From": {"Email": sender, "Name": (from_name or "").strip() or sender},
        "To": [{"Email": to}],
        "Subject": (subject or "").strip() or "(no subject)",
        "Text": (body or "").strip() or "(no message)",
    }
    import requests

    try:
        resp = requests.post(f"{base}/api/v1/send", json=payload, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        frappe.throw(f"Could not reach Mailpit at {base} — is it running? ({exc})")
    return {"to": to, "sender": sender}


# ---- outbound: acknowledgement + reply notifications ----------------------
def send_ticket_ack(doc, method=None):
    """Support Ticket after_insert: acknowledge every CLIENT-initiated ticket (emailed in or
    raised in the portal) to the client with its ID. Agent-logged tickets and bulk/seed/
    migrate inserts are skipped."""
    if (
        frappe.flags.in_install
        or frappe.flags.in_migrate
        or frappe.flags.in_import
        or frappe.in_test
        or frappe.flags.get("skip_ticket_ack")
    ):
        return
    client_initiated = bool(getattr(doc, "from_email", None)) or (
        doc.owner not in ("Administrator", "Guest") and frappe.db.exists("POC", {"user": doc.owner})
    )
    if not client_initiated:
        return
    recipient = _ticket_contact_email(doc)
    if recipient:
        subject = f"[{doc.name}] " + ((doc.title or "").strip() or "We've received your request")
        _queue_mail(recipient, subject, _ack_email_html(doc.name, doc.title), "Ticket acknowledgement")


def notify_client_reply(ticket, body):
    """Email the client a staff member's client-visible reply, with a portal link to continue.
    Called from api.add_message when a team member posts to the conversation."""
    recipient = _ticket_contact_email(ticket)
    if not recipient:
        return
    subject = f"[{ticket.name}] " + ((ticket.title or "").strip() or "Update on your request")
    _queue_mail(recipient, subject, _reply_email_html(ticket.name, body, _portal_ticket_url(ticket.name)), "Ticket reply")


# Status changes worth emailing the client about (heading, body message).
_STATUS_NOTIFY = {
    "Resolved": (
        "Your request has been resolved",
        "We've marked this ticket as resolved. If it didn't fully solve things, just reopen it by "
        "replying in the portal — we're happy to keep helping.",
    ),
    "Pending Client": (
        "We need a little more from you",
        "This ticket is waiting on your input. Please reply in the portal so we can move it forward.",
    ),
}


def on_ticket_update(doc, method=None):
    """Support Ticket on_update: when an agent moves a ticket to a client-facing status
    (Resolved / Pending Client), email the client. Fires only on an actual status change,
    so replies, notes and other edits don't trigger it."""
    if (
        frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_import
        or frappe.in_test or frappe.flags.get("skip_ticket_ack")
    ):
        return
    before = doc.get_doc_before_save()
    if not before or before.status == doc.status or doc.status not in _STATUS_NOTIFY:
        return
    recipient = _ticket_contact_email(doc)
    if not recipient:
        return
    heading, message = _STATUS_NOTIFY[doc.status]
    subject = f"[{doc.name}] " + ((doc.title or "").strip() or heading)
    _queue_mail(
        recipient,
        subject,
        _status_email_html(doc.name, heading, message, doc.status, _portal_ticket_url(doc.name)),
        f"Ticket {doc.status} notification",
    )


# ---- email bodies (branded, indigo accent) --------------------------------
def _portal_button(url, label):
    if not url:
        return ""
    return (
        f'<p style="margin:0 0 22px;"><a href="{url}" style="display:inline-block;background:#4f46e5;'
        f'color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:11px 20px;border-radius:9px;">'
        f'{label}</a></p>'
    )


def _shell(inner):
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:520px;margin:0 auto;color:#1e2230;">' + inner + "</div>"
    )


def _ack_email_html(ticket_name, subject_line):
    subj = frappe.utils.escape_html((subject_line or "").strip() or "Your request")
    return _shell(f"""
  <h2 style="font-size:20px;font-weight:700;margin:0 0 6px;">We&#39;ve got your request</h2>
  <p style="font-size:14px;line-height:1.6;color:#464b5c;margin:0 0 18px;">
    Thanks for reaching out to Inventive Helpdesk. Your message has been logged and our support team is on it —
    we&#39;ll follow up with you at this email address.
  </p>
  <div style="background:#f4f5fb;border:1px solid #e6e8f2;border-radius:10px;padding:14px 16px;margin:0 0 18px;">
    <div style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#8a90a2;font-weight:700;">Your ticket</div>
    <div style="font-size:18px;font-weight:700;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#4f46e5;margin-top:4px;">{ticket_name}</div>
    <div style="font-size:13px;color:#464b5c;margin-top:6px;">{subj}</div>
  </div>
  {_portal_button(_portal_ticket_url(ticket_name), "View your ticket")}
  <p style="font-size:12.5px;line-height:1.6;color:#6b7182;margin:0 0 18px;">
    Please quote <b>{ticket_name}</b> in any follow-up so we can find your request quickly.
  </p>
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    Inventive Helpdesk · After-sales support
  </p>""".strip())


def _reply_email_html(ticket_name, body, portal_url):
    reply = frappe.utils.escape_html((body or "").strip()).replace("\n", "<br>") or "(no message)"
    return _shell(f"""
  <h2 style="font-size:20px;font-weight:700;margin:0 0 6px;">New reply on {ticket_name}</h2>
  <p style="font-size:14px;line-height:1.6;color:#464b5c;margin:0 0 14px;">Our support team has replied to your request:</p>
  <div style="background:#f4f5fb;border-left:3px solid #4f46e5;border-radius:6px;padding:12px 16px;margin:0 0 18px;font-size:14px;line-height:1.6;color:#1e2230;">
    {reply}
  </div>
  <p style="font-size:13px;line-height:1.6;color:#464b5c;margin:0 0 16px;">
    You can add more details or continue the conversation in your portal:
  </p>
  {_portal_button(portal_url, "Reply in the portal")}
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    Ticket {ticket_name} · Inventive Helpdesk
  </p>""".strip())


def _status_email_html(ticket_name, heading, message, status, portal_url):
    badge = "#16a34a" if status == "Resolved" else "#d97706"  # green resolved / amber pending
    label = "Reply in the portal" if status == "Pending Client" else "View your ticket"
    return _shell(f"""
  <h2 style="font-size:20px;font-weight:700;margin:0 0 6px;">{heading}</h2>
  <p style="font-size:14px;line-height:1.6;color:#464b5c;margin:0 0 16px;">{frappe.utils.escape_html(message)}</p>
  <div style="margin:0 0 18px;">
    <span style="display:inline-block;font-size:12px;font-weight:700;color:#fff;background:{badge};padding:4px 12px;border-radius:20px;">{status}</span>
    <span style="font-size:13px;color:#6b7182;margin-left:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{ticket_name}</span>
  </div>
  {_portal_button(portal_url, label)}
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    Inventive Helpdesk · After-sales support
  </p>""".strip())
