#!/usr/bin/env python3
"""Generate dashboards/saved-objects.ndjson for OpenSearch Dashboards import.

Builds one index pattern + a set of aggregation-based visualizations + one
dashboard ("Claude Code – Cost & Usage"). Field references are derived from the
actual ss4o document schema produced by the OTel opensearch exporter:
  - time field:        @timestamp
  - event fields:      attributes.* (strings have a .keyword subfield)
  - numeric fields:    attributes.duration_ms / cost_usd / input_tokens / output_tokens
  - event type:        body.keyword  (e.g. "claude_code.tool_result")
"""
import json
import os

INDEX_PATTERN_ID = "claude-code-logs"
INDEX_PATTERN_TITLE = "ss4o_logs-claudecode-*"
OUT = os.path.join(os.path.dirname(__file__), "saved-objects.ndjson")

objects = []


def index_ref():
    return [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index",
             "type": "index-pattern", "id": INDEX_PATTERN_ID}]


def search_source(query=None):
    # indexRefName ties this searchSource to the index-pattern entry in the
    # object's `references` array; without it OSD reports
    # "Trying to initialize aggs without index pattern".
    src = {"indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
           "query": {"language": "kuery", "query": query or ""}, "filter": []}
    return {"searchSourceJSON": json.dumps(src)}


def viz(vid, title, vis_state, query=None):
    objects.append({
        "id": vid,
        "type": "visualization",
        "attributes": {
            "title": title,
            "visState": json.dumps(vis_state),
            "uiStateJSON": "{}",
            "description": "",
            "version": 1,
            "kibanaSavedObjectMeta": search_source(query),
        },
        "references": index_ref(),
    })


# --- index pattern ---
objects.append({
    "id": INDEX_PATTERN_ID,
    "type": "index-pattern",
    "attributes": {"title": INDEX_PATTERN_TITLE, "timeFieldName": "@timestamp"},
    "references": [],
})

TOOL_RESULT = 'body.keyword: "claude_code.tool_result"'
TOOL_DECISION = 'body.keyword: "claude_code.tool_decision"'
API_REQUEST = 'body.keyword: "claude_code.api_request"'
API_ERROR = 'body.keyword: "claude_code.api_error"'


# Readable column/series names. Without these, OpenSearch shows raw labels like
# "Sum of attributes.input_tokens"; the helpers below set the agg's `customLabel`
# from this map (falling back to the field name). avg/max/min get the verb
# prefixed so e.g. avg and max of duration_ms stay distinct.
FIELD_LABELS = {
    "attributes.cost_usd": "Cost (USD)",
    "attributes.input_tokens": "Input Tokens",
    "attributes.output_tokens": "Output Tokens",
    "attributes.cache_read_tokens": "Cache Read Tokens",
    "attributes.cache_creation_tokens": "Cache Creation Tokens",
    "attributes.duration_ms": "Duration (ms)",
    "attributes.tool_name.keyword": "Tool",
    "attributes.model.keyword": "Model",
    "attributes.session.id.keyword": "Session",
    "attributes.success.keyword": "Success",
    "attributes.decision.keyword": "Decision",
    "attributes.status_code": "Status Code",
    "attributes.project.keyword": "Project",
    "attributes.file_path.keyword": "File",
    "attributes.command.keyword": "Command",
    "body.keyword": "Event Type",
}


def _label_for(field, mtype=None):
    base = FIELD_LABELS.get(field, field)
    if mtype in ("avg", "max", "min"):
        return f"{mtype.capitalize()} {base}"
    return base


def _with_label(params, label):
    if label:
        params["customLabel"] = label
    return params


def count_agg(aid="1", label="Count"):
    return {"id": aid, "enabled": True, "type": "count", "schema": "metric",
            "params": _with_label({}, label)}


def terms_agg(aid, field, size=15, order_by="1", label=None):
    return {"id": aid, "enabled": True, "type": "terms", "schema": "bucket",
            "params": _with_label({"field": field, "orderBy": order_by, "order": "desc",
                                   "size": size, "otherBucket": False,
                                   "missingBucket": False}, label or _label_for(field))}


def date_hist(aid, label=None):
    return {"id": aid, "enabled": True, "type": "date_histogram", "schema": "segment",
            "params": _with_label({"field": "@timestamp", "interval": "auto",
                                   "min_doc_count": 1}, label)}


