"""Inbound-email intake + outbound client notifications for Inventive Helpdesk.

Inbound: a real incoming Email Account (IMAP) creates a Communication per received email,
and ``on_communication`` either appends it to the ticket it belongs to or opens a new one.
Our own outgoing mail (From = the support inbox) is ignored, so there's no feedback loop.

Outbound (to the client):
- ``send_ticket_ack`` (Support Ticket after_insert) — acknowledges every CLIENT-initiated
  ticket (emailed in or raised in the portal) with its ID; agent-logged tickets are skipped.
- ``notify_client_reply`` (from api.add_message) — emails the client a staff member's
  client-visible reply, with a link back to the portal to continue the conversation.

All outbound mail is QUEUED (now=False + retry) so a busy mail server can't drop it, and is
addressed to the client (never the support inbox), so it never loops back into a new ticket.
"""
import json
import re

import frappe
from frappe.email.email_body import get_message_id
from frappe.utils import now_datetime, parse_addr, strip_html
from frappe.utils.html_utils import unescape_html

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
    """The client's email for acks/replies.

    One implementation, in sender.py. This was a second copy of the same fallback chain,
    which is a drift hazard for nothing: the address transport sends to and the address
    classification is computed from MUST be the same, or a ticket could be badged
    "Registered" while mail went somewhere else entirely.
    """
    from inventive_helpdesk_backend import sender

    return sender.reply_address(ticket)


def _portal_ticket_url(ticket_name):
    """Deep link to a ticket in the client portal (empty if app_url isn't configured)."""
    app_url = (frappe.conf.get("app_url") or "").rstrip("/")
    return f"{app_url}/portal/tickets/{ticket_name}" if app_url else ""


def _queue_mail(recipient, subject, html, context, ticket=None, log_kind=None):
    """Queue a client email (now=False + retry) so a transient failure can't drop it and it
    can never roll back the ticket action that triggered it. Skips our own support address.

    `ticket` is what makes replies thread. Frappe stamps the site into every outgoing
    Message-ID, and on the way back InboundMail.reference_document() finds the Email Queue
    row whose message_id matches the reply's In-Reply-To header and reuses ITS
    reference_doctype/reference_name (frappe/email/receive.py:797, :838-867). Without a
    reference on the way out there is nothing to match, and every client reply opens a
    duplicate ticket — which is exactly what happened before this argument existed.

    That Email Queue row is not a durable anchor, though: frappe purges Email Queue after
    30 days (frappe/hooks.py:508), so `_anchor_outgoing` below writes a second, permanent
    one. See its docstring.

    Message-ID threading is used rather than the doctype's email_append_to subject match,
    because it survives a customer editing the subject line and keeps ticket creation in
    _open_ticket_from_email rather than handing it to Frappe.
    """
    recipient = (recipient or "").strip().lower()
    if not recipient or recipient == _support_inbox():
        return
    # Generate the id here rather than letting frappe mint one, so the same value can be
    # written to the durable anchor below. Bare, with no angle brackets: set_message_id
    # adds them on the way out (email_body.py:337) and the inbound side strips them again
    # (receive.py:419), and the anchor has to match the stripped form.
    message_id = get_message_id().strip("<>")
    try:
        queued = frappe.sendmail(
            recipients=[recipient],
            subject=subject,
            message=html,
            message_id=message_id,
            reference_doctype="Support Ticket" if ticket else None,
            reference_name=ticket or None,
            # Ask the recipient's mail system not to answer this with an out-of-office.
            # That is loop protection at the source: an OOO to a support inbox is what
            # starts inbound -> ticket -> ack -> OOO -> ticket. Exchange/M365 honours this
            # header, which covers most correspondents here.
            #
            # It has to be an X- header: frappe's add_headers() force-prefixes "X-" onto
            # any key that lacks it (frappe/email/email_body.py:358), so the RFC 3834
            # `Auto-Submitted: auto-generated` would be emitted as `X-Auto-Submitted` and
            # mean nothing to anyone. This one is already X-, so it survives intact.
            email_headers={"X-Auto-Response-Suppress": "All"},
            now=False,
            retry=3,
        )
    except (frappe.OutgoingEmailError, frappe.ValidationError):
        frappe.log_error(title=f"{context} email failed")
        return
    # Hand the queued mail straight to a worker instead of waiting for the next
    # `frappe.email.queue.flush` tick. Flush is an `All`-frequency job, so with
    # scheduler_interval at 60 an acknowledgement sat up to a further minute after the
    # ticket already existed — measured at 58s on INB-0008, and 240s before the interval
    # was corrected. This is the largest remaining slice of inbound-to-acknowledgement that
    # is ours to remove.
    #
    # Not `now=True` on sendmail above: that runs SMTP inline inside the inbound pull job,
    # so a slow M365 handshake would stall mail INTAKE for every account, and a send
    # failure would surface in the middle of ticket creation. Enqueuing keeps SMTP off the
    # pull path and keeps the retry semantics.
    #
    # Racing flush is safe in both directions: EmailQueue.send() returns immediately unless
    # can_send_now() (email_queue.py:159-167), and flush takes a row lock via
    # get_doc(for_update=True) (queue.py:150). Whichever gets there first wins; the other
    # no-ops. after_commit because the row must be committed before a worker looks for it.
    if queued and getattr(queued, "name", None):
        try:
            frappe.enqueue_doc(
                "Email Queue", queued.name, "send", queue="short", enqueue_after_commit=True
            )
        except Exception:
            # The mail is already queued and flush will still collect it on the next tick.
            # Losing the fast path must never cost the mail itself.
            frappe.log_error(title=f"{context} fast-send enqueue failed")
    if ticket:
        _anchor_outgoing(ticket, message_id, recipient, subject, html)
        _log_outgoing(ticket, message_id, recipient, subject, log_kind or context)


