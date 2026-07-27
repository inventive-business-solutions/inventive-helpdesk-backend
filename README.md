# Inventive Helpdesk

**Inventive Helpdesk** is an after-sales support and ticketing platform by Inventive Business Solutions, built on [Frappe](https://frappeframework.com) **v16**. It provides support tickets backed by client, division, POC and product masters, team members and assignment groups, with role-based tenant isolation, inbound email intake, and outbound client notifications.

> Backend Frappe app — internal app name `inventive_helpdesk_backend`, Frappe module `Inventive Helpdesk`.

## Features

- **Support tickets** — full lifecycle with threaded messages, internal work notes, and collaborators.
- **Org masters** — Clients, Divisions, POCs, Products, Team Members, and Assignment Groups.
- **Role-based access** — Support Team (agents), Support Manager, and Support Client roles, with row-level tenant isolation so each client only sees their own data.
- **Email intake** — inbound email is turned into tickets and replies; clients are auto-acknowledged and notified on client-facing status changes.
- **Realtime updates** — live ticket changes pushed to the owner, team, and collaborators.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — data model, roles and permission enforcement, hooks, HTTP API, ticket numbering, invites.
- [CICD.md](CICD.md) — build pipeline, Portainer/Swarm deployment, first-deploy sequence, rollback.
- [docs/DESIGN-email-reply-workflow.md](docs/DESIGN-email-reply-workflow.md) — sender classification and what happens to a staff reply.
- [docs/RUNBOOK-production-mail.md](docs/RUNBOOK-production-mail.md) — turning on the real mailbox in production.
- [docs/RUNBOOK-backup-and-restore.md](docs/RUNBOOK-backup-and-restore.md) — what is backed up, what is not, and how to restore a site.

## Requirements

- Frappe **v16**
- Python **3.14**, Node **24**, MariaDB **10.6+**, Redis

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/inventive-business-solutions/inventive-helpdesk-backend --branch master
bench install-app inventive_helpdesk_backend
```

## Local development

```bash
# from your bench directory
bench start                          # web + workers + scheduler + redis

# first time only — create a site and install the app
bench new-site helpdesk.localhost --install-app inventive_helpdesk_backend
bench use helpdesk.localhost         # serve it on plain localhost too
```

Then open **http://helpdesk.localhost:8000/app** (or **http://localhost:8000/app**) and sign in as `Administrator`.

## Branches

- **`master`** — production (default branch)
- **`development`** — active development

## Contributing

This app uses `pre-commit` for code formatting and linting (ruff, eslint, prettier, pyupgrade):

```bash
cd apps/inventive_helpdesk_backend
pre-commit install
```

## License

mit
