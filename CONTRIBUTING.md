# Contributing to Agent Observability TUI

Thanks for helping make local agent traces easier to understand without weakening the user's
privacy or overstating what the evidence proves.

## Start with the right issue

- Use the bug form for a reproducible CLI, adapter, storage, replay, TUI, or export problem.
- Use the feature form for a focused product proposal.
- Use the adapter form before adding or materially changing a framework format.
- Report security vulnerabilities privately through [the security policy](SECURITY.md).

Small documentation and test fixes may go directly to a pull request. Discuss broad schema,
storage, threat-model, integrity, or source-lifecycle changes first.

## Set up the project

```bash
git clone https://github.com/ContractorKeith/agent-observability-tui.git
cd agent-observability-tui
uv sync --locked --all-groups
uv run pytest -q
git switch -c feat/short-description
```

## Protect trace data

Contributions must be publishable without trusting the project's sanitizer.

- Use synthetic traces and fictional names, prompts, paths, hosts, tool arguments, and outputs.
- Never contribute real credentials, API keys, cookies, authorization headers, prompts, customer
  data, repository secrets, home-directory contents, or private agent output.
- Do not submit a real trace merely because it was redacted. Automated redaction is risk
  reduction, not a guarantee that every sensitive value was found.
- Use obvious fake sentinel values when testing redaction. Assert that the original sentinel is
  absent from normalized events, database reads, renderables, exports, logs, failures, and test
  artifacts.
- Encode hostile terminal sequences deliberately in test construction; do not paste active
  control sequences into issues, pull-request descriptions, snapshots, or fixtures.
- Keep fixture files small and document why each field is necessary.
- Review generated JSON, Markdown, screenshots, and failure output before attaching them.

If a sanitized sample is still necessary to reproduce an adapter bug, first reduce it to the
smallest synthetic shape that fails. If that is impossible, contact the maintainer privately; do
not post it in an issue.

## Preserve the architecture boundaries

- Route all source input through an adapter, redactor, and hostile-text sanitizer before
  canonicalization, persistence, display, metrics, or export.
- Persist accepted events before projecting them to a live view.
- Keep source timestamps and collector timestamps separate; replay by ingest sequence.
- Preserve sanitized unknown fields and emit explicit diagnostics for malformed, truncated,
  rotated, unknown, or unavailable input.
- Keep event rows immutable. Session summaries, retention metadata, and other projections may be
  updated separately.
- Keep replay, comparison, verification, and export side-effect free with respect to agents and
  tools.
- Never use `shell=True` for child execution or render untrusted Rich/Textual markup.
- Keep tokens source-reported, costs catalog-derived, and missing resource metrics unknown.

Read [Architecture](docs/architecture.md), [Event schema](docs/event-schema.md), and
[Privacy and threat model](docs/privacy-and-threat-model.md) before changing the ingest path.

## Run the checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
uv run agent-observe demo --headless
```

Add focused tests for every changed boundary. Adapter tests should name the source version they
represent. Safety tests must cover malformed, duplicate, out-of-order, oversized, ANSI/OSC,
markup, nested secret, authorization, cookie, and token-like inputs as relevant.

## Submit a reviewable pull request

Explain the user problem, behavior before and after, exact commands run, and any claim-boundary or
schema effect. Include a sanitized screenshot for TUI changes when useful. Keep unrelated cleanup
out of the diff and update documentation plus the `Unreleased` changelog when public behavior
changes.

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). By contributing, you agree
that your contribution is licensed under the repository's [MIT License](LICENSE).