def _log_outgoing(ticket, message_id, recipient, subject, kind):
    """Append-only audit of every email we send about a ticket.

    Not derived from Email Queue on purpose: frappe purges that after 30 days — the same
    purge that used to break reply threading — so it cannot answer "did we ever actually
    tell the customer?" about anything older than a month, which is the one question an
    audit trail exists for.

    Best-effort, like the threading anchor: losing a log row must never cost the mail that
    already went out or the ticket action that triggered it.
    """
    try:
        frappe.get_doc({
            "doctype": "Ticket Email Log",
            "ticket": ticket,
            "kind": kind if kind in ("Acknowledgement", "Reply", "First Response", "Status") else "Reply",
            "recipient": recipient,
            "subject": subject,
            "message_id": message_id,
            "triggered_by": frappe.session.user,
            "queued_on": now_datetime(),
            "delivery_state": "Queued",
        }).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(title="Ticket email log write failed")


def reconcile_email_log():
    """Update Ticket Email Log rows with what actually happened to the mail.

    The log is written at queue time, so every row starts "Queued" — and nothing ever moved
    it, which meant the audit trail could not tell a delivered mail from a refused one.
    Found during pre-merge verification: two rows sat at "Queued" while their Email Queue
    entries had been in Error for hours.

    Reconciled on a schedule rather than by a hook because Email Queue writes its status
    through frappe.db.set_value (email_queue.py:126), which bypasses doc events entirely —
    there is nothing to subscribe to.

    Only touches rows still marked Queued, and only where the Email Queue row still exists;
    frappe purges those after 30 days, so anything older keeps its last known state rather
    than being falsely reported as failed.
    """
    pending = frappe.get_all(
        "Ticket Email Log",
        filters={"delivery_state": "Queued", "message_id": ["is", "set"]},
        fields=["name", "message_id"],
        limit=500,
    )
    if not pending:
        return
    queued = {
        q.message_id: q
        for q in frappe.get_all(
            "Email Queue",
            filters={"message_id": ["in", [p.message_id for p in pending]]},
            fields=["message_id", "status", "error"],
        )
    }
    state = {"Sent": "Sent", "Error": "Failed", "Expired": "Failed"}
    for row in pending:
        q = queued.get(row.message_id)
        if not q or q.status not in state:
            continue
        frappe.db.set_value(
            "Ticket Email Log",
            row.name,
            {"delivery_state": state[q.status], "failure_reason": (q.error or "")[:1000] or None},
            update_modified=False,
        )


def _anchor_outgoing(ticket, message_id, recipient, subject, html):
    """Record outgoing mail as a Communication so a reply can still find its ticket after
    the Email Queue row is gone.

    Threading resolves in order (receive.py:837-867): parent Email Queue row, then parent
    Communication, then a subject match on the doctype's append_to. Only the first two are
    available to us — and the first one EXPIRES. `run_log_clean_up` runs in
    daily_maintenance and deletes Email Queue older than 30 days
    (frappe/hooks.py:270, :508; email_queue.py:261), and that retention is seeded
    automatically on a fresh site, so it applies whether or not anyone configured it.

    Without this, threading silently stopped working on day 31: a customer replying to the
    acknowledgement of a ticket nobody had touched in a month forked a duplicate, exactly
    as before the reference argument existed. Every outbound mail resets the 30-day clock,
    so busy tickets were fine and quiet ones were not — the failure mode that is hardest
    to notice and worst to hit.

    Communication is never purged (it is absent from default_log_clearing_doctypes), and
    parent_communication() matches on Communication.message_id (receive.py:822), so this
    row keeps working indefinitely. It costs one row per outbound mail.

    Inserting a Sent Communication does NOT send anything: Communication.after_insert only
    updates status and fires a realtime notification — there is no send path on insert
    (frappe/core/doctype/communication/communication.py:190-196). Our own on_communication
    hook ignores it too, because it returns early on anything not Received."""
    try:
        doc = frappe.get_doc({
            "doctype": "Communication",
            "communication_type": "Communication",
            "communication_medium": "Email",
            "sent_or_received": "Sent",
            "subject": subject,
            "content": html,
            "sender": _support_inbox(),
            "recipients": recipient,
            "message_id": message_id,
            "reference_doctype": "Support Ticket",
            "reference_name": ticket,
        })
        # Keep the stored copy identical to what was sent; frappe would otherwise append
        # the outgoing account's signature to this record only.
        doc.flags.skip_add_signature = True
        doc.insert(ignore_permissions=True)
    except Exception:
        # Never let the anchor break the mail that already went out, or the ticket action
        # that triggered it. Losing one anchor costs threading on that message after 30
        # days; raising here would cost the whole operation now.
        frappe.log_error(title="Ticket mail threading anchor failed")


# ---- loop protection ------------------------------------------------------
# Automated mail is the one thing here that can run away. send_ticket_ack fires on every
# insert carrying a from_email, so a correspondent whose autoresponder strips threading
# headers gives: inbound -> ticket -> ack -> autoreply -> ticket -> ack, unbounded. On a
# live M365 tenant that is a weekend's worth of mail and a throttled domain.
#
# Two independent guards, because each alone has a gap:
#   1. Recognise machine-sent mail and refuse to open a ticket from it.
#   2. Cap acknowledgements per recipient per hour — which bounds the loop whatever
#      caused it, including causes neither of us predicted.
_ACK_CAP_PER_HOUR = 4

