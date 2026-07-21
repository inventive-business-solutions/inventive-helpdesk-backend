# CI/CD — Inventive Helpdesk Backend

Frappe app deployed on Docker Swarm via Portainer CE, behind Traefik. Adapted from the
`tei-suit-backend` template and running on the same ARM64 infrastructure.

> **Scope:** this pipeline deploys the **Frappe backend only**. The `frontend` service in
> `deploy/docker-compose.yml` is Frappe's own nginx (serves desk assets, proxies to
> `backend:8000`) — it is *not* the Next.js app. That is a separate stack.

---

## Hosts

| Host | Serves | Deployed by |
| --- | --- | --- |
| `helpdeskfrappe.inventivebizsol.co.in` | Frappe backend | this repo |
| `helpdesk.inventivebizsol.co.in` | Next.js frontend | `inventive-helpdesk-frontend` |

Both resolve to `43.242.225.160`. Traefik routes by Host header and issues TLS via the
`le` certresolver, so certificates are automatic.

---

## Architecture

**Branch flow:** day-to-day work is committed to `development`. Releases are made by
merging `development` → `master`, which is the branch the hosted environment tracks.

```
merge development -> master  (release)
       │
       ▼
GitHub Actions (self-hosted, dev-arm64)
       ├── build image from frappe_docker Containerfile (linux/arm64)
       ├── push to GHCR (:<sha> and :latest)
       ├── POST Portainer webhook
       └── poll https://<SITE_NAME>/api/method/ping until HTTP 200
       ▼
Portainer CE → Docker Swarm
       ├── re-pull image, redeploy services
       └── migration service: bench --site all migrate
```

Build args pin the stack to match local dev: Frappe `version-16`, Python `3.14.0`,
Node `24.1.0`.

---

## Server prerequisites

The compose file declares these as **external** — it will not create them:

| Resource | Notes |
| --- | --- |
| `traefik-public` network | Traefik with the `le` certresolver |
| `mariadb-network` network | MariaDB reachable as `mariadb_db:3306`, plus its root password |
| Docker Swarm + Portainer CE | stack with GitOps webhook |
| Runner labelled `dev-arm64` | self-hosted; image is `linux/arm64` only |

---

## GitHub configuration

### Variables (per environment)

| Variable | Development | Production |
| --- | --- | --- |
| `IMAGE_NAME` | `ghcr.io/inventive-business-solutions/inventive-helpdesk-backend` | `ghcr.io/inventive-business-solutions/prod-inventive-helpdesk-backend` |
| `SITE_NAME` | `helpdeskfrappe.inventivebizsol.co.in` | _(prod domain)_ |

### Secrets (per environment)

| Secret | Notes |
| --- | --- |
| `APPS_JSON_BASE64` | Base64 apps.json containing a classic PAT — **a secret, not a variable**, so it stays masked in run logs |
| `PORTAINER_WEBHOOK_URL` | From the Portainer stack's GitOps webhook |

Generate `APPS_JSON_BASE64` with a **classic** PAT scoped to `repo` only
(fine-grained PATs are per-repo and `GITHUB_TOKEN` does not work inside BuildKit for
private repos). The `branch` here must match the environment — `master` for Production:

```bash
# Production (the hosted branch)
echo -n '[{"url":"https://<PAT>@github.com/inventive-business-solutions/inventive-helpdesk-backend.git","branch":"master"}]' | base64 -w 0

# Development
echo -n '[{"url":"https://<PAT>@github.com/inventive-business-solutions/inventive-helpdesk-backend.git","branch":"development"}]' | base64 -w 0
```

### Setup commands

The hosted environment is **Production** (branch `master`):

```bash
REPO=inventive-business-solutions/inventive-helpdesk-backend

gh api repos/$REPO/environments/Production -X PUT

gh variable set IMAGE_NAME --repo $REPO --env Production \
  --body "ghcr.io/inventive-business-solutions/prod-inventive-helpdesk-backend"
gh variable set SITE_NAME  --repo $REPO --env Production \
  --body "helpdeskfrappe.inventivebizsol.co.in"

gh secret set APPS_JSON_BASE64      --repo $REPO --env Production --body "<base64-master-value>"
gh secret set PORTAINER_WEBHOOK_URL --repo $REPO --env Production --body "<webhook-url>"
```

