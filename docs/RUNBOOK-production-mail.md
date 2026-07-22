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

> ### First: turn the mailbox off everywhere else
>
> Production and any developer bench would share **one real mailbox**. Each site tracks IMAP
> UIDs independently, so they do not take turns — they both pull the same message. Every
> customer email would become **two tickets and two acknowledgements**, one of them sent from
> a laptop, and the customer cannot tell which is real.
>
> Before enabling the account here, on every other bench that has it configured, untick
> **Enable Incoming** and **Enable Outgoing** on the Email Account. Verify with:
>
> ```
> bench --site <site> console
> >>> frappe.get_all("Email Account", filters={"enable_incoming": 1}, pluck="name")
> ```
>
> Expect `[]` everywhere except production.

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

Then click **Get OpenID Configuration** (button, top right) and save. That button is what
fills **Authorization URI** and **Token URI** — they do *not* populate on save. If it errors
or leaves them blank the OpenID URL is wrong; fix it before continuing, because nothing
downstream works and the failure resurfaces later disguised as a credential error.

There is **no "Get Access Token" button** in v16. The only other button, *Connect to …*, is
the browser-based web application flow and is the wrong one for a service principal. To
prove the credentials on their own, before involving the mailbox:

```
bench --site <site> console
>>> frappe.get_doc("Connected App", "Inventive Helpdesk Mail").get_backend_app_token()
```

A Token Cache object back means secret, tenant, client ID and scope are all correct.
`AADSTS7000215` means the Secret **ID** was pasted instead of the Secret **Value**.

Do this before step 2. If you skip it and the Email Account fails to save, you cannot tell
whether the problem is the credentials, the scope, or the mailbox.

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
| **Authenticate as Service Principal** | **✓ — mandatory, see below** |
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

Four of those matter and are easy to get wrong:

- **"Authenticate as Service Principal" must be ticked.** This is the one that fails
  silently. `pull()` skips any OAuth account that has neither this flag nor a user token:

  ```python
  if (email_account.auth_method == "OAuth"
      and not email_account.backend_app_flow
      and not has_token(email_account.connected_app, email_account.connected_user)):
      continue
  ```

  (`frappe/email/doctype/email_account/email_account.py:996-1002`.) No error, no log entry —
  inbound mail simply never arrives, while the account looks correctly configured and the
  Connected App holds a perfectly valid token. Leaving it unticked also makes **Connected
  User** appear and behave as required, which pulls you further down the wrong path.

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

The inbound pull cron needs **nothing done by hand**. The app declares `*/2 * * * *` in
`hooks.py`, and `bench migrate` applies it on every deploy.

> Do **not** set it in the Desk UI instead. `sync_jobs → insert_single_event` rewrites
> `cron_format` from hooks whenever they differ
> (`scheduled_job_type.py:270-275`), and that runs on every migrate — so a value typed into
> the UI silently reverts on the next deploy, when nobody is watching. This was found by
> checking the live value after a migrate and discovering it had gone back to `0/10`.

Result, measured locally with Frappe's own `get_next_execution()`: pull every 2 min, flush
every 1 min — worst case inbound-to-acknowledgement ~3–5 minutes.

### The cron is a ceiling, not a guarantee

`pull()` skips a cycle entirely if the previous pull for that account has not finished:

```python
job_name = f"pull_from_email_account|{email_account.name}"
queued_jobs = get_jobs(site=frappe.local.site, key="job_name")[frappe.local.site]
if job_name not in queued_jobs:
    enqueue(...)
```

`get_jobs` counts **queued *and* running** jobs (`background_jobs.py:490`), so a pull that
overruns the interval silently costs the next tick — no log, no retry, no message. The real
intake interval is therefore `max(cron, pull duration)`, not the cron.

Seen locally on 2026-07-22: two mails waited **249 s** and **~173 s** against a 120 s cron,
i.e. one to two skipped cycles.

**So do not shorten the cron before measuring how long a pull actually takes.** If a pull
against M365 runs 30–90 s, moving to `* * * * *` skips roughly every other tick and buys
little while doubling IMAP connections. Measure first, in Desk → **RQ Job**, filtering on
`pull_from_email_account` and comparing `started_at` to `ended_at` over a working day. Change
the cron only if that number is comfortably under the interval you want.

The outbound half needed no such caution and is already done in code: `_queue_mail` hands each
queued mail straight to a worker rather than waiting for the next `flush` tick.

---

## 3b. Confirm `app_url` is set, or no client ever sees a portal link

`_portal_ticket_url` returns an empty string when `app_url` is missing from site config,
and `_client_cta` then falls back to "just reply to this email" — **for everyone, including
registered users who do have a login**. Nothing errors and nothing is logged; the portal
button simply never appears.

```
bench --site <site> console
>>> frappe.conf.get("app_url")
```

Expect the frontend origin, e.g. `https://helpdesk.inventivebizsol.co.in` — not `None`, and
not the backend's own URL. Set it with:

```
bench --site <site> set-config app_url https://helpdesk.inventivebizsol.co.in
```

This is the same gap that failed the first production release: two tests asserted a portal
link and passed only because the developer's bench had `app_url` while CI's fresh site did
not.

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