def metric_agg(aid, mtype, field, label=None):
    return {"id": aid, "enabled": True, "type": mtype, "schema": "metric",
            "params": _with_label({"field": field}, label or _label_for(field, mtype))}


# 1. Total events (metric)
viz("cc-total-events", "Claude Code – Total Events",
    {"title": "Total Events", "type": "metric",
     "aggs": [count_agg("1")],
     "params": {"metric": {"metricColorMode": "None", "labels": {"show": True},
                           "style": {"fontSize": 48}}}})

# 2. Total cost (metric, api_request)
viz("cc-total-cost", "Claude Code – Total Cost (USD)",
    {"title": "Total Cost", "type": "metric",
     "aggs": [metric_agg("1", "sum", "attributes.cost_usd")],
     "params": {"metric": {"labels": {"show": True}, "style": {"fontSize": 48}}}},
    query=API_REQUEST)

# 3. Total tokens in/out (metric, api_request)
viz("cc-total-tokens", "Claude Code – Total Tokens (in / out)",
    {"title": "Total Tokens", "type": "metric",
     "aggs": [metric_agg("1", "sum", "attributes.input_tokens"),
              metric_agg("2", "sum", "attributes.output_tokens")],
     "params": {"metric": {"labels": {"show": True}, "style": {"fontSize": 36}}}},
    query=API_REQUEST)

# 5. Tool usage (count by tool, horizontal bar, stacked by success) — tool_result only.
# The success split colours each tool's bar by pass/fail, so the most-used tools and
# their failure proportion are both visible at a glance.
viz("cc-tool-usage", "Claude Code – Tool Usage (count, by outcome)",
    {"title": "Tool Usage", "type": "horizontal_bar",
     "aggs": [count_agg("1"), terms_agg("2", "attributes.tool_name.keyword", size=25),
              {"id": "3", "enabled": True, "type": "terms", "schema": "group",
               "params": {"field": "attributes.success.keyword", "orderBy": "1",
                          "order": "desc", "size": 5, "otherBucket": False,
                          "missingBucket": False, "customLabel": "Success"}}],
     "params": {"type": "horizontal_bar", "addLegend": True, "legendPosition": "right",
                "seriesParams": [{"data": {"id": "1", "label": "Count"}, "type": "histogram",
                                  "mode": "stacked", "valueAxis": "ValueAxis-1", "show": True}],
                "categoryAxes": [{"id": "CategoryAxis-1", "type": "category",
                                  "position": "left", "show": True,
                                  "scale": {"type": "linear"}}],
                "valueAxes": [{"id": "ValueAxis-1", "name": "BottomAxis-1", "type": "value",
                               "position": "bottom", "show": True,
                               "scale": {"type": "linear", "mode": "normal"}}],
                "grid": {"valueAxis": "ValueAxis-1"}}},
    query=TOOL_RESULT)

# 8. Tool decisions (pie) — tool_decision only
viz("cc-tool-decisions", "Claude Code – Tool Permission Decisions",
    {"title": "Tool Decisions", "type": "pie",
     "aggs": [count_agg("1"), terms_agg("2", "attributes.decision.keyword", size=5),
              terms_agg("3", "attributes.tool_name.keyword", size=25)],
     "params": {"type": "pie", "addLegend": True, "isDonut": True,
                "legendPosition": "right", "labels": {"show": True, "values": True}}},
    query=TOOL_DECISION)

# 9. Cost over time (area, date histogram) — api_request only
viz("cc-cost-over-time", "Claude Code – Cost Over Time (USD)",
    {"title": "Cost Over Time", "type": "area",
     "aggs": [metric_agg("1", "sum", "attributes.cost_usd"), date_hist("2")],
     "params": {"type": "area", "addLegend": True,
                "seriesParams": [{"data": {"id": "1", "label": "Cost (USD)"}, "type": "area",
                                  "mode": "stacked", "valueAxis": "ValueAxis-1",
                                  "interpolate": "linear", "show": True}],
                "categoryAxes": [{"id": "CategoryAxis-1", "type": "category",
                                  "position": "bottom", "show": True,
                                  "scale": {"type": "linear"}}],
                "valueAxes": [{"id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value",
                               "position": "left", "show": True,
                               "scale": {"type": "linear", "mode": "normal"}}],
                "grid": {"categoryLines": False}}},
    query=API_REQUEST)

