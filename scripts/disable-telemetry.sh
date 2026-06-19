#!/usr/bin/env bash
# Stop routing Claude Code usage to the local collector: removes exactly the env
# keys that enable-telemetry.sh added from ~/.claude/settings.json. Backs up first.
# Idempotent: re-running is a no-op once the keys are gone.
set -euo pipefail

SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"

if [[ ! -f "${SETTINGS}" ]]; then
  echo "No settings file at ${SETTINGS}; nothing to disable."
  exit 0
fi

BACKUP="${SETTINGS}.bak.$(date +%Y%m%d-%H%M%S)"
cp "${SETTINGS}" "${BACKUP}"
echo "Backed up ${SETTINGS} -> ${BACKUP}"

python3 - "${SETTINGS}" <<'PY'
import json, sys

path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)

# The exact keys enable-telemetry.sh sets.
telemetry_keys = [
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_LOGS_EXPORT_INTERVAL",
    "OTEL_LOG_TOOL_DETAILS",
    "OTEL_SERVICE_NAME",
]

env = cfg.get("env", {})
removed = [k for k in telemetry_keys if env.pop(k, None) is not None]

# Drop an empty env block we may have emptied; otherwise keep any user keys.
if "env" in cfg and not env:
    del cfg["env"]
elif "env" in cfg:
    cfg["env"] = env

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

if removed:
    print("Removed telemetry env keys:")
    for k in removed:
        print(f"  {k}")
else:
    print("No telemetry env keys were present; nothing changed.")
PY

echo
echo "Done. Restart any running Claude Code sessions to stop exporting telemetry."
