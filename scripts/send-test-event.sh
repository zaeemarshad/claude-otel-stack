#!/usr/bin/env bash
# Send a synthetic OTLP/HTTP log batch mimicking Claude Code events, to verify the
# pipeline end-to-end without needing a live Claude Code session.
set -euo pipefail

ENDPOINT="${OTEL_HTTP_ENDPOINT:-http://localhost:4318}/v1/logs"
NOW_NS="$(date +%s)000000000"

read -r -d '' PAYLOAD <<JSON || true
{
  "resourceLogs": [{
    "resource": { "attributes": [
      { "key": "service.name", "value": { "stringValue": "claude-code" } },
      { "key": "session.id", "value": { "stringValue": "test-session-0001" } }
    ]},
    "scopeLogs": [{
      "scope": { "name": "com.anthropic.claude_code" },
      "logRecords": [
        {
          "timeUnixNano": "${NOW_NS}",
          "body": { "stringValue": "claude_code.tool_result" },
          "attributes": [
            { "key": "event.name", "value": { "stringValue": "tool_result" } },
            { "key": "tool_name", "value": { "stringValue": "Bash" } },
            { "key": "tool_input", "value": { "stringValue": "{\"command\":\"ls /Users/test-user/demo-project\"}" } },
            { "key": "success", "value": { "stringValue": "true" } },
            { "key": "duration_ms", "value": { "intValue": "142" } },
            { "key": "decision_source", "value": { "stringValue": "config" } }
          ]
        },
        {
          "timeUnixNano": "${NOW_NS}",
          "body": { "stringValue": "claude_code.api_request" },
          "attributes": [
            { "key": "event.name", "value": { "stringValue": "api_request" } },
            { "key": "model", "value": { "stringValue": "claude-opus-4-8" } },
            { "key": "cost_usd", "value": { "doubleValue": 0.0123 } },
            { "key": "input_tokens", "value": { "intValue": "1500" } },
            { "key": "output_tokens", "value": { "intValue": "320" } },
            { "key": "duration_ms", "value": { "intValue": "2100" } }
          ]
        },
        {
          "timeUnixNano": "${NOW_NS}",
          "body": { "stringValue": "claude_code.tool_decision" },
          "attributes": [
            { "key": "event.name", "value": { "stringValue": "tool_decision" } },
            { "key": "tool_name", "value": { "stringValue": "Edit" } },
            { "key": "tool_input", "value": { "stringValue": "{\"file_path\":\"/Users/test-user/demo-project/main.py\"}" } },
            { "key": "decision", "value": { "stringValue": "accept" } },
            { "key": "source", "value": { "stringValue": "config" } }
          ]
        }
      ]
    }]
  }]
}
JSON

echo "POST ${ENDPOINT}"
curl -sf -X POST "${ENDPOINT}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}" && echo "  -> logs sent OK" || { echo "  -> FAILED"; exit 1; }

# Also exercise the traces pipeline (Claude Code's beta tracing signal). The
# opensearch exporter routes this span to ss4o_traces-claudecode-telemetry.
TRACE_ENDPOINT="${OTEL_HTTP_ENDPOINT:-http://localhost:4318}/v1/traces"
START_NS="${NOW_NS}"
END_NS="$(( NOW_NS + 1500000000 ))"

read -r -d '' TRACE_PAYLOAD <<JSON || true
{
  "resourceSpans": [{
    "resource": { "attributes": [
      { "key": "service.name", "value": { "stringValue": "claude-code" } }
    ]},
    "scopeSpans": [{
      "scope": { "name": "com.anthropic.claude_code" },
      "spans": [{
        "traceId": "5b8efff798038103d269b633813fc60c",
        "spanId": "eee19b7ec3c1b174",
        "name": "claude_code.interaction",
        "kind": 1,
        "startTimeUnixNano": "${START_NS}",
        "endTimeUnixNano": "${END_NS}",
        "attributes": [
          { "key": "interaction.sequence", "value": { "intValue": "1" } },
          { "key": "user_prompt_length", "value": { "intValue": "42" } }
        ],
        "status": {}
      }]
    }]
  }]
}
JSON

echo "POST ${TRACE_ENDPOINT}"
curl -sf -X POST "${TRACE_ENDPOINT}" \
  -H "Content-Type: application/json" \
  -d "${TRACE_PAYLOAD}" && echo "  -> trace sent OK" || { echo "  -> FAILED"; exit 1; }
