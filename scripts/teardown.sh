#!/usr/bin/env bash
# Tear down the telemetry stack. By default keeps the OpenSearch data volume.
# Pass --purge to also delete indexed telemetry data.
set -euo pipefail

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${STACK_DIR}"

if [[ "${1:-}" == "--purge" ]]; then
  echo "==> Stopping containers and removing data volume"
  docker compose down -v
  # If data lives in a host dir (env var or .env), --purge deletes it too.
  DATA_DIR="${CLAUDE_OTEL_DATA_DIR:-}"
  if [[ -z "${DATA_DIR}" && -f .env ]]; then
    DATA_DIR="$(grep -E '^CLAUDE_OTEL_DATA_DIR=' .env | tail -1 | cut -d= -f2-)"
  fi
  if [[ -n "${DATA_DIR}" && -d "${DATA_DIR}" ]]; then
    echo "==> Removing host data dir ${DATA_DIR}"
    rm -rf "${DATA_DIR}"
  fi
else
  echo "==> Stopping containers (data volume preserved; use --purge to delete it)"
  docker compose down
fi
echo "Done."
