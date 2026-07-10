# Agent Observability TUI — Construction Plan

**Status:** Completed and published
**Date:** 2026-07-10
**Execution mode:** direct on `main` for a new repository, with an atomic initial release commit

## Objective

Deliver a documented, tested, installable MVP and verified public GitHub repository for
a local Textual dashboard that imports, watches, runs, stores, replays, compares, and exports
sanitized agent trace sessions.

## Completion contract

- Native traces and supported captured formats enter one versioned envelope.
- Every durable/rendered/exported payload has passed redaction and hostile-text sanitization.
- Live ingestion persists before UI projection and reports diagnostics instead of silent loss.
- Replay is deterministic and side-effect free.
- Token, price, and resource fields state observed/derived/estimated/unknown provenance.
- Community health, security, support, privacy, architecture, schema, changelog, and release docs exist.
- Local branch is clean and `ahead=0 behind=0` against the public ContractorKeith remote.

## Dependency graph

```text
1 contract/scaffold
  -> 2 safety + canonical pipeline
       -> 3 persistence + replay
       -> 4 sources + supervision
            -> 5 analysis + TUI + export
                 -> 6 audits + publish
```

Steps 3 and 4 may run in parallel after step 2 because their file ownership is separate.

## Step 1 — Contract and scaffold

**Context:** The repo is new. The project-planner/mvp-builder repositories provide handoff
and phase-gate conventions, not code. Use their PRD/STACK/TASKS/CONTEXT shapes while building
a conventional Python package.

**Tasks:**

1. Commit planning handoff and this construction plan.
2. Add `pyproject.toml`, package metadata, source layout, CLI placeholder, demo fixture, CI,
   ignore rules, and license.
3. Define `agenttrace.event/1` and source-mode claim boundaries in docs and fixtures.

**Verification:** package metadata parses; CLI help runs; fixture JSONL parses.

**Exit:** a fresh agent can identify scope, commands, and invariants without chat context.

**Rollback:** remove scaffold commit; no external state exists yet.

## Step 2 — Safety and canonical pipeline

**Context:** Trace input is hostile and can contain secrets, terminal escapes, oversized lines,
malformed JSON, unknown schema versions, or misleading framework fields.

**Tasks:**

1. Implement immutable event/usage/source/correlation records with source and observed time.
2. Implement strict size bounds, control-sequence stripping, markup-neutral display text,
   recursive secret-key/value redaction, truncation metadata, and visible diagnostics.
3. Implement native, MCP capture, Hermes export, OpenHands event, and auto adapters.
4. Preserve sanitized unknown field names/shape and adapter/version metadata while replacing
   arbitrary unknown scalar content with explicit omission markers.

**Verification:** golden tests cover malformed, duplicate, unknown, out-of-order, ANSI/OSC,
markup, oversized, token-like, password, authorization, cookie, and nested secret input.

**Exit:** no representative secret or active terminal control reaches normalized output.

**Rollback:** adapters remain isolated inbound ports and may be disabled independently.

## Step 3 — Persistence, integrity, and replay

**Context:** Sanitized accepted events must be durable before display. SQLite itself is mutable,
so only event rows are immutable; summaries and retention metadata are projections.

**Tasks:**

1. Add migrations, WAL, one-writer transactions, owner-only files, event/session/resource tables,
   and indexes on session sequence and correlation IDs.
2. Chain canonical sanitized event records using SHA-256 with policy/version metadata.
3. Implement finalization and verification that detects edit, insertion, deletion, and reorder.
4. Implement session listing, deterministic replay, and explicit incomplete-chain state.

**Verification:** replay twice yields identical events/summary/head; mutation tests fail verification;
concurrent read and abrupt close recover without updating event rows.

**Exit:** stored sessions can be verified and replayed without touching external systems.

**Rollback:** migration is versioned; an empty data directory recreates the store.

## Step 4 — Sources and safe child supervision

