# Runbook — backup and restore

What is protected today, what is not, and how to actually get a site back. Every command
here is transcribed from `frappe/commands/site.py` and from a backup run against a real
bench, not from documentation.

> ## The short version
>
> As of `fix(deploy): back up the files...`, the scheduled backup captures the database
> **and** the attachments. What it still does not do is survive the loss of the machine it
> runs on, or reach back further than a day.
>
> | | Status |
> | --- | --- |
> | Database captured | yes |
> | Ticket attachments captured | yes — since `--with-files` |
> | Survives losing the Swarm node | **no** — backups are written to the volume they back up |
> | Can recover from a problem noticed a week later | **no** — 23-hour retention |
> | Restore procedure rehearsed | **no** — see §4 |

---

## 1. What runs now

`deploy/docker-compose.yml`, service `backup-sites`:

```yaml
command:
  - bench --site all backup --with-files
```

Driven by swarm-cronjob at `0 */6 * * *` — four times a day. Each run writes four files
into `sites/<site>/private/backups/`:

| File | Contains |
| --- | --- |
| `*-database.sql.gz` | the whole database |
| `*-files.tar` | `public/files` — logos and other non-private uploads |
| `*-private-files.tar` | `private/files` — **ticket attachments** |
| `*-site_config_backup.json` | the site config, minus secrets |

**Retention is automatic and short.** `new_backup()` calls `delete_temp_backups()` on every
run (`frappe/utils/backups.py:625, :651`), which deletes anything in the backup directory
older than `keep_backups_for_hours` — **default 23**, and currently unset, so the default
applies. At four runs a day that means roughly the last four sets and nothing older.

This was confirmed, not assumed: a backup run on 2026-07-26 deleted three sets dated
2026-07-24 without being asked to.

---

## 2. The two gaps, stated precisely

### 2.1 The backups live on the volume they back up

Both the live site and every backup of it are on the single `sites` Docker volume, on the
single Swarm node. Losing that volume — disk failure, a bad `docker volume rm`, a host
rebuild — loses the site and its entire backup history in one step. The backups protect
against damage *inside* a healthy site (a bad migration, a mistaken bulk delete, a corrupt
table). They do not protect against losing the host, which is the scenario most people
picture when they hear "we have backups".

### 2.2 Nothing survives past a day

The 23-hour prune means the oldest recoverable state is yesterday. Any problem noticed
later than that — a bad patch that quietly corrupted data on Friday and was spotted on
Monday, a customer asking for a ticket deleted last week — has nothing to restore from.

Raising `keep_backups_for_hours` addresses this on its own, at a cost of disk: each
retained set carries a full copy of the file trees, so seven days at four runs a day is
28 sets rather than four.

---

## 3. The destination — Google Drive

The chosen direction is a Google Drive folder, pushed with `rclone` from a sidecar that
runs after each backup. That closes gap 2.1: the bytes leave the Swarm node entirely.

Three things have to be settled before it is built, because each one changes the
implementation.

### 3.1 Which kind of Google account

This is the decision that most often gets made by accident and hurts later.

| | Works? | Why |
| --- | --- | --- |
| **Workspace Shared Drive + service account** | **yes — preferred** | A service account authenticates unattended, with no token to re-approve and no dependency on one person's login. It can own files in a Shared Drive. |
| Workspace My Drive + service account | no | A service account has **zero storage quota of its own**, so it cannot own files in a personal My Drive folder — uploads fail with a quota error even though the folder is shared with it. This is the classic Drive-automation trap. |
| Personal Gmail + OAuth token | works, but | The token is tied to an individual. If they leave, change password or revoke access, backups stop — silently, since nothing is watching. Consumer accounts also share one 15 GB quota across Gmail, Drive and Photos. |

Note that this org runs **Microsoft 365** for mail (see RUNBOOK-production-mail.md). If the
Drive folder is a personal account rather than a Workspace one, then the company's disaster
recovery depends on an individual's personal cloud storage. That is a business-continuity
decision, not a technical one, and it should be made deliberately rather than inherited
from whoever set it up.

### 3.2 Encryption is not optional here

A backup set is not an opaque blob. It contains:

- every ticket's full text and conversation, for real named third-party clients;
- contact names, email addresses and phone numbers for those clients' staff;
- the `__Auth` table — password hashes for every user of the system.

