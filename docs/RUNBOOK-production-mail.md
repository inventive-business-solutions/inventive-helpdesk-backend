# Runbook — mail in production

> ## Status: DONE. Production mail is live and receiving real customer email.
>
> The mailbox is connected, inbound is arriving, and acknowledgements are going out.
> **Nothing below needs doing.** Every step is kept as a rebuild reference — for a
> disaster recovery, a second environment, or the 2028 secret rotation.
>
> This file previously opened with a "do not start, the build is unsafe" banner citing
> `build_sha d344460`. That was true the day it was written and stale within days, and it
> was read later as if it described the present. **Do not trust a status line in this or
> any other doc here.** Check the running system instead — it answers in one command:
>
> ```
> curl -s https://helpdeskfrappe.inventivebizsol.co.in/api/method/inventive_helpdesk_backend.health.check
> curl -s https://helpdesk.inventivebizsol.co.in/api/health
> ```
>
> Then prove what that build actually contains, rather than inferring it from its age:
>
> ```
> git show <build_sha>:inventive_helpdesk_backend/email.py | grep reference_name
> git log --oneline <build_sha>..HEAD -- inventive_helpdesk_backend/email.py
> ```
>
> The whole mail series — threading via the durable Communication anchor, the ack rate
> limit with `X-Auto-Response-Suppress`, `_clean_body`, `_is_bounce`, sender
> classification, attachment re-parenting, and the priority-based response-time line —
> has been live since `c89d6e6`.

Every step here is transcribed from configuration proven working on a real mailbox, not
from documentation. Roughly 30 minutes from scratch.

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
| Email Sync Option | **`UNSEEN`** — not `ALL`, see below |

Leave **off**: Create Contact, Enable Automatic Linking, Track Email Status, Append To.

Five of those matter and are easy to get wrong:

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
- **Email Sync Option `UNSEEN`, not `ALL`.** `ALL` derives its IMAP watermark from the
  Communications table, so deleting tickets — clearing test data before go-live, say —
  permanently deadlocks intake with no error anywhere. Full diagnosis under *"Intake stops
  dead after tickets are deleted"* below; it cost an afternoon on 2026-07-27.

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

Set them with **`-gp`**, never `-g`:

```
bench set-config -gp scheduler_tick_interval 60
bench set-config -gp scheduler_interval 60
```

`-g` writes the value as a **string**, and `"60"` breaks the scheduler outright:

```python
"All": f"*/{(frappe.get_conf().scheduler_interval or 240) // 60} * * * *"
```

`"60" // 60` raises `TypeError`, which kills job-sync — so *every* scheduled job stops,
inbound mail included, and the only symptom is that nothing happens. `-p` parses the value
as a number. The stack's own `docker-compose.yml` uses `-gp` for `db_port` and
`socketio_port` for this reason.

Verify before restarting — the quotes are the whole story:

```
grep scheduler sites/common_site_config.json
```

`"scheduler_interval": 60` is correct. `"scheduler_interval": "60"` is broken.

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

## Intake stops dead after tickets are deleted — and never restarts

Happened on 2026-07-27 and cost an afternoon. Every health signal stayed green throughout,
which is the whole reason it is written down.

**Symptom.** Mail arrives in Outlook. Frappe creates no Communication, no ticket, no
acknowledgement. Nothing appears in Error Log — not once, ever. The scheduler ticks on
time, workers are up with zero failures, the Email Account is enabled with valid OAuth, and
`receive()` returns cleanly having fetched nothing.

**Cause.** With `Email Sync Option = ALL`, Frappe derives its IMAP watermark from the
Communications table, not from the mailbox:

```python
# email_account.py, build_email_sync_rule()
max_uid  = get_max_email_uid(self.name)      # MAX(uid) over received Communications
last_uid = max_uid + int(self.initial_sync_count or 100) if max_uid == 1 else "*"
return f"UID {max_uid}:{last_uid}"

# get_max_email_uid() with no Communications:
return 1
```

Delete the Communications — which is what clearing test tickets before go-live does — and
`max_uid` falls back to `1`, pinning the search to `UID 1:101`. Once the mailbox has moved
past UID 101 that window matches nothing, so no Communication is created, so `max_uid`
stays 1, so the window never moves. **It is a deadlock, not a delay, and it does not
self-heal.**

**Diagnosis.** These three together are conclusive, and each is one URL in a browser signed
into Desk:

| Check | Deadlocked reads |
| --- | --- |
| `Communication` list | `[]` — and confirm you are System Manager, or the empty list is just a permission filter |
| `Email Account.uidnext` vs `IMAP Folder.uidnext` | server is far ahead of the folder watermark |
| `Error Log` | nothing at all, which rules out auth and IMAP failure |

`Email Account.uidnext` is rewritten from the server on every `receive()`, so a large gap
between it and the folder's value is a direct measure of unfetched mail.

**Fix.** Set **Email Sync Option** to `UNSEEN`. That rule ignores UIDs entirely and fetches
unread mail, so it cannot depend on a table that someone may empty.

> **Mark the mailbox read first.** Widening the window ingests every unfetched message at
> once — one ticket and one acknowledgement each. There were ~52 waiting when this was
> found, and that is exactly what tripped `Helpdesk ack rate limit tripped` on 2026-07-24.

**Do not** fix it by raising `initial_sync_count`. It works, and it leaves the same trap
armed for the next person who clears test data.

## The whole site returns 404 after restarting services

Happened on 2026-07-23 and cost about an hour, so the diagnosis is written down.

**Symptom.** Every URL on the backend host returns `404 page not found` (Traefik's own page,
not Frappe's). The Next.js frontend still answers, so Traefik itself is fine. Mail keeps
flowing, because `scheduler` and the `queue-*` workers never touch Traefik or nginx.

**Chain.**

```
backend 0/1  ->  Swarm DNS has no "backend" entry
             ->  nginx: [emerg] host not found in upstream "backend:8000"
             ->  frontend (nginx) exits, 0/1
             ->  Traefik has no healthy task, DROPS the router
             ->  404 for every request
```

A 404 rather than a 502 is the tell: 502 means "route exists, upstream sick", 404 means
Traefik has no route at all, which only happens when the service has no healthy task.

**Root cause.** `restart_policy: condition: on-failure` restarts only on a NON-ZERO exit.
Gunicorn exits **0** when it shuts down on SIGTERM, so Swarm marked the task `complete`,
considered it finished successfully, and never started another. The service sat at 0/1
reporting no error. Restarting it does nothing — Swarm does not believe anything is wrong.

**Recovery.** Force a new task by scaling:

```
Portainer -> Services -> <stack>_backend -> Replicas 0 -> Apply -> Replicas 1 -> Apply
```

Then leave `frontend` alone; it is crash-looping on a non-zero exit, so it restarts by
itself as soon as `backend` resolves in Swarm DNS.

**Fixed going forward.** Every long-running service in `deploy/docker-compose.yml` now uses
`condition: any` (Docker's own default) instead of `on-failure`; the one-shot jobs keep
`condition: none`. **A stack that was deployed before that change still carries the old
policy — it only takes effect on the next stack redeploy.**

**Where to look.** Portainer -> Services -> the service -> the **Tasks** table, not Logs. A
container that never started writes no logs; the task row carries the status and error. Over
SSH the same thing, expanded:

```
docker service ps --no-trunc <stack>_backend
```

## Rollback

Mail is entirely site configuration; no code change is involved. To stop all mail flow,
untick **Enable Incoming** and **Enable Outgoing** on the Email Account. Tickets, threads
and logs are untouched.
