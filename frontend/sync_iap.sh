#!/usr/bin/env bash
# Reconcile IAP access with the registry: config/projects.<env>.yaml is the
# single source of truth for membership, at both layers.
#
#   L5 (which project a user belongs to) — rendered into WS_PROJECTS by deploy.sh
#   L2 (whether a user may enter at all) — applied here
#
# Only "user:" bindings are managed. Groups and service accounts are left
# untouched, so a group can front the same service without being fought over.
#
#   ./sync_iap.sh <env> [--dry-run]
set -euo pipefail

ENV=${1:?usage: sync_iap.sh <env> [--dry-run]}
DRY_RUN=${2:-}
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
SERVICE=ws-agent-bff
ROLE=roles/iap.httpsResourceAccessor

TARGET="$REPO_ROOT/config/deploy.$ENV.yaml"
[ -f "$TARGET" ] || { echo "no deploy target for env '$ENV': $TARGET" >&2; exit 1; }
PROJECT=$(python3 -c 'import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))["project"])' "$TARGET")
REGION=$(python3 -c 'import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))["region"])' "$TARGET")

CONFIG="$REPO_ROOT/config/projects.$ENV.yaml"
[ -f "$CONFIG" ] || CONFIG="$REPO_ROOT/config/projects.yaml"

# Desired: every member of every project the BFF serves. L2 is per service,
# so the union is what may enter; L5 still narrows each user to their projects.
DESIRED=$(python3 -c '
import sys, yaml
reg = yaml.safe_load(open(sys.argv[1]))
print("\n".join(sorted({m for p in reg["projects"] for m in p.get("members", [])})))' "$CONFIG")

CURRENT=$(gcloud iap web get-iam-policy --resource-type=cloud-run \
  --service="$SERVICE" --region="$REGION" --project="$PROJECT" --format=json \
  | python3 -c '
import json, sys
policy = json.load(sys.stdin)
role = "roles/iap.httpsResourceAccessor"
for b in policy.get("bindings", []):
    if b.get("role") == role:
        print("\n".join(sorted(
            m.removeprefix("user:") for m in b.get("members", []) if m.startswith("user:")
        )))')

ADD=$(comm -23 <(echo "$DESIRED") <(echo "$CURRENT"))
REMOVE=$(comm -13 <(echo "$DESIRED") <(echo "$CURRENT"))

if [ -z "$ADD" ] && [ -z "$REMOVE" ]; then
  echo "in sync: $(echo "$DESIRED" | grep -c .) members on $SERVICE ($PROJECT)"
  exit 0
fi

[ -n "$ADD" ]    && echo "$ADD"    | sed 's/^/  + /'
[ -n "$REMOVE" ] && echo "$REMOVE" | sed 's/^/  - /'

if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "[dry-run] no changes applied"
  exit 0
fi

bind() { # verb, email
  gcloud iap web "$1-iam-policy-binding" --resource-type=cloud-run \
    --service="$SERVICE" --region="$REGION" --project="$PROJECT" \
    --member="user:$2" --role="$ROLE" --format='value(etag)' >/dev/null
}
for m in $ADD;    do bind add    "$m"; done
for m in $REMOVE; do bind remove "$m"; done
echo "synced $SERVICE ($PROJECT)"