# Only genuine mail-transfer-agent addresses suppress ticket creation. `noreply@` and
# friends deliberately do NOT: a vendor's automated notice arriving here is still something
# the team may need to act on, so it becomes a ticket, gets classified `No Reply` by
# sender.py, and is badged in the UI. What it does not get is an acknowledgement — see
# send_ticket_ack — because nothing would read it and it may bounce straight back.
_AUTO_SENDERS = re.compile(r"^(?:mailer-daemon|postmaster)@", re.I)
# Anchored at the start (after any Re:/Fwd: chain) so an ordinary question that merely
# mentions one of these phrases — "how do I set an out of office?" — is untouched.
_AUTO_SUBJECTS = re.compile(
    r"^\s*(?:(?:re|fw|fwd)\s*:\s*)*(?:"
    r"out of (?:the )?office|automatic reply|auto[- ]?reply|automatic response"
    r"|abwesenheitsnotiz|automatische antwort"          # de
    r"|r[ée]ponse automatique|absence du bureau"        # fr
    r"|respuesta autom[áa]tica|fuera de la oficina"     # es
    r")",
    re.I,
)
# Delivery-failure phrases are NOT in the list above. They used to be, and because these
# are anchored but not corroborated, "Delivery has failed for our shipment, can you check
# the ticket?" was read as machine mail and silently produced no ticket. Bounces are now
# _is_bounce's job, which additionally requires a daemon sender or real DSN structure.


def _is_auto_generated(sender: str, subject: str) -> bool:
    """True for out-of-office replies, bounces and other machine-sent mail.

    Header inspection would be more precise — RFC 3834's `Auto-Submitted`, plus
    `Precedence` and `X-Auto-Response-Suppress` — but Communication stores no raw headers
    (only message_id/in_reply_to), so by the time on_communication runs they are gone.
    Sender and subject are what survives, and they cover the shapes that actually reach a
    support inbox.

    Suppression only stops a TICKET being opened. The Communication row is still written
    by frappe either way, so no mail is destroyed by a false positive here — it just has
    to be picked up by hand rather than appearing as a ticket."""
    sender = (parse_addr(sender or "")[1] or sender or "").strip()
    return bool(_AUTO_SENDERS.match(sender) or _AUTO_SUBJECTS.match(subject or ""))


# ---- bounces --------------------------------------------------------------
# A hard bounce is a delivery failure for mail WE sent, so it belongs on the ticket that
# sent it. Left alone it did the opposite of useful: it opened a junk ticket from
# MAILER-DAEMON, while the ticket that actually failed to reach its customer looked
# perfectly healthy and the agent believed they had replied.
_BOUNCE_SENDERS = re.compile(r"^(?:mailer-daemon|postmaster)@", re.I)
# Anchored at the start of the subject, like the auto-reply markers, so a customer writing
# ABOUT a delivery is untouched.
_BOUNCE_SUBJECTS = re.compile(
    r"^\s*(?:(?:re|fw|fwd)\s*:\s*)*(?:"
    r"undeliverable|undelivered mail|delivery status notification|returned mail"
    r"|mail delivery (?:failed|subsystem)|delivery has failed|failure notice"
    r"|unzustellbar|nicht zustellbar|échec de la remise|no se pudo entregar"
    r")",
    re.I,
)
# Structure only a real DSN has (RFC 3464 fields, or an SMTP status line). Anchoring the
# subject alone is not enough: "Delivery has failed for our shipment, can you check?" is a
# plausible subject for an industrial after-sales desk, and swallowing it would cost the
# customer their ticket. Requiring machine structure in the body separates the two.
_DSN_MARKERS = re.compile(
    r"^\s*(?:Final-Recipient|Original-Recipient|Diagnostic-Code|Reporting-MTA|Action:\s*failed)"
    r"|\b[45]\d\d[ -]\d\.\d\.\d\b|\bStatus:\s*[45]\.\d\.\d\b",
    re.M | re.I,
)
# Our own outgoing subjects are always "[TKT-ID] ...", and a bounce quotes the original
# subject — in its own subject ("Undeliverable: [INB-0002] ..."), in the headers frappe
# lifts out of an attached original, or in the body. The Message-ID would be the precise
# key, but frappe discards it: show_attached_email_headers_in_content keeps only
# From/To/Subject/Date (receive.py:559-571), and message/delivery-status parts are dropped
# by process_part entirely. The ticket id in the subject is what actually survives.
_TICKET_IN_TEXT = re.compile(r"\[([A-Z0-9]{2,12}(?:-[A-Z0-9]{2,12})?-\d{3,})\]")
# The address that failed, as reported in the DSN body.
_FAILED_RECIPIENT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _is_bounce(sender: str, subject: str, body: str = "") -> bool:
    """A delivery failure report, as opposed to a customer writing about a delivery.

    The sender alone settles it when a mail transfer agent reports the failure itself. A
    bounce relayed from some other address has to prove itself twice: the subject must
    OPEN with a failure phrase, and the body must carry DSN structure. Either signal on its
    own produces false positives on ordinary support mail, and a false positive here means
    a customer's request silently never becomes a ticket."""
    sender = (parse_addr(sender or "")[1] or sender or "").strip()
    if _BOUNCE_SENDERS.match(sender):
        return True
    return bool(_BOUNCE_SUBJECTS.match(subject or "")) and bool(_DSN_MARKERS.search(body or ""))


