# Support

Agent Observability TUI is early-alpha open-source software maintained on a best-effort basis.

## Start here

1. Follow the [quick start](README.md#quick-start).
2. Read [CLI workflows](docs/cli.md) and [Adapters](docs/adapters.md).
3. Run `uv run agent-observe --help` and the relevant subcommand's `--help`.
4. Search existing GitHub issues before opening a new one.

## Where to ask

- **Reproducible bug:** use the
  [bug report form](https://github.com/ContractorKeith/agent-observability-tui/issues/new?template=bug.yml).
- **Focused feature:** use the
  [feature request form](https://github.com/ContractorKeith/agent-observability-tui/issues/new?template=feature.yml).
- **Framework adapter:** use the
  [adapter proposal](https://github.com/ContractorKeith/agent-observability-tui/issues/new?template=adapter.yml).
- **Security vulnerability:** report it privately through [Security](SECURITY.md).

Include the exact command, adapter and adapter version, source format version, session ID if it is
safe to share, operating system, Python version, terminal size, and commit. Prefer a minimal
synthetic trace. Never attach a real prompt, credential, cookie, customer record, private tool
output, database, or unsanitized export.

## Expected boundaries

- `import` reads a completed file; `watch` tails a documented file; `run` launches one command.
- Captured MCP, Hermes, and OpenHands formats are experimental and may need an adapter update when
  their upstream shape changes.
- Missing token, price, RSS, or CPU data remains missing; it is not inferred.
- Evidence verification detects accidental changes to chained sanitized records, not a malicious
  local rewrite.

There is no official PyPI package, hosted service, paid support channel, account recovery process,
passive MCP attachment service, or cloud trace storage.