Add the Development environment later if you want a second stack tracking `development`;
it needs its own Portainer stack, webhook and `SITE_NAME`.

---

## Portainer stack

1. **Stacks → Add Stack → Repository**
2. Repository URL: `https://github.com/inventive-business-solutions/inventive-helpdesk-backend`
3. Authentication: ON (GitHub username + PAT)
4. Repository reference: `refs/heads/master` — this is the hosted branch
5. Compose path: `deploy/docker-compose.yml`
6. GitOps updates: ON → **Webhook**, enable *Re-pull image* + *Force redeployment*
7. Copy the webhook URL into the `PORTAINER_WEBHOOK_URL` secret
8. Add environment variables from `deploy/.env.example`
9. Deploy the stack

---

## First deployment

Set these in Portainer for the initial run:

```
CONFIGURE=1
CREATE_SITE=1
MIGRATE=0
SITE_NAME=helpdeskfrappe.inventivebizsol.co.in
ADMIN_PASSWORD=<choose>
DB_ROOT_PASSWORD=<mariadb root password>
INSTALL_APP_ARGS=--install-app inventive_helpdesk_backend
```

Once the site exists, switch to steady state:

```
CONFIGURE=0
CREATE_SITE=0
MIGRATE=1
```

### Post-create configuration (required — not handled by the configurator)

The `configurator` service only writes db/redis/socketio settings. Two things this app
needs must be set manually on the site, once:

```bash
# Invite emails link here. Without it, _send_invite_mail falls back to Frappe's generic
# welcome mail pointing at the Frappe desk instead of the app's /set-password page.
bench --site helpdeskfrappe.inventivebizsol.co.in \
  set-config app_url https://helpdesk.inventivebizsol.co.in
```

Then create an **Email Account** (outgoing, `default_outgoing=1`) in the Frappe desk.
Until one exists, `invite_poc` / `invite_member` create the account and return
`email_sent: false` without raising — invites will silently send nothing.

Leave `developer_mode` **off** (the default). It gates the guest-callable
`receive_webhook` and `send_test_email` endpoints, which must stay inert outside local dev.

---

## Rollback

1. Find the commit SHA from the Actions run history
2. In Portainer, change `VERSION` from `latest` to that SHA
3. Redeploy the stack

Or on the server:

```bash
docker service update \
  --image ghcr.io/inventive-business-solutions/inventive-helpdesk-backend:<sha> \
  <stack>_backend
```

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Build fails, `git clone ... exit status 128` | PAT lacks access to the repo | New classic PAT with `repo` scope; update `APPS_JSON_BASE64` |
| Configurator: `Missing argument 'VALUE'` | `$VAR` substituted by Compose at parse time | Use `$$VAR` in compose command blocks |
| Traefik 404 | `SITE_NAME` unset, or DNS not pointing at the host | Verify DNS and the `SITE_NAME` env var |
| Webhook returns non-200/204 | Wrong URL, or Portainer down | Re-copy the webhook URL from the stack |
| Deploy step skipped | `PORTAINER_WEBHOOK_URL` unset for that environment | Set the secret on the environment |
| Verify fails, exit 60 | TLS verification failed on the runner | Update the runner's CA certificates |
| Verify times out (~240s) | App never returned 200 | Check Portainer logs — usually migration failure, missing env var, or DB connectivity |

---

## Notes on this pipeline vs the template

Two deliberate changes from `tei-suit-backend`:

1. **`APPS_JSON_BASE64` is a secret, not a variable.** GitHub variables are not masked in
   logs and are readable by anyone with repo read access; this one embeds a PAT. The
   workflow also passes it via `env:` rather than inlining it, so the value never appears
   in the rendered command.
2. **The duplicate `Deploy to Server` step is removed.** The template fires the Portainer
   webhook a second time after the health check, which redeploys the build it just
   verified.
