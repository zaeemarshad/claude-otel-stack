#!/usr/bin/env python3
"""Backfill Claude Code cost/telemetry history from local session transcripts.

Claude Code emits OTLP live and cannot replay past sessions, but every session's
messages are on disk under ~/.claude/projects/*/*.jsonl. This walks those
transcripts and reconstructs the two event types the "Cost & Usage" dashboard
reads:

  * claude_code.api_request  -- one per assistant message: token counts + model,
    with a cost_usd computed from per-model rates DERIVED FROM THE LIVE OTLP DATA
    already in OpenSearch (no hardcoded price table).
  * claude_code.tool_result  -- one per tool_use block: tool_name + tool_input,
    with success taken from the paired tool_result's is_error flag.

Records are POSTed as OTLP/HTTP to the collector (:4318) carrying the original
timestamp, so the collector's transform (project / file_path / command / MCP
extraction) and the opensearch exporter handle them exactly as they do live data.

Fidelity notes:
  * tool_decision (permission) and user_prompt events are not reconstructable
    from transcripts and are not emitted.
  * duration_ms is not recorded in transcripts, so it is omitted.
  * cost_usd is computed, not recorded -- see the printed rate table and fit
    residual for accuracy per model.

By default the backfill stops at the earliest live-telemetry timestamp, so it
fills only the gap before telemetry was enabled and never double-counts sessions
that already emitted live data. Override with --before / --all.

Idempotent at session granularity: every backfilled document is tagged
attributes.backfill=transcript, and sessions already present as backfill are
skipped. Use --force to re-send regardless, --purge to delete previously
backfilled docs, --dry-run to report without sending.
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

DEFAULT_PROJECTS = os.path.expanduser("~/.claude/projects")
API_REQUEST = "claude_code.api_request"
TOOL_RESULT = "claude_code.tool_result"
BACKFILL_TAG = "transcript"

# Token components, in the order used by the rate solver / cost formula.
COMPONENTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")

# Models absent from live OTLP borrow rates from a same-tier model present in it.
RATE_FALLBACK = {"claude-opus-4-7": "claude-opus-4-8"}


def http_json(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


# --------------------------------------------------------------------------
# Pricing: solve per-model token rates from the live api_request docs.
# cost_usd = r_in*input + r_out*output + r_cr*cache_read + r_cc*cache_creation.
# Claude Code's cost is a fixed linear function of tokens, so a least-squares
# fit over the live docs recovers the effective rates (the only residual comes
# from cache-creation 5m/1h tiers collapsing into one column).
# --------------------------------------------------------------------------

def solve_4x4(ata, atc):
    """Solve (AtA) x = Atc for a 4x4 system via Gaussian elimination."""
    n = 4
    m = [row[:] + [atc[i]] for i, row in enumerate(ata)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        m[col] = [v / pv for v in m[col]]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col]
                m[r] = [a - f * b for a, b in zip(m[r], m[col])]
    return [m[i][n] for i in range(n)]


def derive_rates(os_url):
    """Return {model: (r_in, r_out, r_cr, r_cc)} derived from live api_request docs."""
    # Accumulate normal-equation sums per model by scrolling all api_request docs.
    ata = defaultdict(lambda: [[0.0] * 4 for _ in range(4)])
    atc = defaultdict(lambda: [0.0] * 4)
    colsum = defaultdict(lambda: [0.0] * 4)  # sum of each token component
    cost_sum = defaultdict(float)            # sum of actual cost_usd
    n_docs = defaultdict(int)
    src = ["attributes.model"] + [f"attributes.{c}" for c in COMPONENTS] + ["attributes.cost_usd"]
    body = {
        "size": 1000,
        "_source": src,
        "query": {"bool": {"must": [{"term": {"body.keyword": API_REQUEST}},
                                    {"exists": {"field": "attributes.cost_usd"}}]}},
    }
    resp = http_json("POST", f"{os_url}/ss4o_logs-claudecode-*/_search?scroll=2m", body)
    sid = resp.get("_scroll_id")
    try:
        while True:
            hits = resp["hits"]["hits"]
            if not hits:
                break
            for h in hits:
                a = h["_source"].get("attributes", {})
                model = a.get("model")
                cost = a.get("cost_usd")
                if not model or cost is None:
                    continue
                x = [float(a.get(c, 0) or 0) for c in COMPONENTS]
                mA, mv, cs = ata[model], atc[model], colsum[model]
                for i in range(4):
                    mv[i] += x[i] * cost
                    cs[i] += x[i]
                    for j in range(4):
                        mA[i][j] += x[i] * x[j]
                cost_sum[model] += cost
                n_docs[model] += 1
            resp = http_json("POST", f"{os_url}/_search/scroll",
                             {"scroll": "2m", "scroll_id": sid})
            sid = resp.get("_scroll_id")
    finally:
        if sid:
            try:
                http_json("DELETE", f"{os_url}/_search/scroll", {"scroll_id": sid})
            except Exception:
                pass

    rates = {}
    print("Derived per-model rates ($/million tokens) from live OTLP data:")
    print(f"  {'model':<28} {'input':>9} {'output':>9} {'cache_rd':>9} {'cache_cr':>9} {'docs':>6} {'fit err':>8}")
    for model in sorted(n_docs):
        sol = solve_4x4(ata[model], atc[model])
        if sol is None:
            print(f"  {model:<28}  (singular system -- skipped)")
            continue
        sol = [max(0.0, r) for r in sol]  # clamp tiny negative rates from collinearity
        rates[model] = tuple(sol)
        # Fit residual: total predicted cost vs total actual cost over live docs.
        pred = sum(sol[i] * colsum[model][i] for i in range(4))
        actual = cost_sum[model]
        err = abs(pred - actual) / actual * 100 if actual else 0.0
        rate_cols = "  ".join(f"{r*1e6:>7.2f}" for r in sol)
        print(f"  {model:<28} {rate_cols} {n_docs[model]:>6} {err:>7.2f}%")
    for missing, proxy in RATE_FALLBACK.items():
        if missing not in rates and proxy in rates:
            rates[missing] = rates[proxy]
            print(f"  {missing:<28}  (no live data -- borrowing {proxy} rates)")
    return rates


def cost_for(rates, model, comps):
    r = rates.get(model)
    if r is None:
        return None
    return sum(r[i] * comps[i] for i in range(4))


# --------------------------------------------------------------------------
# Idempotency helpers
# --------------------------------------------------------------------------

def earliest_live_ts(os_url):
    """Earliest @timestamp of live (non-backfill) telemetry, or None if none yet.

    Backfilled events at or after this point would duplicate live data for the
    same sessions, so by default the backfill stops here and fills only the gap.
    """
    body = {
        "size": 0,
        "query": {"bool": {"must_not": [{"term": {"attributes.backfill.keyword": BACKFILL_TAG}}]}},
        "aggs": {"min": {"min": {"field": "@timestamp"}}},
    }
    try:
        v = http_json("POST", f"{os_url}/ss4o_logs-claudecode-*/_search", body)
        return v["aggregations"]["min"].get("value_as_string")
    except Exception:
        return None


def backfilled_sessions(os_url):
    body = {
        "size": 0,
        "query": {"term": {"attributes.backfill.keyword": BACKFILL_TAG}},
        "aggs": {"s": {"terms": {"field": "attributes.session.id.keyword", "size": 10000}}},
    }
    try:
        resp = http_json("POST", f"{os_url}/ss4o_logs-claudecode-*/_search", body)
        return {b["key"] for b in resp["aggregations"]["s"]["buckets"]}
    except Exception:
        return set()


def purge_backfill(os_url):
    body = {"query": {"term": {"attributes.backfill.keyword": BACKFILL_TAG}}}
    resp = http_json("POST", f"{os_url}/ss4o_logs-claudecode-*/_delete_by_query?refresh=true", body)
    return resp.get("deleted", 0)


# --------------------------------------------------------------------------
# Transcript parsing -> OTLP log records
# --------------------------------------------------------------------------

def iso_to_nanos(ts):
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    return str(int(dt.timestamp() * 1_000_000_000))


def normalise_tool_name(name):
    # Match live convention: mcp__server__tool -> server:tool
    if name.startswith("mcp__"):
        server, _, tool = name[len("mcp__"):].partition("__")
        if tool:
            return f"{server}:{tool}"
    return name


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"stringValue": "true" if value else "false"}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def build_records(path, rates, cutoff=None):
    """Yield OTLP logRecord dicts for one transcript file.

    If cutoff (an ISO8601 UTC timestamp) is given, entries at or after it are
    skipped so backfilled events never overlap live telemetry.
    """
    entries = []
    for line in open(path, encoding="utf-8"):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Map tool_use_id -> is_error from tool_result blocks in user messages.
    is_error = {}
    for e in entries:
        msg = e.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    is_error[b.get("tool_use_id")] = bool(b.get("is_error"))

    for e in entries:
        ts = e.get("timestamp")
        sid = e.get("sessionId")
        msg = e.get("message") or {}
        if not ts or not sid or not isinstance(msg, dict):
            continue
        if cutoff and ts >= cutoff:  # ISO8601 UTC sorts lexically
            continue
        t_nanos = iso_to_nanos(ts)

        # api_request from assistant usage.
        usage = msg.get("usage")
        model = msg.get("model")
        if usage and model:
            comps = [
                int(usage.get("input_tokens", 0) or 0),
                int(usage.get("output_tokens", 0) or 0),
                int(usage.get("cache_read_input_tokens", 0) or 0),
                int(usage.get("cache_creation_input_tokens", 0) or 0),
            ]
            attrs = [
                attr("event.name", "api_request"),
                attr("backfill", BACKFILL_TAG),
                attr("session.id", sid),
                attr("model", model),
                attr("input_tokens", comps[0]),
                attr("output_tokens", comps[1]),
                attr("cache_read_tokens", comps[2]),
                attr("cache_creation_tokens", comps[3]),
            ]
            cost = cost_for(rates, model, comps)
            if cost is not None:
                attrs.append(attr("cost_usd", float(cost)))
            yield {"timeUnixNano": t_nanos,
                   "body": {"stringValue": API_REQUEST},
                   "attributes": attrs}

        # tool_result from each tool_use block.
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name = normalise_tool_name(b.get("name", "unknown"))
                tool_input = json.dumps(b.get("input", {}), separators=(",", ":"))
                err = is_error.get(b.get("id"))
                success = "false" if err else "true"
                yield {"timeUnixNano": t_nanos,
                       "body": {"stringValue": TOOL_RESULT},
                       "attributes": [
                           attr("event.name", "tool_result"),
                           attr("backfill", BACKFILL_TAG),
                           attr("session.id", sid),
                           attr("tool_name", name),
                           attr("tool_input", tool_input),
                           attr("success", success),
                       ]}


def post_batch(endpoint, records):
    payload = {"resourceLogs": [{
        "resource": {"attributes": [attr("service.name", "claude-code")]},
        "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code"},
                       "logRecords": records}],
    }]}
    http_json("POST", endpoint, payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects-dir", default=DEFAULT_PROJECTS)
    ap.add_argument("--collector", default="http://localhost:4318",
                    help="OTLP/HTTP base URL of the collector")
    ap.add_argument("--opensearch", default="http://localhost:9200")
    ap.add_argument("--batch", type=int, default=500, help="log records per POST")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report counts without sending")
    ap.add_argument("--force", action="store_true",
                    help="re-send even for sessions already backfilled")
    ap.add_argument("--purge", action="store_true",
                    help="delete previously backfilled docs, then exit")
    ap.add_argument("--before", metavar="ISO_TS",
                    help="only backfill entries before this UTC timestamp "
                         "(default: earliest live telemetry, to fill only the gap)")
    ap.add_argument("--all", action="store_true",
                    help="ignore the live-telemetry cutoff and backfill every "
                         "transcript entry (use only on a store with no live data)")
    args = ap.parse_args()

    os_url = args.opensearch.rstrip("/")
    logs_endpoint = args.collector.rstrip("/") + "/v1/logs"

    if args.purge:
        n = purge_backfill(os_url)
        print(f"Deleted {n} previously backfilled documents.")
        return

    files = sorted(glob.glob(os.path.join(args.projects_dir, "*", "*.jsonl")))
    if not files:
        print(f"No transcripts found under {args.projects_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(files)} transcript files under {args.projects_dir}\n")

    # Cutoff: don't backfill into the window already covered by live telemetry.
    if args.all:
        cutoff = None
        print("--all: no live-telemetry cutoff; backfilling every entry.\n")
    else:
        cutoff = args.before or earliest_live_ts(os_url)
        if cutoff:
            print(f"Backfilling only entries before {cutoff} "
                  f"({'--before' if args.before else 'earliest live telemetry'}); "
                  f"the rest is already covered by live data.\n")
        else:
            print("No live telemetry found; backfilling every entry.\n")

    rates = {} if args.dry_run else derive_rates(os_url)
    if args.dry_run:
        print("(--dry-run: skipping rate derivation and sending)\n")

    skip = set() if (args.force or args.dry_run) else backfilled_sessions(os_url)
    if skip:
        print(f"Skipping {len(skip)} sessions already backfilled (use --force to re-send).\n")

    batch, sent, n_api, n_tool, n_cost0, seen_sessions = [], 0, 0, 0, 0, set()
    for path in files:
        for rec in build_records(path, rates, cutoff):
            attrs = {a["key"]: a["value"] for a in rec["attributes"]}
            sid = attrs["session.id"]["stringValue"]
            if sid in skip:
                continue
            seen_sessions.add(sid)
            body = rec["body"]["stringValue"]
            if body == API_REQUEST:
                n_api += 1
                if "cost_usd" not in attrs:
                    n_cost0 += 1
            else:
                n_tool += 1
            if args.dry_run:
                continue
            batch.append(rec)
            if len(batch) >= args.batch:
                post_batch(logs_endpoint, batch)
                sent += len(batch)
                batch = []
    if batch and not args.dry_run:
        post_batch(logs_endpoint, batch)
        sent += len(batch)

    print(f"\nSessions processed: {len(seen_sessions)}")
    print(f"api_request events: {n_api}  ({n_cost0} without a computed cost)")
    print(f"tool_result events: {n_tool}")
    if args.dry_run:
        print("\n--dry-run: nothing sent.")
        return

    print(f"Sent {sent} records to {logs_endpoint}")
    # Collector batches on a timeout; give it a moment, then report the count.
    time.sleep(6)
    try:
        http_json("POST", f"{os_url}/ss4o_logs-claudecode-*/_refresh")
        c = http_json("POST", f"{os_url}/ss4o_logs-claudecode-*/_search",
                      {"size": 0, "query": {"term": {"attributes.backfill.keyword": BACKFILL_TAG}}})
        print(f"Backfilled docs now in OpenSearch: {c['hits']['total']['value']}")
    except Exception as ex:
        print(f"(could not confirm count: {ex})")


if __name__ == "__main__":
    main()
