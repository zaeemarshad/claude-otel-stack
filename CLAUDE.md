# CLAUDE.md

## Overview

This repository is a **local, single-node telemetry stack for the Claude Code
CLI**. It captures the OpenTelemetry data Claude Code emits, stores it in
OpenSearch, and visualises tool usage, permission decisions, cost, tokens and
API health on a pre-provisioned dashboard. Everything runs on the developer's
machine and is bound to `localhost` only — nothing leaves the host.

## Architecture

```
Claude Code (host CLI, all projects)
   │  OTLP/gRPC :4317, OTLP/HTTP :4318   (logs/events + traces)
   ▼
OTel Collector (contrib build, rebuilt on a glibc base)
   │  otlp receiver → logs pipeline   (transform → opensearch)
   │               → traces pipeline  (opensearch)
   ▼
OpenSearch  :9200   (single node, security disabled, persistent volume)
   ▲
OpenSearch Dashboards :5601   ── "Claude Code – Cost & Usage" dashboard
```

Claude Code can emit three OTLP signals: **logs/events**, **metrics** and
**traces** (traces are a beta, opt-in via `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`).
This stack ingests **logs and traces**. Each log event already carries what the
dashboard needs (`tool_name`, `success`, `duration_ms`, `cost_usd`,
`input/output_tokens`, `model`, `decision`). **Metrics are not stored:** the
`opensearch` exporter has no metrics support, so `OTEL_METRICS_EXPORTER` is left
unset — enabling it would only produce export errors against a collector with no
metrics pipeline. Traces are enabled by `enable-telemetry.sh` and land in
`ss4o_traces-claudecode-telemetry`.

## Repo structure

```
docker-compose.yml                  OpenSearch + Dashboards + Collector
Dockerfile.collector                Multi-stage: upstream collector binary on a glibc base
Dockerfile.opensearch               OpenSearch + Prometheus exporter plugin
otel-collector-config.yaml          OTLP receiver → logs + traces pipelines → opensearch
opensearch/index-template.json      Field-type mappings for the log index
dashboards/build-saved-objects.py   Dashboard-as-code generator
dashboards/saved-objects.ndjson     Generated import bundle (do not hand-edit)
scripts/start.sh                    Start + fully provision the stack (idempotent, self-healing)
scripts/setup.sh                    Shortcut for `start.sh --build` (first install / image rebuild)
scripts/enable-telemetry.sh         Patch ~/.claude/settings.json to route usage here
scripts/send-test-event.sh          Synthetic OTLP event for pipeline checks
scripts/backfill-history.py         Backfill cost/usage from ~/.claude transcripts (gap before live telemetry)
scripts/teardown.sh                 Stop the stack (--purge also deletes data)
```

## Data schema (ss4o observability schema)

The opensearch exporter writes documents into the **data stream**
`ss4o_logs-claudecode-telemetry` (pattern `ss4o_logs-claudecode-*`; backing
indices are `.ds-ss4o_logs-claudecode-telemetry-NNNNNN`). The data stream rolls
over daily and an ISM policy (`claude-code-retention`) deletes backing indices at
2 years. The exporter writes via bulk `create`, which data streams require.

- **Time field:** `@timestamp` (date).
- **Event type:** `body` (e.g. `claude_code.tool_result`) — aggregate on
  `body.keyword`.
