#!/usr/bin/env bash
# Build the linux/amd64 image, push it to the private Docker Hub repo and create/update the Portainer stack
# on the UGREEN NAS. No SSH; everything goes through Docker Hub and the Portainer API. Idempotent.
#
# Secrets are read from 1Password at run time and are never printed or written to disk:
#   AmoreHireMePls / Docker Hub Push Token / credential   -> docker login (stdin)
#   AmoreHireMePls / Portainer API Token / credential     -> X-API-Key header (via op run, curl reads $PORTAINER_API_KEY)
#   AmoreHireMePls / Dashboard Password, App Session Secret, NAS MongoDB App User, OpenAI API Key
#                                                         -> Portainer stack environment variables
#
# Usage:
#   scripts/deploy_nas.sh                 # build HEAD, push, create or update the stack, wait for health
#   scripts/deploy_nas.sh --skip-build    # only (re)apply the stack with the tag already pushed for HEAD
#   scripts/deploy_nas.sh --tag <tag>     # deploy an existing tag instead of HEAD
#   scripts/deploy_nas.sh --skip-build --set AMORE_BROWSER_ALLOWED_HOSTS=boards.greenhouse.io,jobs.lever.co
#                                         # set/override a non-secret stack variable (repeatable); values stick across runs
#   scripts/deploy_nas.sh --skip-build --set AMORE_PUBLISH_HOST=0.0.0.0   # publish on all NAS interfaces (LAN + tailnet)
#   scripts/deploy_nas.sh --set AMORE_CANDIDATE_DIR=/candidate            # drop folder inside the containers (compose default)
#
# Stack variables not managed here (anything other than AMORE_IMAGE_TAG and the vault secrets) are carried over from
# the stack's current environment, so a value set once with --set stays until changed. Secret names cannot be --set.
#
# Requirements on the Mac: docker (buildx), op (signed in), curl, python3, git. Run from anywhere in the repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PORTAINER_URL="${PORTAINER_URL:-http://192.168.2.49:9000}"
PORTAINER_ENDPOINT_ID="${PORTAINER_ENDPOINT_ID:-1}"
STACK_NAME="${STACK_NAME:-amorehiremepls}"
IMAGE_REPO="${IMAGE_REPO:-docker.io/lepia/amorehiremepls}"
HUB_USER="${HUB_USER:-lepia}"
COMPOSE_FILE="deploy/nas/compose.yaml"
HEALTH_URL="${HEALTH_URL:-http://192.168.2.49:8790/health}"
PUBLISH_HOST="${AMORE_PUBLISH_HOST:-}"   # empty = keep the stack's current value (first deploy: 192.168.2.49)

SKIP_BUILD=0
TAG=""
SET_JSON="[]"
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-build) SKIP_BUILD=1 ;;
    --tag) TAG="$2"; shift ;;
    --set)
      case "$2" in *=*) ;; *) echo "--set expects KEY=VALUE" >&2; exit 2 ;; esac
      SET_JSON="$(KV="$2" python3 -c 'import json,os,sys; k,v=os.environ["KV"].split("=",1); a=json.loads(sys.argv[1]); a.append([k,v]); print(json.dumps(a))' "$SET_JSON")"
      shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$TAG" ]; then
  TAG="$(git rev-parse --short=12 HEAD)"
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "note: working tree has uncommitted changes; the image is built from the committed HEAD ($TAG) only." >&2
  fi
fi

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

CTX="$(mktemp -d /tmp/amorehiremepls-ctx.XXXXXX)"
REFS="$(mktemp /tmp/amorehiremepls-refs.XXXXXX)"
trap 'rm -rf "$CTX" "$REFS"' EXIT

# ---------------------------------------------------------------- 1. build + push (amd64, from a clean export of HEAD)
log "docker login (token from 1Password, stdin only)"
op read 'op://AmoreHireMePls/Docker Hub Push Token/credential' | docker login -u "$HUB_USER" --password-stdin >/dev/null
if [ "$SKIP_BUILD" -eq 0 ]; then
  git archive HEAD | tar -x -C "$CTX"
  log "building $IMAGE_REPO:$TAG for linux/amd64 and pushing (also :latest)"
  docker buildx build --platform linux/amd64 --provenance=false \
    -t "$IMAGE_REPO:$TAG" -t "$IMAGE_REPO:latest" --push "$CTX"
fi
# Never point the stack at a tag that is not in the registry: compose would fail to pull and Portainer would still
# record the new tag on the stack (observed: PUT returns 200, containers keep running the previous image).
if ! docker buildx imagetools inspect "$IMAGE_REPO:$TAG" >/dev/null 2>&1; then
  echo "image $IMAGE_REPO:$TAG is not in the registry; run without --skip-build or pass --tag <pushed tag>" >&2
  docker logout docker.io >/dev/null 2>&1 || true
  exit 1
fi
docker logout docker.io >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 2. create or update the Portainer stack
log "applying Portainer stack '$STACK_NAME' with AMORE_IMAGE_TAG=$TAG"
cat >"$REFS" <<'EOF'
PORTAINER_API_KEY=op://AmoreHireMePls/Portainer API Token/credential
AMORE_APP_PASSWORD=op://AmoreHireMePls/Dashboard Password/password
AMORE_APP_SECRET=op://AmoreHireMePls/App Session Secret/credential
AMORE_MONGODB_USERNAME=op://AmoreHireMePls/NAS MongoDB App User/username
AMORE_MONGODB_PASSWORD=op://AmoreHireMePls/NAS MongoDB App User/password
OPENAI_API_KEY=op://AmoreHireMePls/OpenAI API Key/credential
EOF