Putting that in third-party cloud storage unencrypted is a materially different act from
keeping it on a volume in Microscan's rack. Frappe encrypts backups natively (gpg, via
`bench backup --encrypt-backup`), so this costs one flag.

> ### The trap that makes encrypted off-site backups useless
>
> `get_or_generate_backup_encryption_key()` (`frappe/utils/backups.py:696`) generates the
> key on first use and stores it in **`site_config.json`** — which lives on the `sites`
> volume. That is the volume the off-site copy exists to survive the loss of.
>
> Lose the node and you are left with backups in Drive that nothing on earth can decrypt.
>
> **The key must be copied into a password manager the moment encryption is enabled**, and
> recorded as a recovery prerequisite. An encrypted backup whose key died with the server
> is worse than no backup, because it looks like protection right up until it is needed.

### 3.3 Retention at the destination

Set Drive-side retention to **30 days**. That is what makes §2.2 moot regardless of the
local 23-hour prune, and it is the difference between "we can go back to yesterday" and
"we can go back to before the problem started".

Drive's own trash retains deleted files for 30 days on top of this — useful, but do not
rely on it as the retention policy.

### 3.4 Sizing

Measure rather than estimate, on the server:

```bash
# live data, which is what one backup set costs (the compressed db will be smaller)
docker run --rm -v <stack>_sites:/s alpine du -sh /s/<site>/private/files /s/<site>/public/files
docker exec $(docker ps -qf name=_backend) bash -lc \
  'ls -la sites/*/private/backups | tail'
```

**Capacity needed ≈ one set × 120** (four runs a day × 30 days). Check that against the
Drive quota before enabling, not after — a quota-full Drive fails the upload, and unless
something is watching, it fails quietly.

### 3.5 What still is not covered

Google Drive closes gap 2.1 and, with 30-day retention, gap 2.2. It does **not** make the
restore work — that is §4, and it has never been rehearsed. Getting bytes into Drive is the
easy half.

---

## 4. Restoring — the procedure that has never been run

> **This has not been rehearsed.** An unrehearsed restore is a plan, not a capability. The
> first time anyone runs these commands should not be during an outage.

Flags verified against `frappe/commands/site.py:159-175`.

```bash
# On the Swarm node, into a bench shell:
docker exec -it $(docker ps -qf name=_backend) bash

cd /home/frappe/frappe-bench
ls -la sites/<site>/private/backups/          # pick a timestamp; all four share it

bench --site <site> restore \
  sites/<site>/private/backups/<STAMP>-database.sql.gz \
  --with-public-files  sites/<site>/private/backups/<STAMP>-files.tar \
  --with-private-files sites/<site>/private/backups/<STAMP>-private-files.tar \
  --db-root-username root \
  --db-root-password "$DB_ROOT_PASSWORD"

bench --site <site> migrate        # the backup may predate the deployed code
bench --site <site> clear-cache    # or the desk renders against stale asset hashes
```

**`restore` drops and recreates the database.** It is not additive and there is no undo.
Take a fresh backup of the current state first, however broken it looks — the state you
are about to discard may still hold something the older backup does not.

**Both file flags matter.** Omitting `--with-private-files` restores the database with
every ticket's attachment links intact and no files behind them — the same failure the
`--with-files` fix removed from the backup side. Getting it right on the way out and wrong
on the way back in produces an identical result.

---

## 5. The drill

Quarterly, and after any change to this file. It is the only thing that converts §4 from
documentation into a capability.

1. Copy the four newest backup files off the node.
2. Restore them into a **scratch** site (`bench new-site restore-drill.localhost`, then
   `bench --site restore-drill.localhost restore ...`) — never over production.
3. Check three things, in this order:
   - the ticket count matches what production reported at that timestamp;
   - a ticket's **attachment actually downloads**, not merely appears in the list — this
     is the specific failure this whole runbook exists to prevent;
   - a client POC login sees only their own division's tickets, and no work notes.
4. Record the date and the restore duration below. The duration is the number to quote
   when someone asks how long an outage would last.
5. `bench drop-site restore-drill.localhost`.

| Date | Backup timestamp restored | Duration | Outcome |
| --- | --- | --- | --- |
| _(never run)_ | | | |

---

## Related

- [CICD.md](../CICD.md) — deploy pipeline and the Portainer stack
- [RUNBOOK-production-mail.md](RUNBOOK-production-mail.md) — mail configuration
- `deploy/docker-compose.yml` — the `backup-sites` service and its schedule