# 10. Tokens by model (data table) — api_request only.
# Includes cache_read / cache_creation so cache utilisation is visible per model.
viz("cc-tokens-by-model", "Claude Code – Tokens by Model",
    {"title": "Tokens by Model", "type": "table",
     "aggs": [metric_agg("1", "sum", "attributes.input_tokens"),
              metric_agg("3", "sum", "attributes.output_tokens"),
              metric_agg("5", "sum", "attributes.cache_read_tokens"),
              metric_agg("6", "sum", "attributes.cache_creation_tokens"),
              metric_agg("4", "sum", "attributes.cost_usd"),
              terms_agg("2", "attributes.model.keyword", size=15)],
     "params": {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False,
                "showTotal": True, "totalFunc": "sum"}},
    query=API_REQUEST)

# 12. Cost & tokens by session (data table) — api_request only.
# Answers "which session is burning cost/tokens?". Claude Code does not emit a
# project/cwd attribute, so session.id is the finest available grouping.
viz("cc-cost-by-session", "Claude Code – Cost & Tokens by Session",
    {"title": "Cost by Session", "type": "table",
     "aggs": [metric_agg("1", "sum", "attributes.cost_usd"),
              metric_agg("3", "sum", "attributes.input_tokens"),
              metric_agg("4", "sum", "attributes.output_tokens"),
              metric_agg("5", "min", "@timestamp", label="Started"),
              terms_agg("2", "attributes.session.id.keyword", size=20)],
     "params": {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False,
                "showTotal": True, "totalFunc": "sum"}},
    query=API_REQUEST)

# 13. Cache vs fresh input tokens (metric) — api_request only.
# Cache reads are billed far cheaper than fresh input; this is the cost-efficiency signal.
viz("cc-cache-efficiency", "Claude Code – Cache vs Fresh Tokens",
    {"title": "Cache Tokens", "type": "metric",
     "aggs": [metric_agg("1", "sum", "attributes.cache_read_tokens"),
              metric_agg("2", "sum", "attributes.cache_creation_tokens"),
              metric_agg("3", "sum", "attributes.input_tokens")],
     "params": {"metric": {"labels": {"show": True}, "style": {"fontSize": 30}}}},
    query=API_REQUEST)

# 14. API latency over time (line, avg + max) — api_request only.
viz("cc-api-latency", "Claude Code – API Latency Over Time (ms)",
    {"title": "API Latency", "type": "line",
     "aggs": [metric_agg("1", "avg", "attributes.duration_ms"),
              metric_agg("3", "max", "attributes.duration_ms"),
              date_hist("2")],
     "params": {"type": "line", "addLegend": True, "addTimeMarker": False,
                "seriesParams": [
                    {"data": {"id": "1", "label": "Avg ms"}, "type": "line", "mode": "normal",
                     "valueAxis": "ValueAxis-1", "show": True, "drawLinesBetweenPoints": True,
                     "showCircles": True},
                    {"data": {"id": "3", "label": "Max ms"}, "type": "line", "mode": "normal",
                     "valueAxis": "ValueAxis-1", "show": True, "drawLinesBetweenPoints": True,
                     "showCircles": True}],
                "categoryAxes": [{"id": "CategoryAxis-1", "type": "category",
                                  "position": "bottom", "show": True,
                                  "scale": {"type": "linear"}}],
                "valueAxes": [{"id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value",
                               "position": "left", "show": True,
                               "scale": {"type": "linear", "mode": "normal"}}],
                "grid": {"categoryLines": False}}},
    query=API_REQUEST)

# === cost & usage panels (logs-only subset of a Grafana-style cost board) =====
# Only metrics derivable from the logs-only pipeline are included. Lines-of-code,
# prompt-rate and subagent-cost panels from the reference board are intentionally
# omitted — Claude Code emits those as OTel metrics (not logs) or not at all here.

# C1. Active sessions — unique session count over the selected range.
viz("cc-active-sessions", "Claude Code – Active Sessions",
    {"title": "Active Sessions", "type": "metric",
     "aggs": [{"id": "1", "enabled": True, "type": "cardinality", "schema": "metric",
               "params": {"field": "attributes.session.id.keyword",
                          "customLabel": "Active Sessions"}}],
     "params": {"metric": {"labels": {"show": True}, "style": {"fontSize": 48}}}})

