# Inventive Helpdesk — Backend Architecture

Reference for the `inventive_helpdesk_backend` Frappe app: what exists, how the pieces
fit, and where the non-obvious decisions live. Frappe **v16**, Python **3.14**,
module **Inventive Helpdesk**.

For deployment see [CICD.md](../CICD.md). For setup and features see [README.md](../README.md).

---

## Data model

18 DocTypes — 11 top-level, 7 child tables.

### Support Ticket

The centre of the app. Named by a controller (not `autoname` in JSON), see
[Ticket numbering](#ticket-numbering).

| Field | Type | Notes |
| --- | --- | --- |
| `title` | Data | |
| `ticket_type` | Select | Bug, Query, Improvement, New Feature |
| `priority` | Select | Critical, High, Medium, Low |
| `status` | Select | New, Acknowledged, In Progress, Pending Client, Resolved, Closed, Reopened |
| `client` | Link → Client | tenant scope |
| `division` | Link → Division | tenant scope |
| `product` | Link → Product | optional; backfilled by `backfill_ticket_product` where the division runs exactly one |
| `raised_by` | Data | |
| `assignee` | Link → Team Member | |
| `assignment_group` | Link → Assignment Group | the owning team |
| `due_date`, `sla_risk` | Date, Check | |
| `last_activity_on` | Datetime | bumped by any message or note; drives the per-agent unread dot |
| `source` | Select | Portal, Email, Manual, API |
| `from_email` | Data | set for inbound-email tickets |
| `description` | Text | |
| `attachments` | Long Text | JSON list of `{name, url}` refs |
| `conversation` | Table → Ticket Message | client-visible thread |
| `collaborators` | Table → Ticket Collaborator | looped-in teams/members |
| `notes` | Table → Work Note | internal, never sent to clients — **permlevel 1** |
| `activity` | Table → Ticket Activity | status/priority/assignment handovers — **permlevel 1** |
| `sender_kind` | Select | Registered (POC with a login), Known Contact (matches a POC email, no login), Unregistered (no match — the `INB-` bucket), No Reply (matched a No Reply Rule). Classified in `email.py`; decides whether a reply can be sent and by which channel |
| `no_reply_reason` | Data | why the address was judged unreachable |
| `first_response_notified_on` | Datetime | stamps the one-time first-reply email, so it only ever goes once |

`notes` and `activity` sit at **permlevel 1**, and `Support Client` holds no permlevel-1
row — so Frappe strips both from a client's read of the document. That is what keeps
internal notes internal; it is not the UI declining to render them.

`sender_kind` and `no_reply_reason` are a **cache**, not the authority. A POC being
invited changes the answer without touching the ticket, so anything that must be correct
calls `sender.classify()` rather than reading the column (`sender.refresh_for_poc` is what
re-syncs them on invite).

**`assignee` and `assignment_group` move together.** A member only exists within a team,
and the backend rejects a member with no team — so writes go through `setAssignment`
(both fields in one update), not an assignee-only write.

### Org masters

| DocType | Named by | Fields |
| --- | --- | --- |
| **Client** | `client_name` | client_code, status (Onboarding/Active/On Hold/Churned), since, product *(legacy — see below)* |
| **Division** | `{client}-{division_code}` | division_name, division_code, client |
| **POC** | `email` | poc_name, phone, is_lead, client, **divisions** (child table), user, invited_on, division + is_primary *(legacy — see below)* |
| **Product** | `product_name` | — |
| **Client Product** | hash | client, product, dev_start, expected_completion, divisions (child table) |
| **Team Member** | `member_name` | email, title, status (Not Invited/Invited/Active), user |
| **Assignment Group** | `group_name` | members (child table), `lead` → Team Member (optional) |

**A POC's scope is `divisions`, the child table — not `division`.** A contact holds a
SET: one for a division POC, several for a client Lead. `permissions._poc` reads only the
table, and an empty set means **no ticket access at all**, which is the normal state of a
Lead created during onboarding before any division exists. The singular `division` column
is legacy, still written so the migration can be rolled back, and reconciled with the
table by `POC.validate` — callers that replace the set must clear it first or validate
re-appends the old value. `is_primary` is retired: `sender.reply_address` now falls back
by role (division POC first, then a Lead overseeing it) rather than reading it.

**`Client.product` is likewise legacy**, superseded by **Client Product** — a client runs
several products, each optionally scoped to divisions, with its own dates. An empty
`divisions` table there means "attached to the client as a whole", which is the only shape
available to a client with no divisions.

Both legacy columns are populated but no longer read by the code that replaced them; they
come out once the new model has run in production. See
`patches/backfill_contact_divisions_and_products.py`.

Several of these are autonamed from a human-readable field *and* are Link targets
elsewhere — so editing that field is a **rename**, not a field update. That is why
`update_client`, `update_member`, `update_product` and `update_poc` exist as endpoints
rather than being plain document writes: they call `rename_doc` so Frappe rewrites the
inbound links.

### Child tables

| DocType | Parent | Fields |
| --- | --- | --- |
| **Ticket Message** | Support Ticket | kind (`client`/`team`), author, role, message_on, body, attachments, source_communication |
| **Work Note** | Support Ticket | author, note_on, body, attachments |
| **Ticket Activity** | Support Ticket | action (Created/Status/Priority/Assignee/Team/Collaborator), old_value, new_value, author, acted_on |
| **Ticket Collaborator** | Support Ticket | party_type, team, member, added_by, added_on |
| **POC Division** | POC | division — *the contact's ticket scope* |
| **Client Product Division** | Client Product | division — empty means client-wide |
| **Assignment Group Member** | Assignment Group | member |

### Standalone records

Not child tables, but owned by a ticket rather than by the org:

| DocType | Named by | Purpose |
| --- | --- | --- |
| **Ticket Email Log** | hash | Outbound audit trail — kind, recipient, subject, message_id, delivery_state (Queued/Sent/Failed/Bounced). Reconciled from Email Queue by a scheduled job, because Email Queue writes its status with `db.set_value` and fires no doc events |
| **Ticket Read Receipt** | hash | One row per (ticket, user). Per-user, so one agent reading a reply doesn't clear the dot for the rest of the team |
| **No Reply Rule** | `pattern` | Operator-managed no-reply overrides — pattern + match_type (Exact/Prefix/Domain/Regex). Layer 1 of `sender.no_reply_reason`, and it wins over the built-in conventions |

---

## Roles and permissions

### Roles

Defined in `install.py` and created idempotently on **install and every migrate**, so a
fresh site always has them before DocPerms reference them.

| Role | Desk | Scope |
| --- | --- | --- |
| **Support Team** | yes | Agents. Work tickets; read-only on org masters. |
| **Support Manager** | yes | Adds management of clients, divisions, POCs, members, teams, products. Granted on top of Support Team. |
| **Support Client** | no | Portal only. Sees the tickets of the divisions they hold — a set, not one — plus client-level tickets carrying no division. An empty set therefore sees client-level tickets only, never the whole client. |

`Support Client` carries `desk_access = 0`, and `ensure_roles` re-asserts that on **every**
migrate rather than only at creation. Frappe auto-creates a missing role with its own
defaults (`desk_access = 1`) the moment a DocPerm references it, which happens during
migrate — so a create-only check never applied the intended value. That is not cosmetic:
desk access makes Frappe classify a portal user as a System User, which carries read
access to core doctypes, and a client POC could then list every User on the site.

### The three tiers

The roles above are not the whole story — two of the three tiers are **sets** defined in
`permissions.py`, so that the site owner can never be locked out of their own system:

| Tier | Set | Who | May |
| --- | --- | --- | --- |
| Team | `TEAM_ROLES` | Support Team, System Manager, Administrator | work tickets |
| Manager | `MANAGER_ROLES` | Support Manager + the above two | manage org masters |
| Owner ("Lead Administrator") | `OWNER_ROLES` | **System Manager, Administrator only** | delegate admin access |

The owner tier is **deliberately not a role**. The distinction already existed for a
different reason — System Manager and Administrator are unconditionally managers so the
owner cannot lock themselves out — and that is exactly the population entitled to hand out
access. A delegated manager therefore gets the full manager surface but cannot promote
anyone, making privilege escalation impossible by construction rather than by a check
someone can forget.

### How isolation is enforced

Two Frappe mechanisms, both wired in `hooks.py` → `permissions.py`:

**`permission_query_conditions`** — rewrites *list* queries so rows outside a user's
tenant never come back:

```
Support Ticket -> permissions.ticket_query
Client         -> permissions.client_query
Division       -> permissions.division_query
Client Product -> permissions.client_product_query
```

**A portal contact's ticket scope is `division IN (theirs) OR (division IS NULL AND client
= theirs)`.** The second half is what makes a client with no divisions work at all — a Lead
there holds an empty set and could otherwise not see even the tickets they raised. It also
fixes a case that was broken for everyone: `IN (...)` is never true for NULL, so a
client-level ticket was invisible to every contact on the client side, its author included.

An empty division set is still **not** a fallback to the whole client: a contact with every
division toggled off sees client-level tickets only. "Not scoped yet" and "scoped to
everything" must not be the same state, because that failure looks exactly like working.

**Client Product** follows the same shape: a client-wide engagement (empty divisions table)
is visible to every contact of that client; a division-scoped one only to a contact holding
one of its divisions.

**`has_permission`** — per-document checks, for direct access by name:

```
Support Ticket   -> ticket_has_permission
Client           -> client_has_permission   + manager_write_gate
Division         -> division_has_permission + manager_write_gate
POC              -> manager_write_gate
Product          -> manager_write_gate
Client Product   -> manager_write_gate
Team Member      -> manager_write_gate
Assignment Group -> manager_write_gate
No Reply Rule    -> manager_write_gate
Ticket Read Receipt -> own_read_receipt_gate
Client Product   -> manager_write_gate + client_product_has_permission
```

`manager_write_gate` covers **all seven** org masters, not just the tenant-scoped ones.
That matters because the DocPerms grant Support Team full CRUD on every master — the gate
is the only thing stopping an agent creating or deleting Products, Team Members and
Assignment Groups. The manager-only API endpoints are not the enforcement point: a client
of this backend can reach the same doctypes through `/api/resource/*`, bypassing them
entirely, so the check has to live in the permission layer.

**Every master needs its own line here — a missing one fails open, silently.** Client
Product shipped without one and was, for as long as that lasted, the single master any
agent could create, edit and delete. Nothing surfaced it: the endpoints still called
`_require_manager`, so the UI behaved correctly and only a direct REST call showed the
gap. `tests/test_manager_gate.py` now asserts the rule over the whole master list, both
behaviourally and structurally, so the next master added without a gate fails the suite.

Both layers matter. The query conditions stop a client seeing others' rows in a list;
`has_permission` stops them fetching one by guessing its name. `manager_write_gate` is
what gives agents read access to org masters while reserving writes for managers.

---

## Ticket numbering

`support_ticket.py` → `autoname()`.

Three prefixes, chosen by how much is known about the sender:

| Ticket has | Prefix | Meaning |
| --- | --- | --- |
| client **and** division | `{client_code}-{division_code}-####` | fully scoped |
| client, no division | `{client_code}-####` | known client, division not yet identified |
| neither | `INB-####` | sender matched no registered contact |

The client-only form exists because everything unscoped once shared `INB-`, which made a
known client's ticket indistinguishable from an unidentified one and put every such client
on a single global counter. **`INB-` now means only what its name says**, which makes
"is this ticket from a stranger?" answerable as `client IS NULL` — the discriminator any
cleanup or reporting script should use.

The counter comes from Frappe's `tabSeries` via `getseries()`, which uses
`SELECT ... FOR UPDATE`. That is deliberate: it is atomic under concurrent inserts,
unlike a `max(existing)+1` scan, and the increment shares the insert's transaction so a
failed insert rolls the number back too.

`_ensure_series_floor()` handles a prefix's **first** use on a site that already has
tickets (seeded or backfilled data), setting the counter above the highest existing
number. It counts only suffixes that are **entirely digits**: a `LIKE` alone is too loose
now that a client-only prefix exists, because `MSFT-%` also matches `MSFT-AZU-0001`, and
seeding a client's counter from its divisions' tickets would make the numbering jump for
no visible reason.

It returns early when the `tabSeries` row already exists, so it seeds **once** and never
corrects a counter afterwards — which is why zeroing a counter by hand sticks (see
[Operating hazards](#resetting-the-ticket-counter)). Because a later explicit-name insert
can still overshoot, the autoname also skips forward past collisions rather than failing.

---

## Automatic behaviour

All wired in `hooks.py`.

### Document events

| Trigger | What happens |
| --- | --- |
| **Support Ticket** `after_insert` | Acknowledgement email to the client (`email.send_ticket_ack`), plus `realtime.publish_ticket_update` so open list/board views show it without waiting for the 30s poll |
| **Support Ticket** `on_update` | Client emailed on client-facing status changes — Resolved / Pending Client (`email.on_ticket_update`) — plus `realtime.publish_ticket_update`, nudging owner, team and collaborators |
| **Communication** `after_insert` | Inbound email becomes a ticket or a reply (`email.on_communication`) |
| **Communication** `on_update` | Attachments that arrive after the body are moved onto the ticket (`email.on_communication_update`). Without it a file existed only in the desk, on a Communication no agent opens |

### Scheduled jobs

| Cron | Job |
| --- | --- |
| `*/2 * * * *` | `frappe.email...email_account.pull` — inbound mail. **This is how tickets arrive**; if it stops, intake stops |
| `*/5 * * * *` | `email.reconcile_email_log` — mirrors Email Queue outcomes onto Ticket Email Log |

The reconcile job is a poll rather than a hook because Email Queue writes its status with
`frappe.db.set_value`, which fires no doc events — there is nothing to subscribe to.

> A cron entry is a **ceiling, not a guarantee**: jobs only fire as often as the scheduler
> ticks. That needs **two** site config keys set, not one — see
> [RUNBOOK-production-mail.md](RUNBOOK-production-mail.md).

### Other hooks

| Hook | Target |
| --- | --- |
| `after_install`, `after_migrate` | `install.ensure_roles`, `install.ensure_link_expiry` — both idempotent, both re-run on every migrate |

### Realtime

`realtime.py` emits two events, both `after_commit` so a re-fetch reads committed data:

- **`ticket_update`** → the doc room `doc:Support Ticket/<name>`. Joining it runs Frappe's
  `can_subscribe_doc` → our `ticket_has_permission`, so only the owner, owning team and
  collaborators receive it.
- **`ticket_list_dirty`** → the doctype room `doctype:Support Ticket`. A contentless ping,
  so it can fan out to every subscribed session without leaking a ticket someone may not
  see.

The doctype room must be passed explicitly as `room=get_doctype_room(...)`.
`publish_realtime` only derives it for the built-in `list_update` event; a custom event
with `doctype` but no `docname` silently falls through to the site room, which only
System Users join — meaning portal users would never receive it.

---

## HTTP API

Every mutating endpoint is `methods=["POST"]`: without it Frappe defaults to allowing GET,
which both skips CSRF validation and causes the write to be rolled back rather than
committed.

Endpoint names below are relative to **`inventive_helpdesk_backend.api`** unless a module
is written out. The full call path is
`/api/method/inventive_helpdesk_backend.api.<name>`.

### Tickets
| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `add_message` | Client-visible message. The ticket's POC or staff. |
| POST | `add_note` | Internal work note. Staff only. |
| POST | `reopen` | Owning client or staff reopens a resolved/closed ticket. |
| POST | `claim_ticket` | Agent self-assigns from their team's queue. |
| POST | `add_collaborator` / `remove_collaborator` | Loop a team or member onto a ticket. |
| POST | `upload_attachment` | Multipart upload, stored as a private file scoped to the ticket. |
| POST | `mark_ticket_read` | Stamps a Ticket Read Receipt for the caller. |
| GET | `unread_tickets` | Unread count for the caller — drives the sidebar badge. |
| GET | `ticket_stats` | Dashboard aggregates, scoped to what the caller may see. |

### Org management (manager-gated)
| Method | Endpoint |
| --- | --- |
| POST | `update_client`, `update_member`, `update_product`, `update_poc`, `delete_poc` |
| POST | `update_group` | Renames a team and/or sets its lead. **Rename first, then the lead** — the doctype is autonamed by `group_name`, so a rename changes the docname the lead write has to address. `lead=None` leaves it alone, `""` clears it. A named lead is added to the team if absent. |
| POST | `create_contact`, `set_contact_divisions` |
| POST | `create_client_product`, `update_client_product`, `delete_client_product` |
| POST | `delete_product` |
| POST | `invite_poc`, `invite_member` |

### Admin delegation (owner-gated)

Guarded by `_require_owner`, **not** `_require_manager` — a delegated Support Manager holds
the full manager surface but cannot promote anyone. That keeps privilege escalation
impossible by construction rather than by a check someone can forget.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `list_admins` | Current Lead Administrators and delegated admins. |
| GET | `admin_candidates` | Team Members eligible for promotion. |
| POST | `invite_admin` | Provision an admin directly, without a Team Member record first. |
| POST | `set_member_admin` | Grant or revoke `Support Manager` for one member. |
| POST | `revoke_account` | Disable a login — **also clears `reset_password_key`**, so an outstanding invite cannot let them back in. |

### Session and infrastructure
| Method | Endpoint | Notes |
| --- | --- | --- |
| GET | `me` | Role, tenant scope, CSRF token. **GET by design** — it must work before a CSRF token is held, right after login. |
| GET | **`health.check`** | Health + `build_sha`. Guest-callable; used by CI to verify a deploy is running the expected commit. Lives in `health.py`, **not** `api.py` — `api.check` does not exist and returns a 500 that reads like the site is broken. |
| POST | `request_password_reset` | Guest. Issues a reset key and mails the branded link. Rate-limited. |
| POST | `password_link_status` | Guest. Reports `valid` / `expired` / `revoked` / `invalid` for a set-password key **without consuming it**, plus the support inbox to contact. Rate-limited 30/hr. |
| POST | `set_password_with_key` | Guest. Redeems a set-password key, refusing any state but `valid`. Rate-limited 20/hr. |

---

## Invites and onboarding

`invite_poc` and `invite_member` both provision a login, link it back to the record, and
email a set-password link. Both are idempotent — safe to call again to resend.

- **POC** → Website User + Support Client role. `invited_on` is restamped and
  `activated_on` cleared on every send.
- **Team Member** → System User + Support Team role, status reset to Invited.

**Activation means "chose a password", not "signed in".** Both shapes are stamped by
`_mark_activated`, called from `set_password_with_key` — staff via `Team Member.status`,
contacts via `POC.activated_on`. A resend clears it, so the new link has to be redeemed
before either reads Active again.

This used to be inferred from signing in — an `on_login` hook for staff, a
`last_login` vs `invited_on` compare for contacts — which answered the wrong question
(an invite is not outstanding just because someone has not been back lately) and let a
resent invite be defeated with the old password. The hook is gone. The `last_login`
compare survives only as a fallback in `pocPortalStatus`, for rows the backfill could
not date and for a frontend running against an unmigrated backend.

**Email delivery is best-effort by design.** `_send_invite_mail` catches
`OutgoingEmailError` and returns `False`, so a site with no outgoing mail account still
creates the account — the endpoint returns `{"email_sent": false}` without raising. That
means a missing Email Account fails *silently*; check `email_sent` if invites appear not
to arrive.

The branded link requires **`app_url`** in site config. Without it the code falls back to
Frappe's generic welcome mail, which points at the Frappe desk rather than the app's
`/set-password` page.

### Link lifetime and redemption

Frappe has **one** setting for set-password link expiry, and the two link types need
different answers. An invite is opened whenever the recipient next reads their mail; a
password reset should be short-lived. So the window is derived per key rather than
configured:

| Link | Window | Derived from |
| --- | --- | --- |
| Invite | `INVITE_LINK_TTL_HOURS` = **24h** | `User.last_password_reset_date` is unset |
| Reset | `RESET_LINK_TTL_HOURS` = **1h** | `last_password_reset_date` is set |

`install.ensure_link_expiry` raises Frappe's `reset_password_link_expiry_duration` to 24h
(never lowers it) so the framework does not expire an invite early; the tighter reset
window is enforced per-key in `api._resolve_password_key`. No new field and no cache
entry — a Redis restart cannot turn live invites into dead ones.

`_resolve_password_key` returns one of four states, and **`revoked` is the security-relevant
one**: Frappe's own `update_password` never checks `enabled` and calls `login_as()`, so a
disabled user holding an old link could otherwise walk back in.

| State | Meaning |
| --- | --- |
| `valid` | within window, account enabled |
| `expired` | past the window for its kind |
| `revoked` | account disabled — `revoke_account` also clears `reset_password_key` |
| `invalid` | no such key (keys are stored sha256-hashed and are single-use) |

Two endpoints back this. `password_link_status` is a **pre-flight check that must not
consume the key** — mail scanners (Outlook Safe Links, Defender ATP) fetch every URL in a
message before the human clicks it, and a consuming check would burn the link in transit.
`set_password_with_key` wraps Frappe's `update_password`, refusing anything that is not
`valid` and converting its string-return failure signal into a real exception.

> The frontend proxy no longer forwards Frappe's raw `update_password`. The Frappe host is
> separately reachable, though, so that endpoint still honours the 24h framework window for
> anyone calling it directly — closing that means fronting the desk, not another guard here.

---

## Migration patches

`patches.txt`, run by `bench migrate`:

| Section | Patch | Purpose |
| --- | --- | --- |
| pre_model_sync | `convert_child_timestamps` | Must run before the varchar→DATETIME column sync |
| pre_model_sync | `clear_legacy_client_product` | Retires `Client.product`, superseded by the Client Product model. **Must stay pre_model_sync** — the same release drops the DocField, and once it is gone the value is unreachable through the ORM |
| post_model_sync | `disable_orphaned_logins` | Disables logins whose directory record no longer exists |
| post_model_sync | `fix_uninvited_member_status` | Corrects Team Members left in the wrong status by the pre-`on_login` activation flow |
| post_model_sync | `backfill_sender_kind` | `sender_kind` is computed in `before_save`, so tickets predating the field are blank — and blank shows the agent nothing at the moment they need to know whether a reply can be sent |
| post_model_sync | `report_duplicate_directory_emails` | **Reports only, changes nothing.** Before `access.assert_email_unclaimed`, `Team Member.email` had no uniqueness (the doctype is named by `member_name`), so duplicates could exist |
| post_model_sync | `backfill_contact_divisions_and_products` | Moves to the divisions-table / Client Product model. Three idempotent backfills, deliberately non-lossy — originals are left in place so a rollback costs nothing |
| post_model_sync | `backfill_ticket_product` | Tags pre-existing tickets with a product where the division runs exactly one, so there is no guess involved |
| post_model_sync | `backfill_activation_state` | Seeds stored activation (`POC.activated_on`, `Team Member.status`) from `last_login`. Without it every already-activated person reads Invited the moment activation stops being inferred from signing in — and the obvious remedy, resending the invite, is exactly wrong |

---

## Tests

197 tests across 15 modules in `tests/`, plus 46 more alongside the doctypes they cover —
243 in total. Run with:

```bash
bench --site <site> run-tests --app inventive_helpdesk_backend
```

They use `frappe.tests.IntegrationTestCase` (not the deprecated
`frappe.tests.utils.FrappeTestCase`, which is scheduled for removal in v17). Coverage
centres on the parts most likely to break silently: per-division autoname sequencing,
tenant isolation (a client cannot read a foreign ticket, work notes are stripped from
client reads), reply threading, the invite flows, and set-password link states.

Two are worth knowing about because they guard against failures nothing else catches:

- `test_translator_not_shadowed.py` walks the AST of every module for a rebinding of `_`.
  In this app `_` is frappe's translator, and it is also Python's conventional throwaway —
  `_, status = f()` then makes the next `_("…")` call a document. Ruff and typing are both
  happy with it; it only fails at runtime, on an error path, in front of a user. That exact
  line reached a release pipeline once.
- `test_password_link.py` covers all four link states including `revoked`, which is the one
  that keeps a disabled account from walking back in through an old invite.

Testing must be enabled on the site first:

```bash
bench --site <site> set-config allow_tests true
```

Note that `IntegrationTestCase.setUpClass` creates Frappe's standard test-record
fixtures — around nine `test*@example.com` users — and leaves them behind. Expected, and
recreated on each run.

---

## Development helpers

`seed.py` generates demo data. It refuses to run unless `developer_mode` is enabled,
because it creates users with fixed passwords:

```bash
bench --site helpdesk.localhost execute inventive_helpdesk_backend.seed.run
```

---

## Operating hazards

Things that have actually cost time here, and are not deducible from the code. Deployment
lives in [CICD.md](../CICD.md); mail intake and restore have their own runbooks. This is
the residue — the traps that sit between them.

### "There is no delete option" almost always means you are not Administrator

Frappe short-circuits the whole permission system for the Administrator account before it
ever reads a permission row (`frappe/permissions.py:107`), and `get_role_permissions`
returns `allow_everything()` for it too — which is what the desk uses to decide whether to
*render* a button. So Administrator can already do anything, on every doctype, regardless
of what the JSON says.

**"Administrator" is a specific user account, not the System Manager role.** A System
Manager is an ordinary user and gets exactly the rights in the permission rows. This is the
usual explanation for a missing button, and the usual wrong fix is to grant the right to a
role — which hands it to everyone holding that role, permanently.

Editing permissions through the Role Permissions Manager also has a lasting side effect:
`setup_custom_perms()` copies the shipped rows into `tabCustom DocPerm` and from then on
**the app's JSON permissions are ignored for that doctype on every migrate.** "Restore
Original Permissions" on the same page is the undo.

### Ticket Email Log is read-only on purpose

It is the only non-child doctype in the app without a delete permission, and the only
record that outlives Frappe's 30-day Email Queue purge — so it is the only thing that can
answer *"did we ever actually tell the customer?"* about anything older than a month.
Granting Delete to Support Team hands that eraser to every agent, including the one whose
sent mail is in dispute. Use `bench console` for a one-off instead; it runs as
Administrator and bypasses the restriction without leaving a permanent hole.

### Never delete a Communication whose ticket still exists

Communication is load-bearing for mail in two independent ways:

1. **Reply threading.** `_anchor_outgoing` writes it precisely so a client's reply still
   finds its ticket after the Email Queue row is purged at 30 days. It is deliberately
   absent from `default_log_clearing_doctypes`.
2. **Inbound de-duplication.** `receive.py:765` `is_exist_in_system()` dedups incoming mail
   against Communication by `message_id`.

Delete one while its ticket is alive and the next client reply forks into a **brand new
ticket**. Delete a lot of them and, if the mailbox's `UIDVALIDITY` ever changes, Frappe
re-syncs the last 100 messages with nothing left to dedup against.

The safe invariant, for any cleanup script: **a Communication is only ever deleted if its
own ticket is being deleted in the same operation.** Deleting a ticket normally does *not*
remove its Communications — `clear_references` only nulls `reference_name`, so they survive
as orphans.

See also the deadlock in [RUNBOOK-production-mail.md](RUNBOOK-production-mail.md): with
`Email Sync Option = ALL`, an empty Communications table pins the IMAP watermark at
`UID 1:101` **forever**, silently, with every health signal green.

### Deleting does not mean gone

`delete_doc(force=True)` still archives the full document JSON into `tabDeleted Document`.
Only `delete_permanently=True` skips that (`frappe/model/delete_doc.py:214`). Desk deletes
never pass it. So after "deleting" a ticket, its contents are still on the site.

### bench console

- It does **not** autocommit. Nothing persists without an explicit `frappe.db.commit()`.
  `bench mariadb` does autocommit, which is exactly why this catches people out.
- IPython mangles pasted multi-line blocks. Paste **one line at a time**, or write a file
  and run `exec(open(f).read(), {})` with the explicit globals dict.
- The Portainer web console silently drops large pastes — no error, the text simply never
  arrives. Keep lines short there.

### Resetting the ticket counter

`tabSeries` has no desk UI. `_ensure_series_floor` seeds a prefix only on its **first** use
(it returns early when the row already exists), so zeroing an existing row sticks and will
not be pushed back up. Deleting the row instead makes the next ticket re-seed the floor
from surviving tickets.

```python
frappe.db.sql("update `tabSeries` set current = 0 where name = 'INB-'")
frappe.db.commit()
```

Verify with `select name, current from \`tabSeries\``, then prove it end to end by mailing
in from an address that is **not** a registered contact — a known contact is named from a
different counter, so it does not test what you think it tests.

### Release order when the frontend depends on a new endpoint

Deploy the **backend first** and verify the endpoint answers in production before the
frontend that calls it goes out. The reverse order ships a UI whose requests 404. On the
frontend side a new endpoint also needs **three** allowlists updated, not one — the
`next.config.mjs` rewrites, the `proxy.ts` matcher, and the caller.
