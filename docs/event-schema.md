# `agenttrace.event/1` event schema

`agenttrace.event/1` is the source-neutral canonical envelope used by Agent Observability TUI.
Native JSONL uses one JSON object per complete line. Framework adapters normalize their supported
captured shapes into the same envelope after applying bounds, redaction, and hostile-text
sanitization.

The exact installed CLI and package version are the source of truth. Schema changes require a new
schema identifier or a documented backward-compatible extension; adapters must not silently
reinterpret an existing field.

## Envelope

| Field | Required | Meaning |
| --- | --- | --- |
| `schema` | Yes | Literal schema identifier `agenttrace.event/1`. |
| `event_id` | Yes | Stable identifier for this canonical event. |
| `session_id` | Yes | Identifier grouping events into one captured session. |
| `ingest_sequence` | Yes | Collector-assigned deterministic ordering within the session. |
| `source_timestamp` | Yes, nullable | Time reported by the source. It remains unknown when the source provides none. |
| `observed_timestamp` | Yes | Time the collector observed or accepted the event. |
| `adapter` | Yes | Adapter name used to interpret the input. |
| `adapter_version` | Yes | Version of the adapter contract used for normalization. |
| `category` | Yes | Broad source-neutral event family used for grouping and projections. |
| `kind` | Yes | More specific event kind within the category. |
| `phase` | No | Lifecycle phase when the source supplies or the adapter can deterministically map one. |
| `status` | No | Source or lifecycle status; absence is not treated as success. |
| `direction` | No | Captured direction when meaningful to the source format. |
| `transport` | No | Captured transport metadata when meaningful and known. |
| `correlation` | Yes, may be empty | Normalized request, tool-call, run, parent, trace, and span identifiers as applicable. |
| `token_usage` | No | Source-reported token information with provenance; never guessed. |
| `cost` | No | Source-reported or locally derived USD cost with explicit provenance; unknown models remain unpriced. |
| `attributes` | Yes | Sanitized metadata used by views, with content/unknown scalar omission markers. |
| `raw` | Yes | Metadata-only structural source view; full semantic content is not retained by default. |

Optional values may be absent or null according to the serializer. `correlation`, `attributes`,
and `raw` remain present in canonical serialized records, though the first two may be empty and
`raw` may be null. Consumers must not convert an absent source timestamp, status, token count,
cost, or metric into zero or success.

## Minimal illustrative record

This example shows the envelope, not a promise that every adapter emits the same category, kind,
or attributes:

```json
{
  "schema": "agenttrace.event/1",
  "event_id": "evt-demo-0001",
  "session_id": "session-demo",
  "ingest_sequence": 1,
  "source_timestamp": null,
  "observed_timestamp": "2026-07-10T12:00:00Z",
  "adapter": "native",
  "adapter_version": "1",
  "category": "log",
  "kind": "message",
  "correlation": {},
  "attributes": {
    "stream": "stdout",
    "text": "synthetic demo output"
  },
  "raw": {}
}
```

Use synthetic values in examples and contributions. A valid schema does not make its content safe
to publish.

## Ordering and identity

`ingest_sequence` is the replay and chain order. It is assigned by the collector and must be
stable for the stored session. `source_timestamp` and `observed_timestamp` answer different
questions and are never collapsed:

- source time says when the source claims the event occurred;
- observed time says when the collector saw it; and
- ingest sequence says where the accepted event belongs in deterministic replay.

Events with missing, duplicate, or out-of-order source timestamps remain replayable. A source's
own identifier may be retained in `correlation`; `event_id` identifies the canonical record.

## Correlation

The correlation object carries only identifiers that are present or can be mapped without
guessing:

| Field | Meaning |
| --- | --- |
| `trace_id` | Trace identifier reported by the source. |
| `span_id` | Current span identifier reported by the source. |
| `parent_span_id` | Parent span identifier reported by the source. |
| `request_id` | Protocol or framework request/response correlation identifier. |
| `tool_call_id` | Tool invocation/result correlation identifier. |
| `run_id` | Framework run, loop, or execution identifier. |
| `extra` | Bounded sanitized source correlation fields without a stable normalized key. |

Fields are optional and remain absent when unknown. An adapter must:

- preserve a meaningful source identifier after sanitization;
- map request and response identifiers consistently;
- avoid manufacturing trace or parent relationships that the source did not report; and
- keep unknown sanitized correlation fields available for diagnosis.

Correlation values are data, not trusted database keys or terminal markup.

## Usage, cost, and resource data

`token_usage` is present only for source-reported usage:

| Field | Meaning |
| --- | --- |
| `input_tokens` | Source-reported input tokens, when present. |
| `output_tokens` | Source-reported output tokens, when present. |
| `reasoning_tokens` | Source-reported reasoning tokens, when separately present. |
| `cache_read_tokens` | Source-reported cache-read tokens, when present. |
| `cache_write_tokens` | Source-reported cache-write tokens, when present. |
| `total_tokens` | Source-reported total; it is not fabricated when the source omits it. |
| `provenance` | Adapter/source context for the usage values. |

Each numeric field is optional; missing values remain missing rather than becoming zero.

`cost` may be source-reported (actual or estimated) or derived from the local price catalog.
It is never presented as a verified provider invoice:

| Field | Meaning |
| --- | --- |
| `amount_usd` | Decimal-text USD estimate, avoiding binary floating-point representation. |
| `currency` | `USD` for the v0.1 local catalog. |
| `provenance` | Whether the value is source-reported or locally estimated and why. |
| `price_catalog_version` | Optional version of the local catalog used for the estimate. |
| `price_effective_from` | Optional reviewed effective date frozen with a local estimate. |

Source-reported cost retains its source provenance. Bundled v0.1 catalog entries are illustrative
rather than current provider prices. Users must configure provider rates for a provider-specific
local estimate. If neither a trustworthy source value nor an effective local price can be
resolved, the event or session remains explicitly `unpriced` rather than receiving a zero-valued
cost.

Portable RSS and CPU samples are represented as observed resource events or projections. The
schema does not represent per-process Apple GPU or unified-memory attribution.

## Metadata-only content policy

Semantic adapters preserve allowlisted metadata values and unknown field names/shape after safety
processing. Prompt, tool argument/result, body, and other known content values become
`[CONTENT OMITTED]`; arbitrary unknown scalar values become `[VALUE OMITTED]`. Known normalized
metadata belongs in `attributes`; the bounded structural source view belongs in `raw`. Sanitized
stdout/stderr from an explicitly supervised child is marked `observed-log` and is the deliberate
exception. Consumers must never render `raw` as trusted markup.

Redaction and sanitization metadata must make truncation or replacement visible. An adapter must
emit a diagnostic rather than silently discard malformed records, unknown schema versions, gaps,
rotation, truncation, or source loss.

## Native emitter rules

A native emitter should:

1. write UTF-8 JSONL with one complete object per line;
2. use the literal `agenttrace.event/1` identifier;
3. provide stable event and session identifiers;
4. report source time only when known;
5. include tokens only when reported by the provider or framework;
6. avoid embedding raw credentials or unnecessary prompt/tool content; and
7. flush each complete line so `watch` can observe it without reading a partial record.

The collector still applies its own safety policy. Emitters should minimize sensitive data before
it reaches that boundary.