def _file_bounce(doc) -> bool:
    """Record a delivery failure on the ticket whose mail bounced. Returns True when it
    was handled, so the caller knows not to open a ticket from it.

    Filed as a WORK NOTE, not a client message: "we could not reach this address" is
    something the team must see and the customer must not. Work notes sit at permlevel 1,
    so frappe strips them from a client read for free.

    Returning True on an unmatched bounce is deliberate — a bounce is never a support
    request, so even when we cannot tell which ticket it belongs to the right outcome is
    "no ticket", not "junk ticket"."""
    body = _clean_body(_mail_body(doc), is_reply=False)
    haystack = f"{doc.subject or ''}\n{body}"
    match = _TICKET_IN_TEXT.search(haystack)
    ticket = match.group(1) if match else None
    if not ticket or not frappe.db.exists("Support Ticket", ticket):
        frappe.logger("helpdesk").info(
            f"bounce from {doc.sender} could not be matched to a ticket: {doc.subject!r}"
        )
        return True

    inbox = _support_inbox()
    failed = next(
        (
            a
            for a in _FAILED_RECIPIENT.findall(body)
            if a.lower() != inbox and not _BOUNCE_SENDERS.match(a)
        ),
        None,
    )
    reason = " ".join(body.split())[:400] or "no detail supplied"
    note = (
        f"Delivery failed{f' to {failed}' if failed else ''} — this ticket's email did not "
        f"reach the customer.\n\n{reason}"
    )

    t = frappe.get_doc("Support Ticket", ticket)
    if any((r.body or "").startswith("Delivery failed") and reason in (r.body or "") for r in (t.notes or [])):
        return True  # same bounce redelivered
    t.append("notes", {"author": "Mail system", "note_on": now_datetime(), "body": note})
    # Surfaces the ticket as unread for the team — an agent believing they replied when
    # they did not is the whole problem this solves.
    t.last_activity_on = now_datetime()
    t.save(ignore_permissions=True)
    return True


def _ack_key(recipient: str) -> str:
    """Redis key for one recipient's ack budget.

    The site is in the key by hand, on purpose. `incr`/`expire` come straight from
    redis.Redis and act on the RAW key, while frappe's own get_value/set_value/delete_value
    prefix the site themselves — so mixing the two families silently addresses two
    different keys. Everything here (and in the tests) goes through this helper and the raw
    family, so there is one key and it is site-scoped."""
    return f"helpdesk:ack:{frappe.local.site}:{recipient}"


def _ack_allowed(recipient: str) -> bool:
    """Per-recipient hourly cap on acknowledgements — the backstop that bounds any loop.

    Deliberately counts ACKs only. They are the automatic ones, sent on insert with no
    human involved; replies and status notifications need an agent to act, so a human is
    already in that loop. Four an hour is far above what a real correspondent generates
    (each of their mails is a separate ticket) and far below a runaway."""
    key = _ack_key(recipient)
    cache = frappe.cache()
    count = cache.incr(key)
    if count == 1:
        cache.expire(key, 3600)
    if count > _ACK_CAP_PER_HOUR:
        # Once per window, not once per message — a loop would otherwise flood Error Log
        # with the evidence of itself.
        if count == _ACK_CAP_PER_HOUR + 1:
            frappe.log_error(
                message=f"Suppressing acknowledgements to {recipient}: more than "
                f"{_ACK_CAP_PER_HOUR} in an hour. Likely an autoresponder loop — check "
                f"recent tickets from this address.",
                title="Helpdesk ack rate limit tripped",
            )
        return False
    return True