export PORTAINER_URL PORTAINER_ENDPOINT_ID STACK_NAME COMPOSE_FILE TAG PUBLISH_HOST IMAGE_REPO SET_JSON
op run --env-file "$REFS" -- python3 - <<'PY'
import json, os, sys, time, urllib.error, urllib.request

base = os.environ["PORTAINER_URL"].rstrip("/")
endpoint = int(os.environ["PORTAINER_ENDPOINT_ID"])
name = os.environ["STACK_NAME"]
key = os.environ["PORTAINER_API_KEY"]
compose = open(os.environ["COMPOSE_FILE"], encoding="utf-8").read()
SECRET_NAMES = ("AMORE_APP_PASSWORD", "AMORE_APP_SECRET", "AMORE_MONGODB_USERNAME", "AMORE_MONGODB_PASSWORD", "OPENAI_API_KEY")
MANAGED = set(SECRET_NAMES) | {"AMORE_IMAGE_TAG"}
overrides = dict(json.loads(os.environ["SET_JSON"]))
for k in overrides:
    if k in MANAGED:
        print(f"refusing --set {k}: managed by this script (secrets come from the vault)", file=sys.stderr); sys.exit(2)


def call(method, path, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:600]


st, stacks = call("GET", "/api/stacks")
if st != 200:
    print("cannot list stacks:", st, stacks, file=sys.stderr); sys.exit(1)
existing = [s for s in stacks if s["Name"] == name and s["EndpointId"] == endpoint]
wanted_image = os.environ["IMAGE_REPO"] + ":" + os.environ["TAG"]

# non-secret variables: carried over from the current stack, then --set overrides, then AMORE_PUBLISH_HOST from the shell
carried = {}
if existing:
    st, cur = call("GET", f"/api/stacks/{existing[0]['Id']}")
    if st == 200:
        carried = {e["name"]: e["value"] for e in (cur.get("Env") or []) if e["name"] not in MANAGED}
carried.update(overrides)
if os.environ.get("PUBLISH_HOST"):
    carried["AMORE_PUBLISH_HOST"] = os.environ["PUBLISH_HOST"]
carried.setdefault("AMORE_PUBLISH_HOST", "192.168.2.49")
env = [{"name": "AMORE_IMAGE_TAG", "value": os.environ["TAG"]}]
env += [{"name": k, "value": v} for k, v in sorted(carried.items())]
env += [{"name": k, "value": os.environ[k]} for k in SECRET_NAMES]
for e in env:
    if e["name"] in MANAGED and not e["value"]:
        print(f"refusing to deploy: {e['name']} resolved to an empty value", file=sys.stderr)
        sys.exit(1)

if existing:
    sid = existing[0]["Id"]
    st, out = call("PUT", f"/api/stacks/{sid}?endpointId={endpoint}", {"stackFileContent": compose, "env": env, "prune": True, "pullImage": True})
    action = "updated"
else:
    st, out = call("POST", f"/api/stacks/create/standalone/string?endpointId={endpoint}", {"name": name, "stackFileContent": compose, "env": env, "fromAppTemplate": False})
    action = "created"
    sid = out.get("Id") if isinstance(out, dict) else None
if st != 200:
    print(f"stack {action[:-1]} failed:", st, out, file=sys.stderr); sys.exit(1)
print(f"stack {action}: id={sid} name={name} image_tag={os.environ['TAG']}")
print("  env applied (names only): " + ", ".join(e["name"] for e in env))

# wait until web and worker run the requested image and are healthy (Portainer answers the create/update
# request before compose has finished recreating containers; an unchanged stack is a no-op and passes at once)
deadline = time.time() + 300
while True:
    st, cs = call("GET", f"/api/endpoints/{endpoint}/docker/containers/json?all=1&filters=" + urllib.request.quote(json.dumps({"label": [f"com.docker.compose.project={name}"]})))
    states = {}
    if st == 200:
        for c in cs:
            svc = c["Labels"].get("com.docker.compose.service")
            states[svc] = (c["State"], c["Status"] + " " + c["Image"])
    running = {k: v for k, v in states.items() if k in ("web", "worker")}
    healthy = len(running) == 2 and all(v[0] == "running" and "(healthy)" in v[1] and v[1].endswith(" " + wanted_image) for v in running.values())
    st, info = call("GET", f"/api/stacks/{sid}")
    status = info.get("Status") if isinstance(info, dict) else None
    if (healthy and status == 1) or time.time() > deadline:
        break
    time.sleep(5)
for k in sorted(states):
    print(f"  {k}: {states[k][0]} {states[k][1]}")
print(f"  stack status: {status} (1 = active)")
if not healthy or status != 1:
    print("deploy did not converge (containers not recreated/healthy within 300s or stack not active); check Portainer → Stacks → amorehiremepls", file=sys.stderr)
    sys.exit(1)
PY

# ---------------------------------------------------------------- 3. dashboard health
code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
log "GET $HEALTH_URL -> $code"
[ "$code" = "200" ]
