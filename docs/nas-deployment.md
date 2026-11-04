# NAS deployment (UGREEN, existing MongoDB)

Target: the existing `mongo:8.0` container on the NAS (bound to loopback 27018 on the host; not reachable from this Mac at build time). Nothing here changes other NAS services, rotates credentials, exposes ports publicly, or mounts the Docker socket.

## 1. Database user (one-off, on the NAS)

Create a separate database and user for this app with an admin session you already have; do not reuse other services' users:

```js
use amorehiremepls
db.createUser({ user: "amore", pwd: passwordPrompt(), roles: [{ role: "readWrite", db: "amorehiremepls" }] })
```

Store the password in 1Password (vault for this project) as `AmoreHireMePls/mongodb/password`.

## 2. Network

Preferred: attach the app to the Docker network the Mongo container is on and use `AMORE_MONGODB_URI=mongodb://<mongo-container-name>:27017`. Add to `docker-compose.yml` on the NAS:

```yaml
networks:
  default:
    name: <existing mongo network>
    external: true
```

Alternative: `host.docker.internal` plus `extra_hosts: ["host.docker.internal:host-gateway"]` to reach loopback 27018. Never publish Mongo on a LAN interface for this app.

Keep `AMORE_BIND_HOST=127.0.0.1` on the NAS and reach the dashboard through SSH port forwarding or the existing VPN: `ssh -L 8790:127.0.0.1:8790 nas`.

## 3. Secrets (1Password, unattended)

Same pattern as AmoreGymBro: a read-only service account scoped to the project vault. Store the token at `/volume1/docker/amorehiremepls/op_token` (`chmod 600`, owned by the deploy user) and start services through `op run`:

```bash
export OP_SERVICE_ACCOUNT_TOKEN="$(cat /volume1/docker/amorehiremepls/op_token)"
op run --env-file .env.references -- docker compose up -d web worker
```

`.env` on the NAS holds only non-secret settings. `.env.references` maps `AMORE_MONGODB_PASSWORD`, `AMORE_APP_PASSWORD`, `AMORE_APP_SECRET`, `AMORE_OPENAI_API_KEY`/`AMORE_ANTHROPIC_API_KEY`, `AMORE_SMTP_PASSWORD` to `op://` references.

## 4. Build and run

```bash
rsync -a --exclude runtime --exclude .env --exclude .git ./ nas:/volume1/docker/amorehiremepls/
ssh nas 'cd /volume1/docker/amorehiremepls && docker compose build && op run --env-file .env.references -- docker compose up -d web worker'
ssh nas 'curl -s http://127.0.0.1:8790/health'
```

The image is based on `mcr.microsoft.com/playwright/python:v1.64.1-noble` and includes `libreoffice-writer` so PDF rendering and page verification work on the NAS. Both services run as uid 10001 with all capabilities dropped, `no-new-privileges`, 1.5 CPU / 1536 MB limits, json-file log rotation, and a persistent `/data` volume for artifacts, private files and logs.

## 5. Browser sessions

The worker runs headless. Sign in to job sites on the Mac with `uv run amore browser login <site> <url>` and copy `runtime/private/sessions/<site>.json` to the NAS volume (`/data/private/sessions/`) with `scp` over SSH. Expired sessions surface as `handoff` interrupts with the message to refresh.

## 6. Tracker clone

On the NAS, `amore tracker clone` needs git credentials for `christya95/job-application-records`. Use a fine-grained GitHub token limited to that repository, injected as `GIT_ASKPASS` through 1Password, or keep `AMORE_TRACKER_PUSH_ENABLED=false` and let the Mac push from its own clone (`local_only` items stay visible until pushed).

## 7. Checks after deploy

- `docker compose ps` shows both services healthy (web: `/health`; worker: `amore worker-health`).
- Dashboard Overview: database ok, worker heartbeat ok, provider check ok.
- `amore tracker preview` shows the expected 39 historical records before the first import.
- Run a backup and copy it off the NAS.
