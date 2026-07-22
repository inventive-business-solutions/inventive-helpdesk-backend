# Runbook — turning on mail in production

Every step here is transcribed from the configuration proven working on the local site, not
from documentation. Roughly 30 minutes.

> ## Do not start until the code is deployed
>
> Verified live at the time of writing:
>
> ```
> backend   https://helpdeskfrappe.inventivebizsol.co.in  ->  build_sha d344460
> frontend  https://helpdesk.inventivebizsol.co.in        ->  version   c114368
> ```
>
> That backend is **13 commits behind `development`**, and its `_queue_mail` calls
> `frappe.sendmail(recipients, subject, message, now, retry)` — with **no ticket
> reference at all**.
>
> Connecting the live mailbox to that build means, on real customer mail:
>
> | Missing on prod | Consequence the first day |
> | --- | --- |
> | reference / Message-ID anchor | **every customer reply opens a duplicate ticket** |
> | ack rate limit + `X-Auto-Response-Suppress` | an autoresponder can loop unbounded; M365 throttles the domain |
> | `_clean_body` | replies arrive as tag soup carrying the whole quoted thread |
> | `_is_bounce` | `MAILER-DAEMON` opens junk tickets; real delivery failures stay invisible |
> | `sender` classification | no-reply senders are silently dropped, agents see no warning |
> | attachment re-parenting | emailed files never reach the ticket |
>
> **Merge `development` → `master` first.** That is the release, and it triggers the deploy.
> Then confirm `build_sha` has moved before touching any of the steps below.

---

## 0. Prerequisites

- Someone with **System Manager** on the production site.
- The Azure **client secret value** for app `89799f0f-fab0-46bb-bd25-9424e61e1b43`.
  Not recorded anywhere in this repo, and it should not be pasted into a chat or a ticket.
  Expires around **2028-07-22** — put a calendar reminder at 2028-06-01.
- Non-secret identifiers (safe to copy):
  - Tenant ID `1cc82d37-b0b2-490a-b2bf-11b17d184d27`
  - Client ID `89799f0f-fab0-46bb-bd25-9424e61e1b43`

Azure side is already done — the service principal exists and `Set-CASMailbox` was applied
to the mailbox. Nothing to repeat there unless the secret has expired.

---

## 1. Connected App

Desk → **Connected App** → New.

| Field | Value |
| --- | --- |
| Provider Name | `Inventive Helpdesk Mail` |
| OpenID Configuration | `https://login.microsoftonline.com/1cc82d37-b0b2-490a-b2bf-11b17d184d27/v2.0/.well-known/openid-configuration` |
| Client ID | `89799f0f-fab0-46bb-bd25-9424e61e1b43` |
| Client Secret | *the secret **Value**, not the Secret ID* |
| Scopes | one row: `https://outlook.office365.com/.default` |

Save, then reopen and confirm **Authorization URI** and **Token URI** self-populated from
the OpenID document. If they are blank the OpenID URL is wrong — fix it before continuing;
nothing downstream will work.

> The client-credentials section only appears **after the first save**. That is expected.

**Redirect URI is irrelevant here.** Client credentials is a service-principal flow with no
browser round-trip, so the value Frappe generates (it will name `localhost`) is never used.
Do not spend time on it.

---

## 2. Email Account

Desk → **Email Account** → New.

| Field | Value |
| --- | --- |
| Email Address | `helpdesk@inventivebizsol.com` |
| Auth Method | **OAuth** |
| Connected App | the app from step 1 |
| Connected User | *leave empty* — client credentials authenticates as the app, not a user |
| Enable Incoming | ✓ · Use IMAP ✓ · Use SSL ✓ |
| Email Server | `outlook.office365.com` · port `993` |
| IMAP Folder | one row: `INBOX` (leave *Append To* empty) |
| Enable Outgoing | ✓ · Use TLS ✓ |
| SMTP Server | `smtp.office365.com` · port `587` |
| Default Incoming | ✓ |
| Default Outgoing | ✓ |
| Always use Account's Email Address as Sender | ✓ |
| Email Sync Option | `ALL` · Initial Sync Count `100` |

Leave **off**: Create Contact, Enable Automatic Linking, Track Email Status, Append To.

Three of those matter and are easy to get wrong:

- **Append To empty.** Setting it to Support Ticket hands ticket creation to Frappe's
  `_create_reference_document`, bypassing `_open_ticket_from_email` — and with it the POC
  scoping, body cleaning and no-reply classification.
- **Enable Automatic Linking off.** It only governs plus-addressing and recipient-based
  timeline links. Reply threading does not depend on it (verified in the framework source).
- **An IMAP Folder row is required.** Saving without one fails with an unhelpful error.

Save. Frappe validates the connection on save, so a failure here is a real credential
problem — most often the Secret **ID** pasted instead of the Secret **Value**, which
surfaces from Microsoft as `AADSTS7000215: Invalid client secret provided`.

---

## 3. Scheduler cadence — the step that is not in the UI

Without this, inbound mail takes **~10–14 minutes** to appear and the reply another
**4–8**, so a customer waits up to ~20 minutes for an acknowledgement.

In `common_site_config.json` on the production bench:

```json
"scheduler_tick_interval": 60,
"scheduler_interval": 60
```

**Both keys.** Frappe v16 reads them from different places — `scheduler_tick_interval`
drives the tick (`frappe/utils/scheduler.py:264`), while `scheduler_interval` builds the
cron for `All`-frequency jobs (`scheduled_job_type.py:126`), which is what
`frappe.email.queue.flush` is. Set only the first and outbound mail stays on a 4-minute
cycle no matter what.

Then **restart the bench** — the scheduler reads the tick at startup.

Finally, speed up the inbound pull. Desk → **Scheduled Job Type** → find
`frappe.email.doctype.email_account.email_account.pull` → set **Cron Format** to
`*/2 * * * *`.

Result, measured locally with Frappe's own `get_next_execution()`: pull every 2 min, flush
every 1 min — worst case inbound-to-acknowledgement ~3–5 minutes.

---

## 4. Verify, in this order

Stop at the first failure; each step depends on the one before.

1. **Token** — Connected App → *Get Access Token*. Should return a token, not an error.
2. **Inbound** — send a mail from an outside address (a personal Gmail) to
   `helpdesk@inventivebizsol.com`. Within ~3 minutes a ticket appears with a clean body:
   no HTML tags, no quoted thread, no signature.
3. **Acknowledgement** — that same address receives `[INB-xxxx] <subject>`.
4. **Threading — the one that used to fail.** Reply to that acknowledgement. It must attach
   to the **existing** ticket as a client message. **No second ticket.** If a duplicate
   appears, the deployed build predates the fix; stop and check `build_sha`.
5. **Attachment** — reply with a file attached; it appears on the ticket, not just in the
   desk's Communication.
6. **Audit** — Desk → **Ticket Email Log** has a row per outgoing mail with its kind.

---

## 5. After go-live

- **Log Settings → Email Queue retention** no longer needs raising. That was the stopgap
  for the 30-day threading cliff, and the durable Communication anchor replaces it.
- **Watch Error Log** for `Helpdesk ack rate limit tripped` — that is the loop guard firing,
  and it names the correspondent.
- **No Reply Rule** is where to add a customer address the built-in patterns miss. The
  built-ins are deliberately narrow: they only match addresses that announce they take no
  replies, because a false positive withholds the acknowledgement and the sender hears
  nothing at all.

## Rollback

Mail is entirely site configuration; no code change is involved. To stop all mail flow,
untick **Enable Incoming** and **Enable Outgoing** on the Email Account. Tickets, threads
and logs are untouched.
