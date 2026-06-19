#!/usr/bin/env bash
# Start — and fully provision — the Claude Code telemetry stack.
#
# This is the single entry point: it is idempotent and self-healing. Every run
# brings the containers up, (re)applies the OpenSearch retention policy, index
# template and data stream, imports the dashboards, and refreshes the
# index-pattern field cache. So one `start.sh` always leaves a known-good
# dashboard — safe to re-run after a reboot, after editing the config or the
# dashboard generator, or when a panel errors with
# "Could not locate that index-pattern-field". There is no separate fix script.
#
# Flags:
#   --build    (re)build the collector image first — use after changing
#              Dockerfile.collector or the collector version. `scripts/setup.sh`
#              is just a shortcut for `start.sh --build`.
#   --verify   after starting, push a synthetic event through the pipeline and
#              confirm it lands in OpenSearch.
set -euo pipefail

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${STACK_DIR}"

OS_URL="http://localhost:9200"
OSD_URL="http://localhost:5601"

BUILD=0
VERIFY=0
for arg in "$@"; do
  case "${arg}" in
    --build) BUILD=1 ;;
    --verify) VERIFY=1 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

# If a host data dir is configured (env var or .env), create it before `up` so
# Docker doesn't make the bind-mount source root-owned. Compose reads .env itself.
DATA_DIR="${CLAUDE_OTEL_DATA_DIR:-}"
if [[ -z "${DATA_DIR}" && -f .env ]]; then
  DATA_DIR="$(grep -E '^CLAUDE_OTEL_DATA_DIR=' .env | tail -1 | cut -d= -f2-)"
fi
[[ -n "${DATA_DIR}" ]] && mkdir -p "${DATA_DIR}"

echo "==> 1/5 Ensuring docker daemon is reachable"
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon not reachable. Start the Docker daemon and retry." >&2
  exit 1
fi

echo "==> 2/5 Starting containers"
# The collector image is built via a multi-stage Dockerfile that pulls the
# upstream binary onto a glibc base (see Dockerfile.collector); no local copy.
if [[ "${BUILD}" -eq 1 ]]; then
  docker compose up -d --build
else
  docker compose up -d
fi

echo "==> 3/5 Waiting for OpenSearch to be healthy"
for i in $(seq 1 60); do
  if curl -sf "${OS_URL}/_cluster/health" >/dev/null 2>&1; then break; fi
  sleep 2
  [[ $i -eq 60 ]] && { echo "ERROR: OpenSearch did not become healthy" >&2; exit 1; }
done

echo "==> 4/5 Applying ISM policy, data-stream template and data stream"
# Retention policy (daily rollover + delete after 90d). Create if absent, else
# update in place using the current seq_no/primary_term.
ism_meta="$(curl -s "${OS_URL}/_plugins/_ism/policies/claude-code-retention")"
if echo "${ism_meta}" | grep -q '"_seq_no"'; then
  read -r seq pt < <(echo "${ism_meta}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["_seq_no"],d["_primary_term"])')
  curl -s -X PUT "${OS_URL}/_plugins/_ism/policies/claude-code-retention?if_seq_no=${seq}&if_primary_term=${pt}" \
    -H "Content-Type: application/json" --data-binary @opensearch/ism-policy.json >/dev/null
else
  curl -s -X PUT "${OS_URL}/_plugins/_ism/policies/claude-code-retention" \
    -H "Content-Type: application/json" --data-binary @opensearch/ism-policy.json >/dev/null
fi
# Data-stream index template (data_stream + mappings + policy_id setting).
curl -sf -X PUT "${OS_URL}/_index_template/claude-code-logs" \
  -H "Content-Type: application/json" \
  --data-binary @opensearch/index-template.json >/dev/null
# Create the data stream if absent (no-op if the exporter already created it).
curl -s -X PUT "${OS_URL}/_data_stream/ss4o_logs-claudecode-telemetry" >/dev/null 2>&1 || true
# Attach the policy to current backing indices (ism_template handles future ones).
curl -s -X POST "${OS_URL}/_plugins/_ism/add/ss4o_logs-claudecode-telemetry" \
  -H "Content-Type: application/json" -d '{"policy_id":"claude-code-retention"}' >/dev/null 2>&1 || true
# Config is bind-mounted, so a compose change alone won't reload it; restart the collector.
docker compose restart otel-collector >/dev/null 2>&1 || true

echo "==> 5/5 Building and importing dashboards"
python3 dashboards/build-saved-objects.py
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "${OSD_URL}/api/status" || true)"
  [[ "${code}" == "200" ]] && break
  sleep 2
  [[ $i -eq 60 ]] && { echo "ERROR: OpenSearch Dashboards did not start" >&2; exit 1; }
done
curl -sf -X POST "${OSD_URL}/api/saved_objects/_import?overwrite=true" \
  -H "osd-xsrf: true" --form file=@dashboards/saved-objects.ndjson >/dev/null
echo "    dashboards imported."

# The import ships the index-pattern with no field list, resetting its field
# cache to empty; OSD 3.x then errors "Could not locate that index-pattern-field"
# on panels. Repopulate it from the live mapping (headless "refresh field list")
# so every panel resolves its fields — this is why start.sh self-heals that error.
echo "    refreshing index-pattern field cache"
OSD_URL="${OSD_URL}" python3 - <<'PY'
import json, os, urllib.request

osd = os.environ["OSD_URL"]
pattern, ipid = "ss4o_logs-claudecode-*", "claude-code-logs"

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(osd + path, data=data, method=method,
                               headers={"osd-xsrf": "true", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r))

meta = "&".join(f"meta_fields={m}" for m in ("_source", "_id", "_index", "_score"))
fields = req("GET", f"/api/index_patterns/_fields_for_wildcard?pattern={pattern}&{meta}")["fields"]
req("POST", f"/api/saved_objects/index-pattern/{ipid}?overwrite=true",
    {"attributes": {"title": pattern, "timeFieldName": "@timestamp",
                    "fields": json.dumps(fields)}})
print(f"    {len(fields)} fields cached.")
PY

if [[ "${VERIFY}" -eq 1 ]]; then
  echo "==> Verifying the pipeline end-to-end (--verify)"
  before="$(curl -s "${OS_URL}/ss4o_logs-claudecode-*/_count" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count",0))' 2>/dev/null || echo 0)"
  ./scripts/send-test-event.sh >/dev/null
  # Collector batches on a 5s timeout; poll for the count to rise.
  ok=0
  for i in $(seq 1 12); do
    sleep 2
    curl -s -X POST "${OS_URL}/ss4o_logs-claudecode-*/_refresh" >/dev/null 2>&1 || true
    after="$(curl -s "${OS_URL}/ss4o_logs-claudecode-*/_count" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count",0))' 2>/dev/null || echo 0)"
    if [[ "${after}" -gt "${before}" ]]; then ok=1; break; fi
  done
  if [[ "${ok}" -eq 1 ]]; then
    echo "    OK: synthetic event indexed (count ${before} -> ${after})."
  else
    echo "ERROR: synthetic event did not reach OpenSearch (count stayed ${before})." >&2
    echo "       Check 'docker logs claude-otel-collector'." >&2
    exit 1
  fi
fi

cat <<EOF

==========================================================================
 Claude Code telemetry stack is up.

   Dashboard : ${OSD_URL}/app/dashboards#/view/claude-code-cost-usage
   OpenSearch: ${OS_URL}
   OTLP gRPC : http://localhost:4317   (Claude Code -> here)

 First time? Run ./scripts/enable-telemetry.sh once to route all Claude Code
 usage here, then restart your Claude Code sessions.
==========================================================================
EOF
