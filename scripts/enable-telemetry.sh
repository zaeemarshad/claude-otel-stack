#!/usr/bin/env bash
# Route ALL Claude Code usage to the local OTel collector by merging an "env" block
# into ~/.claude/settings.json (user-level => every project/session). Backs up first.
# Idempotent: re-running just re-applies the same keys.
set -euo pipefail

SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
BACKUP="${SETTINGS}.bak.$(date +%Y%m%d-%H%M%S)"

if [[ ! -f "${SETTINGS}" ]]; then
  echo "{}" > "${SETTINGS}"
fi

cp "${SETTINGS}" "${BACKUP}"
echo "Backed up ${SETTINGS} -> ${BACKUP}"

python3 - "${SETTINGS}" <<'PY'
import json, sys

path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)

telemetry_env = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
    "OTEL_LOGS_EXPORT_INTERVAL": "5000",
    "OTEL_LOG_TOOL_DETAILS": "1",
    "OTEL_SERVICE_NAME": "claude-code",
    # Distributed tracing (Claude Code beta). Spans land in OpenSearch under
    # ss4o_traces-claudecode-telemetry via the collector's traces pipeline.
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_TRACES_EXPORT_INTERVAL": "5000",
    # NOTE: metrics are intentionally NOT enabled. The opensearch exporter cannot
    # store OTLP metrics, so OTEL_METRICS_EXPORTER is left unset; turning it on
    # would only produce export errors against a collector with no metrics pipeline.
}

env = cfg.get("env", {})
env.update(telemetry_env)
cfg["env"] = env

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

print("Merged telemetry env keys:")
for k in telemetry_env:
    print(f"  {k}={telemetry_env[k]}")
PY

echo
echo "Done. Restart any running Claude Code sessions for the new env to take effect."
