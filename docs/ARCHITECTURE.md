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
| **Assignment Group** | `group_name` | members (child table) |

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
| **Support Client** | no | Portal only. Sees the tickets of the divisions they hold — a set, not one. An empty set sees nothing. |

### How isolation is enforced

Two Frappe mechanisms, both wired in `hooks.py` → `permissions.py`:

**`permission_query_conditions`** — rewrites *list* queries so rows outside a user's
tenant never come back:

```
Support Ticket -> permissions.ticket_query
Client         -> permissions.client_query
Division       -> permissions.division_query
```

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

- Scoped tickets: `{client_code}-{division_code}-####`
- Unscoped inbound email: `INB-####`

The counter comes from Frappe's `tabSeries` via `getseries()`, which uses
`SELECT ... FOR UPDATE`. That is deliberate: it is atomic under concurrent inserts,
unlike a `max(existing)+1` scan, and the increment shares the insert's transaction so a
failed insert rolls the number back too.

`_ensure_series_floor()` handles a prefix's **first** use on a site that already has
tickets (seeded or backfilled data), setting the counter above the highest existing
number. Because a later explicit-name insert can still overshoot the counter, the
autoname also skips forward past collisions rather than failing.

---

## Automatic behaviour

All wired in `hooks.py`.

### Document events

| Trigger | What happens |
| --- | --- |
| **Support Ticket** `after_insert` | Acknowledgement email to the client (`email.send_ticket_ack`), plus a realtime list ping so open list/board views show it without waiting for the 30s poll |
| **Support Ticket** `on_update` | Client emailed on client-facing status changes — Resolved / Pending Client (`email.on_ticket_update`) — plus a realtime nudge to owner, team and collaborators |
| **Communication** `after_insert` | Inbound email becomes a ticket or a reply (`email.on_communication`) |

### Other hooks

| Hook | Target |
| --- | --- |
| `on_login` | `api.activate_member_on_login` — flips an invited Team Member to Active on their first real sign-in |
| `after_install`, `after_migrate` | `install.ensure_roles` |

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

18 whitelisted endpoints. Every mutating one is `methods=["POST"]`: without it Frappe
defaults to allowing GET, which both skips CSRF validation and causes the write to be
rolled back rather than committed.

### Tickets
| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `add_message` | Client-visible message. The ticket's POC or staff. |
| POST | `add_note` | Internal work note. Staff only. |
| POST | `reopen` | Owning client or staff reopens a resolved/closed ticket. |
| POST | `claim_ticket` | Agent self-assigns from their team's queue. |
| POST | `add_collaborator` / `remove_collaborator` | Loop a team or member onto a ticket. |
| POST | `upload_attachment` | Multipart upload, stored as a private file scoped to the ticket. |

### Org management (manager-gated)
| Method | Endpoint |
| --- | --- |
| POST | `update_client`, `update_member`, `update_product`, `update_poc`, `delete_poc` |
| POST | `invite_poc`, `invite_member` |

### Session and infrastructure
| Method | Endpoint | Notes |
| --- | --- | --- |
| GET | `me` | Role, tenant scope, CSRF token. **GET by design** — it must work before a CSRF token is held, right after login. |
| GET | `check` | Health + `build_sha`. Guest-callable; used by CI to verify a deploy is running the expected commit. |
| POST | `password_link_status` | Guest. Reports `valid` / `expired` / `revoked` / `invalid` for a set-password key **without consuming it**, plus the support inbox to contact. Rate-limited 30/hr. |
| POST | `set_password_with_key` | Guest. Redeems a set-password key, refusing any state but `valid`. Rate-limited 20/hr. |

---

## Invites and onboarding

`invite_poc` and `invite_member` both provision a login, link it back to the record, and
email a set-password link. Both are idempotent — safe to call again to resend.

- **POC** → Website User + Support Client role. `invited_on` is restamped on every send;
  the UI compares `last_login` against it, so a stale resend cannot read "Active" off a
  login that predates it.
- **Team Member** → System User + Support Team role, status set to Invited. Activation is
  event-driven via `on_login` rather than a timestamp compare.

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
| post_model_sync | `disable_orphaned_logins` | |
| post_model_sync | `fix_uninvited_member_status` | |

---

## Tests

187 tests across 15 modules, run with:

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
