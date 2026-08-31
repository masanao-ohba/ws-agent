#!/usr/bin/env bash
# Build and deploy the BFF to Cloud Run with IAP in front.
#
# Config is rendered from config/projects.<env>.yaml at deploy time, so the
# registry lives in exactly one place. IAP is the entry gate: the service
# itself never accepts unauthenticated traffic.
#
#   ./deploy.sh dev <agent-engine-resource-name>
set -euo pipefail

ENV=${1:?usage: deploy.sh <env> <agent-engine-resource-name>}
ENGINE=${2:?usage: deploy.sh <env> <agent-engine-resource-name>}
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
SERVICE=ws-agent-bff

TARGET="$REPO_ROOT/config/deploy.$ENV.yaml"
[ -f "$TARGET" ] || { echo "no deploy target for env '$ENV': $TARGET" >&2; exit 1; }
PROJECT=$(python3 -c 'import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))["project"])' "$TARGET")
REGION=$(python3 -c 'import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))["region"])' "$TARGET")

CONFIG="$REPO_ROOT/config/projects.$ENV.yaml"
[ -f "$CONFIG" ] || CONFIG="$REPO_ROOT/config/projects.yaml"

# Registry: only the fields the BFF needs. members stay out — they feed IAP
# sync only; anyone past IAP has access to all projects.
WS_PROJECTS=$(python3 -c '
import json, sys, yaml
reg = yaml.safe_load(open(sys.argv[1]))
print(json.dumps({"projects": [
    {"id": p["id"], "name": p["name"], "anchors": p.get("anchors", [])}
    for p in reg["projects"]
]}, ensure_ascii=False))' "$CONFIG")

WS_ENGINES=$(python3 -c '
import json, sys, yaml
reg = yaml.safe_load(open(sys.argv[1]))
print(json.dumps([{
    "name": sys.argv[3],
    "resource_name": sys.argv[2],
    "region": sys.argv[4],
    "project_ids": [p["id"] for p in reg["projects"]],
}], ensure_ascii=False))' "$CONFIG" "$ENGINE" "$ENV" "$REGION")

# JSON is full of commas, and member emails contain "@", so neither can
# delimit the env-var list; "~" appears in neither.
case "$WS_PROJECTS$WS_ENGINES" in
  *"~"*) echo "config contains '~', which is the env-var delimiter" >&2; exit 1 ;;
esac

IMAGE="$REGION-docker.pkg.dev/$PROJECT/ws-agent/bff:latest"
echo "building $IMAGE"
gcloud builds submit "$REPO_ROOT/frontend" --tag "$IMAGE" --project "$PROJECT" --quiet

echo "deploying $SERVICE"
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --project "$PROJECT" \
  --no-allow-unauthenticated \
  --iap \
  --set-env-vars "^~^WS_PROJECTS=$WS_PROJECTS~WS_ENGINES=$WS_ENGINES" \
  --quiet

# Membership has one source: registry members drive IAP access (whether a
# user may enter). Anyone past IAP has access to all projects.
"$REPO_ROOT/frontend/sync_iap.sh" "$ENV"

gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" \
  --format 'value(status.url)'