- **Event fields:** under `attributes.*`. String fields have a `.keyword`
  subfield; numeric fields (`duration_ms`, `cost_usd`, `input_tokens`,
  `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `status_code`)
  are typed by the index template and aggregated directly.
- **Resource fields:** `resource.service.name`, `resource.session.id`.
- **Collector-derived fields:** Claude Code emits no project/cwd/file/command
  attributes, so the collector's `transform` processor extracts `project`,
  `file_path` and `command` from `tool_input` into `attributes.*`. These exist
  only on tool events (`tool_result`/`tool_decision`); `api_request` events
  (which carry cost) have no path, so cost is grouped by session, not project.
  MCP tool calls all arrive with `tool_name` = `mcp_tool`; the transform pulls
  `mcp_server_name`/`mcp_tool_name` from `tool_parameters` and rewrites
  `tool_name` to `<server>:<tool>` (e.g. `myserver:search`) so each MCP tool
  ranks separately instead of collapsing into one `mcp_tool` bucket.

Observed event types: `user_prompt`, `tool_result`, `tool_decision`,
`api_request`, `api_error`, `mcp_server_connection`, `plugin_loaded`,
`hook_registered`, `hook_execution_start/complete`.

**Traces (beta).** When `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` and
`OTEL_TRACES_EXPORTER=otlp` are set (`enable-telemetry.sh` does both), Claude Code
emits spans (`claude_code.interaction` → `claude_code.llm_request` /
`claude_code.tool`). The collector's `traces` pipeline writes them to the **plain
index** `ss4o_traces-claudecode-telemetry` (no `transform` processor — that is
logs-only). Unlike the logs data stream, this index has **no rollover/retention
policy yet**; prune it manually if it grows. There is no trace dashboard — query
spans in OpenSearch Dashboards' Discover/Observability views.

**Backfilled history.** Claude Code can't replay past sessions over OTLP, but its
session transcripts (`~/.claude/projects/*/*.jsonl`) hold token counts, models
and tool calls. `scripts/backfill-history.py` reconstructs `api_request` and
`tool_result` events from them and POSTs them to the collector with the original
timestamp, so the `transform` and exporter enrich them exactly like live data.
`cost_usd` is not in the transcripts; the script computes it from per-model,
per-token-component rates it derives by least-squares over the live `api_request`
docs already in OpenSearch (no hardcoded price table) — the effective rates
Claude Code itself reported, reproduced to within ~1–3%. `opus-4-7` (absent from
live data) borrows `opus-4-8` rates. Every backfilled doc is tagged
`attributes.backfill=transcript`; the run is idempotent per session and, by
default, stops at the earliest live-telemetry timestamp so it fills only the gap
before telemetry was enabled and never double-counts. `tool_decision`,
`user_prompt` and `duration_ms` aren't reconstructable and are omitted. History
reaches only as far back as the oldest local transcript (Claude Code prunes them
per `cleanupPeriodDays`); there is no older source.

## Setup / build / test commands

- Everything: `./scripts/start.sh` — the single, idempotent, self-healing entry
  point. Each run starts the containers, re-applies the template/ISM/data stream,
  re-imports the dashboards, and refreshes the index-pattern field cache. Safe to
  re-run after a reboot, after editing the generator, or to clear a stale-field
  error. Containers also auto-restart via `restart: unless-stopped`.
- First install / collector-image rebuild: `./scripts/setup.sh`
  (== `start.sh --build`). `--verify` smoke-tests the pipeline.
- Route all Claude Code usage here (once): `./scripts/enable-telemetry.sh`,
  then restart open Claude Code sessions.
- Smoke-test the pipeline without a live session: `./scripts/send-test-event.sh`.
- Backfill pre-telemetry history from local session transcripts:
  `./scripts/backfill-history.py` (`--dry-run` to preview, `--purge` to remove).
- After editing the dashboard generator, just re-run `./scripts/start.sh` (it
  runs the generator and re-imports `saved-objects.ndjson`).
- Tear down: `./scripts/teardown.sh` (keeps data) or `--purge` (deletes the
  `opensearch-data` volume).

## Conventions

- **Dashboards are code.** Edit `dashboards/build-saved-objects.py`, never the
  generated `saved-objects.ndjson` by hand.
- **Aggregate strings on `.keyword`,** numerics directly. Aggregating a `text`
  field errors with "fielddata".
- **Ratios/projections use Vega, not classic viz.** Classic OSD aggregations can't
  divide one aggregate by another (cost per token, per-session averages, run-rate
  projections) and TSVB metric panels only show the last time bucket. The
  `vega_viz()` helper builds single-stat tiles that query OpenSearch directly
  (`%context%`/`%timefield%` honour the dashboard time filter) and compute the
  ratio client-side. The stack ships a single dashboard, **Claude Code – Cost &
  Usage** (`claude-code-cost-usage`).
- Each visualisation's `searchSourceJSON` must carry
  `"indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index"` matching its
  `references` entry, or panels fail with "Trying to initialize aggs without
  index pattern". The generator handles this.
- Pin container/image versions in `docker-compose.yml` (currently OpenSearch +
  Dashboards 3.8.0, collector 0.116.0). OpenSearch is built from
  `Dockerfile.opensearch`, which installs the Prometheus exporter plugin. The
  plugin version must match the OpenSearch version exactly (`3.8.0` →
  `3.8.0.0`), so bump both together and rebuild with `./scripts/setup.sh`.
- Storage is a data stream with an ISM retention policy
  (`opensearch/ism-policy.json`, daily rollover + 2-year delete). The data-stream
  template carries the `policy_id`; the policy's `ism_template` auto-attaches it
  to new backing indices. A data stream can't share a name with a plain index, so
  converting an existing plain index needs the one-time reindex migration (README).
- Data lives in the `claude-otel-stack_opensearch-data` volume and survives
  container shutdown; only `down -v` / `teardown.sh --purge` delete it. Back the
  volume up before a major upgrade — they're one-way.
- To insulate data from an accidental `down -v`, set `CLAUDE_OTEL_DATA_DIR` (env
  or `.env`) to an absolute host path before the first start: compose bind-mounts
  it instead of the named volume, and a bind mount isn't removed by `down -v`.
  `setup.sh`/`start.sh` create the dir; `teardown.sh --purge` still deletes it
  deliberately. `.env` is gitignored (machine-specific path).
- The collector config is bind-mounted, so editing it needs a
  `docker compose restart otel-collector` (setup.sh does this) to take effect.
- Keep all ports bound to `127.0.0.1`.

## Privacy & security

- Single node, security plugin **disabled**, all ports `127.0.0.1` only.
- `OTEL_LOG_TOOL_DETAILS=1` captures tool inputs (bash commands, file paths).
  Prompt text is **not** captured (`OTEL_LOG_USER_PROMPTS` is left unset), and
  neither is full tool/API content (`OTEL_LOG_TOOL_CONTENT`,
  `OTEL_LOG_RAW_API_BODIES` are left unset). Traces carry `user_prompt_length`,
  not prompt text.
- **Do not expose these ports beyond localhost.** Security is disabled, so an
  exposed OpenSearch would be an unauthenticated store of your command history.

## Troubleshooting / footguns

- **Collector exits with `exec /otelcol-contrib: no such file or directory`.**
  The official `otel/opentelemetry-collector-contrib` image is `FROM scratch`
  with a dynamically-linked binary whose ELF interpreter isn't resolvable under
  some container runtimes. `Dockerfile.collector` is a multi-stage
  build that copies the upstream binary onto `debian-slim` (glibc); the binary is
  pulled at build time, so no local `otelcol-contrib` copy is needed.
- **`opensearch` exporter logs an "unmaintained component" warning.** Known
  upstream status; it functions correctly for this use case.
- **Panel errors "Could not locate that index-pattern-field".** The bundle ships
  the index pattern with no field list, so any `saved-objects.ndjson` import wipes
  the field cache; OSD 3.x then can't resolve panel fields. Just re-run
  `./scripts/start.sh` — it repopulates the cache from the live mapping on every
  run — then hard-refresh the browser. (No separate fix script; start.sh owns it.)
- **No data on the dashboard.** Confirm `./scripts/enable-telemetry.sh` ran and
  you restarted Claude Code; check `docker logs claude-otel-collector` and that
  `curl localhost:9200/ss4o_logs-claudecode-*/_count` is non-zero.
- **No traces appearing.** Traces are a Claude Code beta and only flow when
  `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` and `OTEL_TRACES_EXPORTER=otlp` are set
  (enable-telemetry.sh sets both — restart Claude Code after running it). Check
  `curl localhost:9200/ss4o_traces-claudecode-*/_count`.
- **OpenSearch's own metrics.** The Prometheus exporter plugin serves cluster,
  node and index metrics at `http://localhost:9200/_prometheus/metrics`. These
  are OpenSearch health metrics, not Claude Code metrics.
- **Metrics aren't captured.** By design — the `opensearch` exporter has no
  metrics support, so this stack stores logs and traces only. Don't set
  `OTEL_METRICS_EXPORTER`; with no metrics pipeline the collector would reject the
  export. Capturing metrics (lines-of-code, commit/PR counts, etc.) needs a
  separate backend such as Prometheus.