# C2. Average per API request (cost / tokens / latency). Per-request, NOT
# per-session: classic OSD can't divide sum by cardinality for a true per-session
# average, so this reports the mean of each api_request event.
viz("cc-avg-request", "Claude Code – Avg per API Request",
    {"title": "Avg per Request", "type": "metric",
     "aggs": [metric_agg("1", "avg", "attributes.cost_usd"),
              metric_agg("2", "avg", "attributes.input_tokens"),
              metric_agg("3", "avg", "attributes.output_tokens"),
              metric_agg("4", "avg", "attributes.duration_ms")],
     "params": {"metric": {"labels": {"show": True}, "style": {"fontSize": 24}}}},
    query=API_REQUEST)

# C3. Tokens over time (area, in / out) — api_request only.
viz("cc-tokens-over-time", "Claude Code – Tokens Over Time",
    {"title": "Tokens Over Time", "type": "area",
     "aggs": [metric_agg("1", "sum", "attributes.input_tokens"),
              metric_agg("3", "sum", "attributes.output_tokens"),
              date_hist("2")],
     "params": {"type": "area", "addLegend": True,
                "seriesParams": [
                    {"data": {"id": "1", "label": "Input Tokens"}, "type": "area",
                     "mode": "stacked", "valueAxis": "ValueAxis-1",
                     "interpolate": "linear", "show": True},
                    {"data": {"id": "3", "label": "Output Tokens"}, "type": "area",
                     "mode": "stacked", "valueAxis": "ValueAxis-1",
                     "interpolate": "linear", "show": True}],
                "categoryAxes": [{"id": "CategoryAxis-1", "type": "category",
                                  "position": "bottom", "show": True,
                                  "scale": {"type": "linear"}}],
                "valueAxes": [{"id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value",
                               "position": "left", "show": True,
                               "scale": {"type": "linear", "mode": "normal"}}],
                "grid": {"categoryLines": False}}},
    query=API_REQUEST)

# C4. Cost distribution by model (donut) — api_request only.
viz("cc-cost-by-model", "Claude Code – Cost Distribution by Model",
    {"title": "Cost by Model", "type": "pie",
     "aggs": [metric_agg("1", "sum", "attributes.cost_usd"),
              terms_agg("2", "attributes.model.keyword", size=10)],
     "params": {"type": "pie", "addLegend": True, "isDonut": True,
                "legendPosition": "right", "labels": {"show": True, "values": True}}},
    query=API_REQUEST)

# C5. Code-edit acceptance (accept vs reject on Edit / Write / MultiEdit) — tool_decision.
viz("cc-edit-acceptance", "Claude Code – Code Edit Acceptance",
    {"title": "Code Edit Acceptance", "type": "pie",
     "aggs": [count_agg("1"), terms_agg("2", "attributes.decision.keyword", size=5)],
     "params": {"type": "pie", "addLegend": True, "isDonut": True,
                "legendPosition": "right", "labels": {"show": True, "values": True}}},
    query=(TOOL_DECISION + ' and (attributes.tool_name.keyword: "Edit" or '
           'attributes.tool_name.keyword: "Write" or '
           'attributes.tool_name.keyword: "MultiEdit")'))

# C6. Tool decision sources (config / user) — tool_decision.
viz("cc-decision-sources", "Claude Code – Tool Decision Sources",
    {"title": "Tool Decision Sources", "type": "pie",
     "aggs": [count_agg("1"),
              terms_agg("2", "attributes.source.keyword", size=10, label="Decision Source")],
     "params": {"type": "pie", "addLegend": True, "isDonut": True,
                "legendPosition": "right", "labels": {"show": True, "values": True}}},
    query=TOOL_DECISION)

# === efficiency KPIs (Vega single-stat tiles) ================================
# Classic OSD aggregations can't divide one aggregate by another (cost per token,
# per-session averages, run-rate projections) and TSVB metric panels show only the
# last time bucket, not a whole-range total. Vega queries OpenSearch directly and
# computes the ratio client-side, honouring the dashboard time filter via
# %context% / %timefield%. min/max on @timestamp return epoch millis for run-rates.
API_REQUEST_DSL = {"term": {"body.keyword": "claude_code.api_request"}}


