## What changed

<!-- Describe the developer problem and the focused solution. -->

## How to verify

<!-- Include exact commands, adapter/source versions, and synthetic fixture IDs. -->

```text
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
uv run agent-observe demo --headless
```

## User-visible evidence

<!-- Add sanitized screenshots or headless output for TUI/CLI changes. Never attach a real session, database, prompt, credential, customer record, or private tool output. -->

## Claim and data impact

<!-- State effects on schema, adapter maturity, redaction, evidence integrity, replay, metrics provenance, networking, or stored/exported data. Write "none" when applicable. -->

## Checklist

- [ ] The change is focused and excludes unrelated cleanup.
- [ ] Fixtures and examples are synthetic and safe to publish without relying on the sanitizer.
- [ ] Source input still passes through bounds, redaction, and hostile-text sanitization before persistence, rendering, metrics, export, logs, or test artifacts.
- [ ] Missing timestamps, tokens, prices, or resource samples remain explicit rather than becoming zero or success.
- [ ] Replay, comparison, verification, and export do not execute agents or tools.
- [ ] Adapter changes identify the tested upstream version and preserve sanitized unknown fields.
- [ ] Security-sensitive behavior has hostile-input and regression tests.
- [ ] Public behavior changes update documentation and the `Unreleased` changelog.
- [ ] The standard lint, format, test, build, and relevant wheel smoke checks pass.
- [ ] I have read and will follow the Code of Conduct.
