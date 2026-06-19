#!/usr/bin/env bash
# First-time / full bootstrap of the Claude Code telemetry stack.
#
# Thin shortcut for `start.sh --build`: start.sh does all the work (start,
# provision, import dashboards, refresh field cache) and is idempotent, so this
# just forces a rebuild of the collector image as well. Use this for a fresh
# install or after changing Dockerfile.collector; use start.sh otherwise.
# Pass --verify to smoke-test the pipeline end-to-end.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${DIR}/start.sh" --build "$@"