def vega_viz(vid, title, panel_title, filter_query, aggs, value_expr, text_signal,
             value_label, color="#54B399"):
    # OSD Vega forbids url.body.query alongside %context%/%timefield% (which inject
    # the dashboard time range + global filters). So keep %context% for the time
    # filter and apply the per-tile event-type restriction via a `filter` aggregation
    # instead; format.property reads through that filter bucket so value_expr can
    # reference the metrics directly (datum.<metric>.value).
    spec = {
        "$schema": "https://vega.github.io/schema/vega/v5.json",
        "autosize": {"type": "fit", "contains": "padding"},
        "config": {"kibana": {"hideWarnings": True}},
        "data": [{
            "name": "src",
            "url": {"%context%": True, "%timefield%": "@timestamp",
                    "index": INDEX_PATTERN_TITLE,
                    "body": {"size": 0,
                             "aggs": {"f": {"filter": filter_query, "aggs": aggs}}}},
            "format": {"property": "aggregations.f"},
            "transform": [{"type": "formula", "as": "value", "expr": value_expr}],
        }],
        "marks": [
            {"type": "text", "from": {"data": "src"}, "encode": {"enter": {
                "text": {"signal": text_signal},
                "x": {"signal": "width/2"}, "y": {"signal": "height/2 - 8"},
                "align": {"value": "center"}, "baseline": {"value": "middle"},
                "fontSize": {"value": 38}, "fill": {"value": color}}}},
            {"type": "text", "encode": {"enter": {
                "text": {"value": value_label},
                "x": {"signal": "width/2"}, "y": {"signal": "height/2 + 24"},
                "align": {"value": "center"}, "baseline": {"value": "middle"},
                "fontSize": {"value": 12}, "fill": {"value": "#98A2B3"}}}},
        ],
    }
    objects.append({
        "id": vid, "type": "visualization",
        "attributes": {
            "title": title,
            "visState": json.dumps({"title": panel_title, "type": "vega",
                                    "params": {"spec": json.dumps(spec)}}),
            "uiStateJSON": "{}", "description": "", "version": 1,
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(
                {"query": {"language": "kuery", "query": ""}, "filter": []})},
        },
        "references": [],
    })


_TOKENS = {"intok": {"sum": {"field": "attributes.input_tokens"}},
           "outtok": {"sum": {"field": "attributes.output_tokens"}}}
_COST = {"cost": {"sum": {"field": "attributes.cost_usd"}}}
_SESS = {"sess": {"cardinality": {"field": "attributes.session.id.keyword"}}}
_SPAN = {"mints": {"min": {"field": "@timestamp"}},
         "maxts": {"max": {"field": "@timestamp"}}}

# V1. Cost per 1K tokens
vega_viz("cc-cost-per-1k", "Claude Code – Cost per 1K Tokens", "Cost per 1K Tokens",
         API_REQUEST_DSL, {**_COST, **_TOKENS},
         "datum.cost.value / ((datum.intok.value + datum.outtok.value) / 1000)",
         "format(datum.value, '$,.4f')", "Cost per 1K tokens")

# V2. Tokens per dollar
vega_viz("cc-tokens-per-dollar", "Claude Code – Tokens per Dollar", "Tokens per $",
         API_REQUEST_DSL, {**_COST, **_TOKENS},
         "(datum.intok.value + datum.outtok.value) / datum.cost.value",
         "format(datum.value, ',.0f')", "Tokens per $", color="#6092C0")

# V3. Cache hit rate %
vega_viz("cc-cache-hit", "Claude Code – Cache Hit Rate", "Cache Hit Rate",
         API_REQUEST_DSL,
         {"cacheread": {"sum": {"field": "attributes.cache_read_tokens"}},
          "intok": {"sum": {"field": "attributes.input_tokens"}}},
         "100 * datum.cacheread.value / (datum.cacheread.value + datum.intok.value)",
         "format(datum.value, '.1f') + '%'", "Cache hit rate", color="#9170B8")

# V4. Avg cost / session
vega_viz("cc-avg-cost-session", "Claude Code – Avg Cost / Session", "Avg Cost / Session",
         API_REQUEST_DSL, {**_COST, **_SESS},
         "datum.cost.value / datum.sess.value",
         "format(datum.value, '$,.4f')", "Avg cost / session")

# V5. Avg tokens / session
vega_viz("cc-avg-tokens-session", "Claude Code – Avg Tokens / Session", "Avg Tokens / Session",
         API_REQUEST_DSL, {**_TOKENS, **_SESS},
         "(datum.intok.value + datum.outtok.value) / datum.sess.value",
         "format(datum.value, ',.0f')", "Avg tokens / session", color="#6092C0")

# V6. Cost burn rate ($/hr) — run-rate over the selected range.
vega_viz("cc-burn-rate", "Claude Code – Cost Burn Rate ($/hr)", "Cost $/hr",
         API_REQUEST_DSL, {**_COST, **_SPAN},
         "datum.cost.value / max((datum.maxts.value - datum.mints.value) / 3600000, 0.0167)",
         "format(datum.value, '$,.4f')", "Cost / hour (run-rate)", color="#E7664C")

