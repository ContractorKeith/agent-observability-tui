# Agent Observability TUI — Product Requirements Document

**Status:** Approved for MVP
**Date:** 2026-07-10
**Owner:** ContractorKeith

## Problem

Developers running local agents and MCP-enabled workflows have logs, token counters,
tool calls, and process metrics scattered across terminals and framework-specific files.
That makes a failed loop difficult to reconstruct and model choices difficult to compare.

## User

Developers who run local or hybrid agents from the terminal, including Hermes,
OpenHands-style loops, MCP clients and servers, and custom orchestrators.

## Solution

A local-first Textual application that ingests a small, source-neutral event envelope,
normalizes supported captured formats, redacts sensitive values before persistence,
and presents live traces, deterministic replay, resource/token/cost summaries,
read-only model comparisons, and exportable evidence logs.

## Modes and claim boundaries

- `import`: ingest a completed JSONL trace.
- `watch`: tail a documented trace file as it is written.
- `run`: launch one child command without a shell and observe its stdout, stderr,
  exit state, and process metrics.
- `replay`: read a stored session without executing tools or subprocesses.

The MVP parses captured MCP JSON-RPC. It does not passively attach to an arbitrary
existing stdio or HTTP connection. Hermes and OpenHands adapters target documented
exported/event JSON shapes and are labeled experimental until pinned against more
upstream versions.

## Success criteria

1. `agent-observe demo` launches a useful TUI without external services or API keys.
2. A native or supported JSONL trace can be imported, watched, stored, and replayed
   with stable ordering and the same derived summary.
3. Representative secrets and hostile terminal control sequences never reach the
   database, TUI rendering, exports, logs, or test artifacts in their original form.
4. Sessions show observed process RSS/CPU, provider-reported token usage, versioned
   cost estimates, errors, and missing-data status without inventing values.
5. Captured sessions can be compared read-only by duration, status, tool/error counts,
   tokens, estimated cost, and peak sampled RSS.
6. Evidence exports are atomic, sanitized, schema-versioned, and verifiable for
   accidental edit, deletion, insertion, or reordering of stored records.
7. The wheel installs on Python 3.11+ and CI passes lint, tests, build, and smoke install.

## In scope

- Textual live/replay dashboard with timeline, details, metrics, and comparison views
- Native `agenttrace.event/1` JSONL envelope and documented emitter contract
- Captured MCP JSON-RPC, Hermes session export, and current OpenHands event adapters
- SQLite event store with immutable event rows, mutable projections, WAL, and migrations
- Redaction, hostile-text sanitization, bounded rendering, and sanitized hash chain
- Safe argv-based child supervision and portable psutil RSS/CPU sampling
- Versioned local price catalog with explicit provenance and `unpriced` state
- JSON and Markdown evidence export
- Demo fixture, tests, CI, and public-project documentation

## Out of scope

- Passive attachment to arbitrary running stdio/HTTP sessions
- MCP Streamable HTTP proxying or a general OTLP receiver
- Cloud storage, accounts, collaboration, or telemetry sent off-device
- Automatic multi-model reruns or any quality score
- Full raw prompt/tool-content retention by default; semantic adapters retain metadata/shape and
  explicit omission markers, while supervised stdout/stderr remains an intentional log capture
- Per-process Apple GPU or unified-memory attribution
- Live provider billing APIs, PyPI publication, plugin marketplace, or web dashboard
- Adversarially secure notarization of evidence; the local SHA-256 chain detects
  accidental corruption of sanitized records but can be recomputed by a local attacker

## Key decisions

- Local-only by default; no network dependency in the runtime path.
- Redact and sanitize before canonicalization, persistence, display, or export.
- Persist accepted events before projecting them into the UI.
- Keep source timestamp and collector timestamp separate; replay by ingest sequence.
- Preserve unknown sanitized fields and explicit diagnostics instead of silently dropping data.
- Unknown tokens, prices, and platform metrics remain unknown.

## Open questions

None block the MVP. Transparent MCP proxying and signed/externally anchored evidence
are roadmap items that require separate threat-model and compatibility work.
