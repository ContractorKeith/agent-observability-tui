# Security policy

Agent Observability TUI processes potentially hostile traces and can launch a user-selected child
command. Its security boundary therefore includes source adapters, redaction, terminal-text
sanitization, size limits, SQLite persistence, evidence exports, child-process supervision,
dependencies, packaging, and GitHub workflows.

The application is local-first and does not require a hosted service, account, API key, or
telemetry upload. It is not a sandbox. A command explicitly supplied to `agent-observe run --`
has the same authority as that command would have outside this application.

## Supported versions

During the 0.x period, security fixes are provided for the current development branch and the
latest 0.x minor line only.

| Version | Supported |
| --- | --- |
| Current `main` branch | Yes |
| 0.1.x, while it is the latest 0.x line | Yes |
| Older 0.x minor lines | No |
| Untagged forks or modified builds | No |

Agent Observability TUI is currently source-distributed and is not published on PyPI.

## Report a vulnerability privately

Do not open a public issue or attach a sensitive trace to a report.

[Report a vulnerability with a private GitHub security advisory](https://github.com/ContractorKeith/agent-observability-tui/security/advisories/new)

Include:

- the affected commit, tag, adapter, and source format version;
- operating system, Python version, and installation method;
- minimal synthetic reproduction steps or a proof of concept;
- the expected security impact and trust boundary crossed;
- whether the behavior reaches the database, TUI, export, logs, failure output, or test artifacts;
- any suggested mitigation; and
- whether disclosure is time-sensitive.

You should receive an acknowledgement within seven days. The maintainer will validate the report,
coordinate remediation and a release when needed, and credit the reporter unless anonymity is
requested. Please allow a reasonable remediation window before public disclosure.

## In scope

- A representative secret or active terminal control sequence surviving into persistence,
  rendering, exports, logs, failure output, or test artifacts.
- Command injection, unsafe argument handling, or signal/process-group defects with security impact,
  or an undocumented use of a shell.
- Crafted trace input that bypasses documented size, parsing, sanitization, or markup boundaries.
- Integrity verification that accepts an edited, inserted, deleted, or reordered stored record
  under the documented accidental-corruption model.
- Unexpected network communication or telemetry from the Agent Observability TUI runtime itself.
- Unsafe owner-only storage behavior, dependency or supply-chain compromise, workflow permissions,
  action pinning, or secret exposure.

## Out of scope and documented limits

- Reports that treat the local SHA-256 chain as adversarially tamper-proof. A local attacker who
  can rewrite the database can recompute it.
- A user intentionally running an untrusted command with `agent-observe run`; the command is not
  sandboxed.
- Network or filesystem behavior performed by that user-selected child command.
- Descendants that intentionally escape the supervised process group by creating a new session;
  `run` is not a sandbox.
- Sensitive content that the user publishes after ignoring the requirement to inspect and
  sanitize it, unless the documented sanitizer itself failed on a reproducible representative
  pattern.
- Passive attachment, transparent MCP proxying, cloud services, live provider billing, or Apple
  GPU/unified-memory attribution, which are not v0.1 features.
- Scanner-only reports without a reproducible impact.

For ordinary defects, use the bug-report form. For setup questions, see [Support](SUPPORT.md). The
full boundary is documented in [Privacy and threat model](docs/privacy-and-threat-model.md).