# V7. Daily cost projection — daily run-rate over the selected range.
vega_viz("cc-daily-projection", "Claude Code – Daily Cost Projection", "Daily Projection",
         API_REQUEST_DSL, {**_COST, **_SPAN},
         "datum.cost.value / max((datum.maxts.value - datum.mints.value) / 86400000, 0.01)",
         "format(datum.value, '$,.2f')", "Daily projection (run-rate)", color="#D6BF57")

# V8. Monthly cost projection — daily run-rate x 30.
vega_viz("cc-monthly-projection", "Claude Code – Monthly Cost Projection", "Monthly Projection",
         API_REQUEST_DSL, {**_COST, **_SPAN},
         "30 * datum.cost.value / max((datum.maxts.value - datum.mints.value) / 86400000, 0.01)",
         "format(datum.value, '$,.2f')", "Monthly projection (run-rate)", color="#D6BF57")

# --- cost & usage dashboard (the single Claude Code dashboard) ---
# 48-column grid layout.
cost_panels = [
    # efficiency KPI tiles (Vega) — two rows of four
    ("cc-cost-per-1k", 0, 0, 12, 8),
    ("cc-tokens-per-dollar", 12, 0, 12, 8),
    ("cc-cache-hit", 24, 0, 12, 8),
    ("cc-burn-rate", 36, 0, 12, 8),
    ("cc-avg-cost-session", 0, 8, 12, 8),
    ("cc-avg-tokens-session", 12, 8, 12, 8),
    ("cc-daily-projection", 24, 8, 12, 8),
    ("cc-monthly-projection", 36, 8, 12, 8),
    # totals
    ("cc-total-cost", 0, 16, 12, 8),
    ("cc-total-tokens", 12, 16, 12, 8),
    ("cc-active-sessions", 24, 16, 12, 8),
    ("cc-total-events", 36, 16, 12, 8),
    ("cc-avg-request", 0, 24, 24, 10),
    ("cc-cache-efficiency", 24, 24, 24, 10),
    ("cc-cost-over-time", 0, 34, 48, 12),
    ("cc-tokens-over-time", 0, 46, 48, 12),
    ("cc-cost-by-model", 0, 58, 16, 15),
    ("cc-tokens-by-model", 16, 58, 32, 15),
    ("cc-edit-acceptance", 0, 73, 16, 15),
    ("cc-decision-sources", 16, 73, 16, 15),
    ("cc-tool-decisions", 32, 73, 16, 15),
    ("cc-tool-usage", 0, 88, 48, 16),
    ("cc-cost-by-session", 0, 104, 48, 15),
    ("cc-api-latency", 0, 119, 48, 12),
]

cost_panels_json = []
cost_refs = []
for i, panel in enumerate(cost_panels, start=1):
    vid, x, y, w, h = panel[:5]
    ptype = panel[5] if len(panel) > 5 else "visualization"
    pid = f"panel_{i}"
    cost_panels_json.append({
        "version": "3.7.0",
        "gridData": {"x": x, "y": y, "w": w, "h": h, "i": pid},
        "panelIndex": pid,
        "embeddableConfig": {},
        "panelRefName": f"panel_{i}",
    })
    cost_refs.append({"name": f"panel_{i}", "type": ptype, "id": vid})

objects.append({
    "id": "claude-code-cost-usage",
    "type": "dashboard",
    "attributes": {
        "title": "Claude Code – Cost & Usage",
        "description": "Cost, token and efficiency overview — logs-only subset of a Grafana-style cost board.",
        "panelsJSON": json.dumps(cost_panels_json),
        "optionsJSON": json.dumps({"useMargins": True, "hidePanelTitles": False}),
        "version": 1,
        "timeRestore": True,
        "timeTo": "now",
        "timeFrom": "now-24h",
        "refreshInterval": {"pause": False, "value": 30000},
        "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []})},
    },
    "references": cost_refs,
})

with open(OUT, "w") as f:
    for o in objects:
        f.write(json.dumps(o) + "\n")
    f.write(json.dumps({"exportedCount": len(objects),
                        "missingRefCount": 0, "missingReferences": []}) + "\n")

print(f"Wrote {len(objects)} saved objects to {OUT}")