**Context:** `import`, `watch`, and `run` have different trust and lifecycle boundaries. Captured
MCP parsing is not passive attachment.

**Tasks:**

1. Implement completed-file import and partial-line-safe file watch.
2. Launch child argv after `--` with `shell=False`, concurrent stdout/stderr draining, explicit cwd,
   line bounds, signal forwarding, dedicated-process-group cleanup, an explicit detached-child
   limitation, and terminal session events.
3. Sample observed RSS/CPU with process identity checks and graceful unavailable states.
4. Persist gap/diagnostic events for truncation, rotation, malformed data, or source loss.

**Verification:** tests cover partial writes, truncation, rotation, fast exit, stderr bursts, shell
metacharacters, Ctrl-C cleanup, sampling access errors, and no deadlock.

**Exit:** supervised runs terminate cleanly and watched files never fail silently.

**Rollback:** each source implements a small lifecycle contract and can be disabled independently.

## Step 5 — Analysis, TUI, and evidence export

**Context:** Presentation reads only sanitized stored/projected data. High event rates may coalesce
UI refreshes but never silently drop durable accepted events.

**Tasks:**

1. Aggregate sessions and per-model duration/status/tool/error/token/cost/RSS metrics with provenance
   and missing-data counts; snapshot price catalog/version per session.
2. Build Textual sessions, live timeline, selected detail, metric strip, evidence, and comparison views.
3. Support pause/filter/resume without pausing ingestion; cap rendered detail with visible truncation.
4. Export JSON/Markdown atomically with schema, filters, policy, provenance, and chain status.
5. Add demo and noninteractive headless summary for verification.

**Verification:** exact pricing fixtures including unknown/effective-date cases; read-only comparison;
Pilot tests at 80x24 and 120x40; hostile content renders inert; export re-verifies.

**Exit:** demo, import/watch/run, replay, compare, and export form one coherent user journey.

**Rollback:** UI/export depend on read APIs only and may change without migrating raw events.

## Step 6 — Audit, release, and public repository

**Context:** New public observability software needs exact privacy/integrity claims and a usable
contributor path. Publication is not complete until the remote state and CI are visible.

**Tasks:**

1. Add README, architecture, schema, adapters, privacy/threat model, roadmap, changelog, support,
   contributing, security, Contributor Covenant, issue/PR templates, Dependabot, and CI.
2. Run requirement-by-requirement fresh-eyes audit and security review; remediate all critical/high findings.
3. Run lint, format, tests, build, clean-wheel smoke install, demo summary, replay, comparison, export,
   and chain verification.
4. Create public GitHub repo, push `main`, configure topics/description/private vulnerability reporting,
   inspect Actions, and verify local/remote commit equality with `ahead=0 behind=0`.

**Exit:** public URL loads, default branch contains the verified artifacts, checks pass, and local worktree is clean.

**Rollback:** publication changes are additive; if checks fail, fix forward before tagging any release.

## Eval gates

| Gate | Required evidence |
|---|---|
| Canonical ingest | Stable sequence/correlation, unknown field shape retained, diagnostics visible |
| Privacy | Secrets absent from DB, renderables, exports, logs, failures, and CI artifacts |
| Hostile input | ANSI/OSC/markup/oversized input is inert and visibly truncated |
| Replay | Two runs produce identical records, summaries, and finalized chain head |
| Cost truth | Exact catalog math; unknown stays unpriced; version/effective date shown |
| Resource truth | RSS/CPU observed or unavailable; no Apple GPU/unified-memory claim |
| Comparison | Captured sessions only; correct counts/percentiles/missing-data status |
| Packaging | Wheel installs cleanly and all public commands smoke-test |
| Publication | GitHub public, CI passing, branch clean, `ahead=0 behind=0` |

## Plan mutation protocol

Record scope changes in this file and `CHANGELOG.md`. A step may be split when one risk dominates,
reordered only when dependencies remain satisfied, or deferred only if the PRD claim and README are
changed first. Never mark a task complete because code exists; attach the verification result.
