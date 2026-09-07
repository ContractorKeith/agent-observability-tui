# Changelog

All notable changes to Agent Observability TUI are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is early-alpha software distributed from GitHub source and release artifacts. It has
not been published to PyPI.

## [Unreleased]

### Fixed

- Stop dashboard refresh callbacks during Textual shutdown so a removed metrics widget is not
  queried.

### Changed

- Pause scheduled CodeQL and security scans plus Dependabot polling; keep push, pull-request,
  and manually dispatched checks.
- Remove the unsupported dependency-review workflow. The security workflow retains its locked
  dependency audit.

### Added

- Local-first Textual dashboard for live and stored session inspection.
- Native `agenttrace.event/1` JSONL import, file watch, safe child supervision, deterministic
  replay, captured-session comparison, evidence export, and integrity verification commands.
- Stable native and child-output adapters plus experimental captured MCP JSON-RPC, Hermes export,
  and OpenHands event adapters.
- Pre-persistence redaction, hostile-text sanitization, bounded input and rendering, visible
  diagnostics, metadata-only semantic capture, explicit content/value omission markers, and
  unknown-field shape preservation. Explicitly supervised stdout/stderr remains captured as logs.
- SQLite event storage, read-only projections, process RSS/CPU observations, source-reported token
  usage, source-reported cost plus versioned local estimates, and explicit unavailable/unpriced
  states.
- SHA-256 chaining for detecting accidental edits, insertion, deletion, or reordering of canonical
  sanitized records, with documented adversarial limitations.
- Public architecture, schema, adapter, privacy, evidence, security, support, contribution,
  conduct, roadmap, and command documentation.
- Pinned CI, CodeQL, and security workflows.

[Unreleased]: https://github.com/ContractorKeith/agent-observability-tui/commits/main