# ---- inbound: email → ticket ----------------------------------------------
def _open_ticket_from_email(sender, subject, body):
    """Create a Support Ticket from an inbound email. Returns the ticket name, or None when
    the message is our own outgoing mail (which must never loop into a ticket). The
    acknowledgement is sent by the Support Ticket after_insert hook, not here."""
    sender = (parse_addr(sender or "")[1] or sender or "").strip().lower()
    if not sender or sender == _support_inbox():
        return None
    if _is_auto_generated(sender, subject):
        # An out-of-office or a bounce is not a support request. Opening a ticket from one
        # both creates junk AND acks it, which is the first turn of the loop.
        frappe.logger("helpdesk").info(f"ignored auto-generated mail from {sender}: {subject!r}")
        return None
    doc = frappe.get_doc({
        "doctype": "Support Ticket",
        "title": (subject or "").strip()[:140] or "(no subject)",
        # is_reply=False: this mail OPENS the ticket, so there is no prior thread to strip
        # and a forwarded/quoted body is the customer's actual content.
        "description": (_clean_body(body, is_reply=False) or "—")[:100000],
        "status": "New",
        "from_email": sender,  # before_insert matches the POC + scopes the ticket
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def _backfill_description(ticket, content):
    """Fill in a ticket's description from the mail that opened it, when it landed empty.

    Same first-contact reasoning as _open_ticket_from_email: this is the ORIGINATING
    message, so nothing in it is a duplicate of the ticket and quote-stripping can only
    lose content."""
    if (frappe.db.get_value("Support Ticket", ticket, "description") or "").strip():
        return
    body = _clean_body(content, is_reply=False)
    if body:
        frappe.db.set_value("Support Ticket", ticket, "description", body[:100000], update_modified=False)


def _mail_body(doc) -> str:
    """Raw body of an inbound Communication, best part first.

    `text_content` is the message's plain-text alternative — no tag soup, no entities to
    unpick, and the quote/signature markers below are all line-anchored, so it reads far
    more reliably. Virtually every real mail client sends multipart; the HTML part is only
    the fallback."""
    return getattr(doc, "text_content", None) or getattr(doc, "content", None) or ""


# Where a mail client starts quoting the message being replied to. Everything from the
# earliest match onwards is the previous thread, which the ticket already holds — keeping
# it would make a ticket unreadable by the third or fourth reply, each one carrying every
# message before it.
_QUOTE_MARKERS = (
    # "On <date>, <someone> wrote:" — Gmail, Apple Mail, Thunderbird, Yahoo, and the
    # same shape in other locales. Two things this has to get right:
    #  - the attribution WRAPS ("...Inventive Helpdesk,\n<helpdesk@...>\nwrote:"), so it
    #    must span newlines rather than match a single line;
    #  - it must contain a DATE. Without the \d{1,4} requirement this also fires on
    #    ordinary prose — "On Monday the engineer said the valve was fine, but in his
    #    report he wrote: replace it" truncated the message to its first line.
    re.compile(
        r"^(?:On|Le|El|Em|Op|Am|Den|På)\s[^\n]{0,60}?\d{1,4}.{0,340}?"
        r"\b(?:wrote|sent|a écrit|escribió|escreveu|schreef|schrieb|skrev)\b[^:]{0,120}:",
        re.M | re.S,
    ),
    # Lotus Notes: "Helpdesk <h@x.com> wrote on 22/07/2026 18:21:"
    re.compile(r"^[^\n]{0,120}\bwrote on\s+\d[^\n]{0,60}:\s*$", re.M),
    re.compile(
        r"^\s*-{2,}\s*(?:Original Message|Ursprüngliche Nachricht|Message d'origine|"
        r"Mensaje original|Mensagem original|Oorspronkelijk bericht)\s*-{2,}",
        re.M | re.I,
    ),
    # Forwarded-message banners (Gmail, Apple/Outlook).
    re.compile(
        r"^\s*(?:-{2,}\s*)?(?:Forwarded message|Begin forwarded message|"
        r"Weitergeleitete Nachricht|Message transféré|Mensaje reenviado)\s*[:-]*\s*-*\s*$",
        re.M | re.I,
    ),
    re.compile(r"^[ \t]*(?:_{10,}|-{10,})[ \t]*$", re.M),  # Outlook web / Thunderbird rule
    # Outlook desktop header block: two consecutive "Header: value" lines. Matched as two
    # adjacent lines rather than "From: ... <anything> ... To:", because the lazy
    # `.+?` under re.S that did that restarted a scan to end-of-string at every "From:"
    # line — 200 KB of mail took 6.5 SECONDS of CPU in a background worker. This form
    # never scans past one line boundary: 200 KB now takes ~20 ms.
    re.compile(
        r"^[ \t]*\*?(?:From|Von|De|Van|Sent|Date|Gesendet|Envoyé|Enviado|Datum|"
        r"Subject|Betreff|Objet|Asunto|Assunto|To|An|À|Para|Cc)[ \t]*:[ \t]?[^\n]*\n"
        r"[ \t]*\*?(?:From|Von|De|Van|Sent|Date|Gesendet|Envoyé|Enviado|Datum|"
        r"Subject|Betreff|Objet|Asunto|Assunto|To|An|À|Para|Cc)[ \t]*:[ \t]?",
        re.M,
    ),
)

# The plain-text quote prefix, anchored to the END of the message. This is deliberately
# NOT in _QUOTE_MARKERS: as a bare `^\s*>` it cut at the FIRST ">" line anywhere, so a
# customer pasting a log or a config block lost everything after it —
#   "The log shows:\n> ERROR 500 at /api/export\nCan you check?"  ->  "The log shows:"
# losing both the error and the question, on exactly the messages that matter most to a
# technical support desk. Quoting is only quoting when it runs to the end of the message.
_QUOTE_TAIL = re.compile(r"\n[ \t]*>[^\n]*(?:\n(?:[ \t]*>[^\n]*|[ \t]*))*$")


# Tags that end a visual line. Rewritten to newlines so the markers below, all of which
# are line-anchored, can see the structure of an HTML-only mail.
_BLOCK_END = re.compile(
    r"</(?:div|p|li|tr|h[1-6]|blockquote|table)\s*>|<br\s*/?>|</?(?:div|p|blockquote)\b[^>]*>",
    re.I,
)
# Signature blocks. Only unambiguous markers are cut outright: "-- " on its own line is
# the RFC 3676 delimiter, and mobile footers are fixed strings. Sign-offs are handled
# separately below because "Thanks" can legitimately open a sentence.
_SIGNATURE_MARKERS = (
    re.compile(r"^-{2,3}\s*$", re.M),  # RFC 3676 "-- ", plus the common broken "---"
    re.compile(r"^Sent from (?:my|Outlook|Mail for Windows)\b", re.M | re.I),
    re.compile(r"^Get Outlook for \w+", re.M | re.I),
    re.compile(r"^(?:Enviado desde mi|Envoyé de mon|Verzonden vanaf mijn)\b", re.M | re.I),
    re.compile(r"^Von meinem \w+ gesendet", re.M | re.I),
)
# A closing line ("Regards," / "Thanks,") followed by only a few short lines — a name,
# a company, maybe a phone number. Anchored to the END of the message, and capped at five
# lines of under 60 characters, so a "Thanks," in the middle of a real paragraph and a
# genuine closing sentence are both left alone.
_SIGN_OFF = re.compile(
    r"\n[ \t]*(?:thanks|thank you|regards|best regards|kind regards|warm regards|sincerely|"
    r"respectfully|cheers|best|yours truly|yours sincerely|mit freundlichen grüßen|"
    r"cordialement|saludos|atenciosamente)[,.!]?[ \t]*\n"
    # Each trailing line ends in a mandatory \n; only the last may be unterminated. The
    # previous `(?:[^\n]{0,60}\n?){0,5}$` made the \n optional inside the repetition,
    # which is superlinear on a long body.
    r"(?:[^\n]{0,60}\n){0,5}[^\n]{0,60}\n?$",
    re.I,
)


def _clean_body(text: str, is_reply: bool = True) -> str:
    """Plain, quote-free, signature-free text for storing as a message.

    Three things get removed, in order of confidence:
      1. HTML tags and entities — strip_html only does tags, which is why a raw reply
         rendered as `&lt;helpdesk@...&gt;`.
      2. The quoted thread, which the ticket already holds.
      3. The sender's signature.

    ``is_reply=False`` stops after step 1. On a FIRST-CONTACT mail there is no earlier
    conversation in the ticket to deduplicate against, so quote-stripping can only destroy
    content: a customer forwarding a supplier's rejection notice ("---------- Forwarded
    message ----------" plus the original headers and body) is one of the commonest ways a
    B2B ticket arrives, and treating that banner as a quote boundary reduces the whole
    ticket to the banner.

    Deliberately conservative: every rule keeps the text when it isn't confident, and the
    function never returns empty for non-empty input. Losing a customer's actual question
    is far worse than leaving a stray "Regards," on the end of it — that is why a message
    that is nothing but a signature (an early inbound ticket here was exactly that) keeps
    its body instead of being blanked."""
    # Turn block boundaries into newlines BEFORE stripping tags. strip_html just deletes
    # them, which welds the last word of the body onto the first word of the quote
    # ("I don't knowOn Wed, 22 Jul...") and leaves every line-anchored marker below unable
    # to match. Only matters when a mail has no plain-text alternative.
    text = _BLOCK_END.sub("\n", text or "")
    text = unescape_html(strip_html(text)).replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text or not is_reply:
        return text

    def cut_at(patterns, s):
        start = min((m.start() for m in (p.search(s) for p in patterns) if m), default=len(s))
        return s[:start].rstrip() or s

    text = cut_at(_QUOTE_MARKERS, text)
    text = _QUOTE_TAIL.sub("", text).rstrip() or text
    text = cut_at(_SIGNATURE_MARKERS, text)
    trimmed = _SIGN_OFF.sub("", text).strip()
    return trimmed or text


def _append_client_reply(ticket_name, doc):
    """Record an inbound reply as a client message on the ticket it belongs to.

    Frappe resolved the reference for us (via the Message-ID on our outgoing mail), so all
    that's left is to make the reply visible: without this the mail threads onto the right
    ticket and then sits in Communication where no agent ever sees it.

    Mirrors the row api.add_message writes, so a reply that arrived by email and one typed
    in the portal are indistinguishable in the thread."""
    # text_content is the message's plain-text alternative — no tag soup, no entities to
    # unpick — so prefer it and fall back to the HTML part only when it's absent.
    body = _clean_body(_mail_body(doc))
    if not body:
        return
    ticket = frappe.get_doc("Support Ticket", ticket_name)
    # Idempotency keys on the Communication, NOT on the body text. Body matching looks
    # equivalent and is not: a client who sends the same wording twice — "any update?",
    # "thanks", "still broken" — is ordinary support traffic, and matching on text threw
    # the second one away silently, so the agent never saw the follow-up. Keying on the
    # source keeps a genuine re-delivery idempotent without ever discarding a real message.
    source = getattr(doc, "name", None)
    if source and any((r.source_communication or "") == source for r in (ticket.conversation or [])):
        return
    sender = (parse_addr(doc.sender or "")[1] or doc.sender or "").strip().lower()
    ticket.append("conversation", {
        "kind": "client",
        "author": (doc.sender_full_name or "").strip() or sender,
        "role": "Client",
        "message_on": now_datetime(),
        "body": body,
        "source_communication": source,
    })
    # Marks the ticket unread for every agent except whoever posts next (see api.py).
    ticket.last_activity_on = now_datetime()
    ticket.save(ignore_permissions=True)


def on_communication(doc, method=None):
    """Real incoming Email Account path. An inbound email either continues an existing
    ticket or opens a new one.

    Frappe has already set reference_doctype/reference_name when the message is a reply to
    mail we sent (it matches In-Reply-To against our outgoing Email Queue row — see the
    note in _queue_mail). Anything unreferenced is a fresh conversation."""
    if doc.sent_or_received != "Received":
        return
    if doc.reference_doctype == "Support Ticket" and doc.reference_name:
        _backfill_description(doc.reference_name, _mail_body(doc))
        _append_client_reply(doc.reference_name, doc)
        return
    if doc.reference_doctype:
        return  # linked to some other doctype — not ours
    # A bounce is a failure report about mail WE sent, so it belongs on the ticket that
    # sent it — never as a new ticket. Checked before intake because a DSN otherwise looks
    # exactly like a fresh inbound email.
    if _is_bounce(doc.sender, doc.subject, _mail_body(doc)) and _file_bounce(doc):
        return
    name = _open_ticket_from_email(doc.sender, doc.subject, _mail_body(doc))
    if name:
        doc.db_set("reference_doctype", "Support Ticket", update_modified=False)
        doc.db_set("reference_name", name, update_modified=False)


# A single mail should not be able to bury a ticket. Anything beyond this is left on the
# Communication, where an agent can still reach it from the desk.
_MAX_INBOUND_ATTACHMENTS = 20


def on_communication_update(doc, method=None):
    """Move an inbound email's attachments onto the ticket it belongs to.

    Frappe attaches inbound files to the COMMUNICATION, not to the referenced document
    (receive.py:632-645), so "here's a screenshot of the error" — routine for after-sales
    support — left the agent reading a body that referred to a file the ticket did not
    have. The file existed, but only in the desk, on a Communication no agent opens.

    This is `on_update` rather than part of `on_communication` because of the ORDER frappe
    works in: insert (our after_insert hook runs, sees nothing), reload, save attachments,
    then save — and that final save is what gets us here. Putting this in after_insert
    would look right and always find zero files.

    Re-parenting rather than copying also fixes the permission: File.has_permission defers
    to whatever the file is attached to, so on a Communication a client POC could not
    download their OWN attachment, while on the ticket it follows the tenant isolation
    already enforced there.

    Naturally idempotent — once re-parented, the query below no longer matches, so
    repeated on_update calls do nothing."""
    if doc.sent_or_received != "Received":
        return
    if doc.reference_doctype != "Support Ticket" or not doc.reference_name:
        return
    files = frappe.get_all(
        "File",
        filters={"attached_to_doctype": "Communication", "attached_to_name": doc.name},
        fields=["name", "file_name", "file_url"],
        order_by="creation asc",
        # `limit`, not `limit_page_length` — the latter is deprecated for removal in v17
        # (frappe/model/qb_query.py:153).
        limit=_MAX_INBOUND_ATTACHMENTS,
    )
    if not files:
        return
    try:
        refs = []
        for f in files:
            frappe.db.set_value(
                "File",
                f.name,
                {"attached_to_doctype": "Support Ticket", "attached_to_name": doc.reference_name},
                update_modified=False,
            )
            refs.append({"name": f.file_name, "url": f.file_url})
        _append_ticket_attachments(doc.reference_name, refs)
    except Exception:
        # An attachment that fails to move must not cost us the message it came with.
        frappe.log_error(title="Inbound attachment re-parenting failed")


def _append_ticket_attachments(ticket_name, refs):
    """Add file refs to the ticket's own `attachments` list, matching the shape
    api._attach_private_file writes so the portal renders both identically."""
    current_raw = frappe.db.get_value("Support Ticket", ticket_name, "attachments")
    try:
        current = json.loads(current_raw or "[]")
    except (ValueError, TypeError):
        current = []
    seen = {(r or {}).get("url") for r in current}
    added = [r for r in refs if r["url"] not in seen]
    if not added:
        return
    frappe.db.set_value(
        "Support Ticket",
        ticket_name,
        "attachments",
        json.dumps(current + added),
        update_modified=False,
    )


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
    # An unmonitored mailbox gets no acknowledgement: nobody reads it, and on a live tenant
    # acking `noreply@` invites a bounce or a loop for zero benefit. The ticket still
    # exists and still shows in the queue — only the automatic mail is withheld.
    from inventive_helpdesk_backend import sender

    if not sender.can_receive_email(doc):
        return
    recipient = _ticket_contact_email(doc)
    if recipient and _ack_allowed(recipient):
        subject = f"[{doc.name}] " + ((doc.title or "").strip() or "We've received your request")
        _queue_mail(
            recipient,
            subject,
            _ack_email_html(doc.name, doc.title, _client_cta(doc, "View your ticket")),
            "Ticket acknowledgement",
            doc.name,
            log_kind="Acknowledgement",
        )


def notify_client_reply(ticket, body, kind="Reply"):
    """Email the client a staff member's client-visible reply.

    `kind` is the plan sender.reply_plan chose, and it does two things: it selects the
    wording, and it is recorded in Ticket Email Log so the audit trail says WHY this went
    out. "First Response" is the one-time mail sent even though the agent left the email
    toggle off — it carries the reply verbatim (the client can already read it in the
    portal, so there is nothing to withhold) plus an explicit pointer to sign in, because
    that is the whole reason for sending it.
    """
    recipient = _ticket_contact_email(ticket)
    if not recipient:
        return
    subject = f"[{ticket.name}] " + ((ticket.title or "").strip() or "Update on your request")
    html = (
        # First Response is only ever planned for a Registered sender, so the portal link
        # is always the right call to action there.
        _first_response_email_html(ticket.name, body, _portal_ticket_url(ticket.name))
        if kind == "First Response"
        else _reply_email_html(ticket.name, body, _client_cta(ticket, "Reply in the portal"))
    )
    _queue_mail(recipient, subject, html, "Ticket reply", ticket.name, log_kind=kind)


# Status changes worth emailing the client about (heading, body message).
#
# Deliberately channel-NEUTRAL: not one of these sentences may name the portal or email.
# Naming a channel here contradicts the call to action underneath, which _client_cta picks
# per recipient — an unregistered sender was being told "please reply in the portal" in one
# paragraph and "there is no account to set up" in the next. The CTA knows who it is
# talking to; this text does not, so it must not guess.
_STATUS_NOTIFY = {
    "Resolved": (
        "Your request has been resolved",
        "We've marked this ticket as resolved. If it didn't fully solve things, just let us know — "
        "we're happy to keep helping.",
    ),
    "Pending Client": (
        "We need a little more from you",
        "This ticket is waiting on your input before we can move it forward.",
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
    # "Pending Client" asks them to respond; the others just invite a look.
    label = "Reply in the portal" if doc.status == "Pending Client" else "View your ticket"
    subject = f"[{doc.name}] " + ((doc.title or "").strip() or heading)
    _queue_mail(
        recipient,
        subject,
        _status_email_html(doc.name, heading, message, doc.status, _client_cta(doc, label)),
        f"Ticket {doc.status} notification",
        doc.name,
        log_kind="Status",
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


def _client_portal_url(ticket):
    """Portal deep link, but only for someone who can actually sign in.

    Offering "View your ticket" to a sender with no account sends them to a login wall
    they cannot pass — worse than offering nothing, because it looks like the system
    expects something of them that is impossible. Only a Registered sender has a login;
    an unregistered sender and a known contact who was never invited do not.
    """
    from inventive_helpdesk_backend import sender

    kind, _address, _reason = sender.classify(ticket)
    return _portal_ticket_url(ticket.name) if kind == sender.REGISTERED else ""


def _reply_by_email_line():
    """The call to action for a client with no portal: the channel they actually have.

    Accurate as well as useful — a reply threads back onto the ticket via the Message-ID
    anchor, so this is not a polite fiction.
    """
    return (
        '<p style="font-size:13.5px;line-height:1.6;color:#464b5c;margin:0 0 22px;">'
        "<b>Just reply to this email</b> and your message will be added to the ticket. "
        "There is no account to set up."
        "</p>"
    )


def _client_cta(ticket, portal_label):
    """Portal button for someone who can sign in; reply-by-email for everyone else."""
    url = _client_portal_url(ticket)
    return _portal_button(url, portal_label) if url else _reply_by_email_line()


def _shell(inner):
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:520px;margin:0 auto;color:#1e2230;">' + inner + "</div>"
    )


def _ack_email_html(ticket_name, subject_line, cta):
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
  {cta}
  <p style="font-size:12.5px;line-height:1.6;color:#6b7182;margin:0 0 18px;">
    Please quote <b>{ticket_name}</b> in any follow-up so we can find your request quickly.
  </p>
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    Inventive Helpdesk · After-sales support
  </p>""".strip())


def _reply_email_html(ticket_name, body, cta):
    reply = frappe.utils.escape_html((body or "").strip()).replace("\n", "<br>") or "(no message)"
    return _shell(f"""
  <h2 style="font-size:20px;font-weight:700;margin:0 0 6px;">New reply on {ticket_name}</h2>
  <p style="font-size:14px;line-height:1.6;color:#464b5c;margin:0 0 14px;">Our support team has replied to your request:</p>
  <div style="background:#f4f5fb;border-left:3px solid #4f46e5;border-radius:6px;padding:12px 16px;margin:0 0 18px;font-size:14px;line-height:1.6;color:#1e2230;">
    {reply}
  </div>
  {cta}
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    Ticket {ticket_name} · Inventive Helpdesk
  </p>""".strip())


def _first_response_email_html(ticket_name, body, portal_url):
    """The one-time mail sent when an agent replies with the email toggle off.

    Carries the reply verbatim — it is already in the client-visible thread, so there is
    nothing to withhold — and then says plainly where the rest of the conversation lives.
    That pointer is the entire reason this mail exists: without it a registered client has
    no way to know a portal they may never have opened now has something in it.
    """
    reply = frappe.utils.escape_html((body or "").strip()).replace("\n", "<br>") or "(no message)"
    return _shell(f"""
  <h2 style="font-size:20px;font-weight:700;margin:0 0 6px;">Our team has replied to {ticket_name}</h2>
  <div style="background:#f4f5fb;border-left:3px solid #4f46e5;border-radius:6px;padding:12px 16px;margin:14px 0 18px;font-size:14px;line-height:1.6;color:#1e2230;">
    {reply}
  </div>
  <p style="font-size:13.5px;line-height:1.6;color:#464b5c;margin:0 0 16px;">
    You have an account on our support portal, and that&#39;s where the rest of this
    conversation will be. Sign in to reply, add details, or follow progress on this ticket —
    we won&#39;t email every update.
  </p>
  {_portal_button(portal_url, "Sign in to your portal")}
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    Ticket {ticket_name} · Inventive Helpdesk
  </p>""".strip())


def _status_email_html(ticket_name, heading, message, status, cta):
    badge = "#16a34a" if status == "Resolved" else "#d97706"  # green resolved / amber pending
    return _shell(f"""
  <h2 style="font-size:20px;font-weight:700;margin:0 0 6px;">{heading}</h2>
  <p style="font-size:14px;line-height:1.6;color:#464b5c;margin:0 0 16px;">{frappe.utils.escape_html(message)}</p>
  <div style="margin:0 0 18px;">
    <span style="display:inline-block;font-size:12px;font-weight:700;color:#fff;background:{badge};padding:4px 12px;border-radius:20px;">{status}</span>
    <span style="font-size:13px;color:#6b7182;margin-left:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">{ticket_name}</span>
  </div>
  {cta}
  <p style="font-size:12px;color:#8a90a2;line-height:1.6;margin:0;border-top:1px solid #eceef3;padding-top:14px;">
    Inventive Helpdesk · After-sales support
  </p>""".strip())
