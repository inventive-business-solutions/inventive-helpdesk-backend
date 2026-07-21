# CI/CD — Inventive Helpdesk Backend

Frappe app deployed on Docker Swarm via Portainer CE, behind Traefik. Adapted from
`samanvay-sangam-backend`, which runs on the same x86_64 Swarm node.

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
GitHub Actions (self-hosted, inventive-microscan)
       ├── build image from frappe_docker Containerfile (linux/amd64)
       └── push to GHCR (:<sha> and :latest)
       ▼
you, in Portainer: Update the stack (Re-pull image)
       ▼
Docker Swarm
       ├── pulls the new image, redeploys services
       └── migration service: bench --site all migrate
```

**The stack's `VERSION` must be `latest`.** This is the single setting the whole deploy
chain depends on, and getting it wrong fails silently.

The webhook available on Community Edition only re-pulls the git repository and
redeploys — it does not set environment variables (a `?VERSION=` query parameter is
accepted and then ignored), and it cannot force an image pull, since *Re-pull image*
and *Force redeployment* are Business features.

What makes it work anyway: `docker stack deploy` re-resolves an image tag to its
current registry digest, so redeploying `:latest` picks up whatever CI last pushed.

Pin `VERSION` to a SHA and every redeploy faithfully reinstalls that same image
forever — the pipeline goes green, the webhook returns 204, the containers restart,
and the old code keeps serving. Every build is also tagged `:<sha>`, so pinning one
deliberately is still how you roll back.

Build args pin the stack to match local dev: Frappe `version-16`, Python `3.14.0`,
Node `24.1.0`.

**Architecture matters.** The Swarm node is `linux/x86_64`, so the image is built
`linux/amd64` on the x86_64 `inventive-microscan` runner. Building `linux/arm64` (as an
earlier revision did) leaves every task stuck in `pending` with
`no suitable node (unsupported platform on 1 node)` — the Redis services still start,
because their image is multi-arch, which makes the failure look partial.

Two build flags are deliberate, both mirroring `samanvay-sangam-backend`:
`provenance: false` keeps the manifest single-platform, and `no-cache: true` prevents
the GHA layer cache reusing the `bench get-app` layer, which silently shipped stale app
code. Each build stamps `build_sha.txt` into the image so a running container can be
matched to its commit.

---

## Server prerequisites

The compose file declares these as **external** — it will not create them:

| Resource | Notes |
| --- | --- |
| `traefik-public` network | Traefik with the `le` certresolver |
| `mariadb-network` network | MariaDB reachable as `mariadb_db:3306`, plus its root password |
| Docker Swarm + Portainer CE | hosts the stack; deploys are triggered manually |
| Runner labelled `inventive-microscan` | self-hosted, x86_64; builds `linux/amd64` |

---

## GitHub configuration

### Variables (per environment)

| Variable | Development | Production |
| --- | --- | --- |
| `IMAGE_NAME` | `ghcr.io/inventive-business-solutions/inventive-helpdesk-backend` | `ghcr.io/inventive-business-solutions/prod-inventive-helpdesk-backend` |
| `SITE_NAME` | `helpdeskfrappe.inventivebizsol.co.in` | `helpdeskfrappe.inventivebizsol.co.in` |

`IMAGE_NAME` is the only variable CI currently reads. `SITE_NAME` is kept for the
Portainer stack (Traefik routing and site creation) and for the health check, should the
API-based deploy step be added later.

### Secrets (per environment)

| Secret | Notes |
| --- | --- |
| `APPS_JSON_BASE64` | Base64 apps.json containing a classic PAT — **a secret, not a variable**, so it stays masked in run logs |

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
```

Add the Development environment later if you want a second stack tracking `development`;
it needs its own Portainer stack, domain and `SITE_NAME`.

---

## Portainer stack

1. **Stacks → Add Stack → Repository**
2. Repository URL: `https://github.com/inventive-business-solutions/inventive-helpdesk-backend`
3. Authentication: ON (GitHub username + PAT)
4. Repository reference: `refs/heads/master` — this is the hosted branch
5. Compose path: `deploy/docker-compose.yml`
6. GitOps updates: **OFF** (webhooks are Business Edition; see above)
7. Add environment variables from `deploy/.env.example` — note `SITES` **includes backticks** in its value
8. Deploy the stack

> Create the stack **after** the first image exists in GHCR, otherwise the initial
> deploy fails with nothing to pull.

---

## First deployment

Set these in Portainer for the initial run:

```
CONFIGURE=1
CREATE_SITE=1
MIGRATE=0
SITES=`helpdeskfrappe.inventivebizsol.co.in`
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

---

## Notes on this pipeline vs the template

The compose and workflow are adapted from **`samanvay-sangam-backend`**, which runs on
this same Swarm node — not from `tei-suit-backend`, which targets ARM infrastructure
elsewhere. Deliberate differences from that reference:

1. **`APPS_JSON_BASE64` is a secret, not a variable.** GitHub variables are not masked in
   logs and are readable by anyone with repo read access; this one embeds a PAT. The
   workflow also passes it via `env:` rather than inlining it, so the value never appears
   in the rendered command.
2. **No CD step.** `samanvay-sangam-backend` posts to a `PORTAINER_WEBHOOK_URL` and
   then verifies the deployed `build_sha`. No webhook URL is configured for this stack
   yet, so releasing is a manual *Update the stack* in Portainer. If a webhook becomes
   available, add the Deploy and Verify steps back — the `build_sha.txt` stamp needed
   for verification is already built into the image.
