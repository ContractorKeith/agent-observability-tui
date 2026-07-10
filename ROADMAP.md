# Roadmap

This roadmap describes direction, not promised dates. Issues and pull requests are the source of
truth for active work.

## v0.1 — Local capture, replay, and evidence

The first release line is intentionally narrow:

- native `agenttrace.event/1` JSONL and unstructured supervised-child output;
- experimental captured MCP JSON-RPC, Hermes export, and OpenHands event adapters;
- completed-file import, partial-line-safe file watch, and one argv-based supervised child;
- redaction and hostile-text sanitization before persistence, display, metrics, or export;
- local SQLite storage with deterministic ingest-order replay;
- observed process RSS/CPU, source-reported tokens, and versioned local cost estimates;
- read-only comparison of captured sessions;
- atomic sanitized JSON and Markdown evidence exports; and
- a local SHA-256 chain that detects accidental record edits, insertion, deletion, or reordering.

V0.1 remains source-distributed. PyPI publishing, automatic multi-model reruns, and quality scoring
are not release requirements.

## Candidate next steps

These require separate compatibility, privacy, and threat-model work:

- a deliberate transparent MCP proxy rather than passive attachment claims;
- MCP Streamable HTTP proxying or a focused OTLP receiver;
- signed or externally anchored evidence for stronger integrity guarantees;
- more pinned upstream fixtures and compatibility tests for experimental adapters;
- richer price-catalog maintenance and provenance review;
- additional portable process metrics where platforms expose reliable data; and
- more accessibility, compact-terminal, filtering, and high-rate rendering work.

## Explicit non-goals for v0.1

- Passive attachment to arbitrary existing stdio or HTTP sessions.
- Cloud storage, accounts, collaboration, telemetry upload, or a web dashboard.
- Raw prompt or tool-content retention by default.
- Per-process Apple GPU or unified-memory attribution.
- Live provider billing APIs or claims that local estimates equal an invoice.
- Automatic agent reruns, model quality scores, or autonomous model selection.
- Adversarially secure notarization of evidence.
- General-purpose process sandboxing.
