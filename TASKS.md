# Agent Observability TUI — MVP Tasks

## Sprint 1 — Working, releasable MVP

- [x] Freeze native envelope, source modes, privacy policy, and sanitized fixtures
- [x] Scaffold package, CLI, demo, lint, tests, build, and CI
- [x] Implement adapters, redaction, hostile-text sanitization, and diagnostics
- [x] Implement immutable event persistence, session lifecycle, chain verification, and replay
- [x] Implement aggregation, versioned pricing, resource sampling, and read-only comparison
- [x] Implement safe child supervision and import/watch sources
- [x] Build and test Textual timeline, detail, metrics, sessions, and comparison views
- [x] Implement atomic JSON/Markdown evidence export
- [x] Complete README, architecture/schema/privacy docs, community files, and templates
- [x] Run fresh-eyes spec/security audit, remediate findings, and verify clean wheel install
- [x] Create and verify the public `ContractorKeith/agent-observability-tui` repository

## Backlog

- [ ] Byte-transparent MCP stdio proxy with passthrough contract tests
- [ ] Streamable HTTP and OTLP receivers
- [ ] Hermes SQLite/API event-stream integration
- [ ] OpenHands Agent Server websocket integration
- [ ] HMAC/signing or external chain-head anchoring
- [ ] Calibrated high-volume benchmarks and snapshot regression suite
- [ ] PyPI trusted publishing and signed releases

## Done

The completed items above passed the construction-plan acceptance checks locally on 2026-07-10:
Ruff, 72 tests on Python 3.11 and 3.14, wheel/sdist build, clean Python 3.12 wheel command
smoke, dependency audit, Bandit medium/high scan, workflow YAML parsing, and local-link checks.
The public repository, CI, CodeQL, Security workflow, vulnerability-reporting setting, and exact
local/remote branch equality were also verified before this completion record was published.
