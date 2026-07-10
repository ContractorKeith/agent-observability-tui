# Adapters and maturity

Adapters translate a captured source shape into sanitized `agenttrace.event/1` records. Adapter
support means “parse the documented captured format,” not “integrate with every version or attach
to an existing live connection.” Every event records the adapter name and version used.

## V0.1 support matrix

| Adapter | Maturity | Input boundary | Important limit |
| --- | --- | --- | --- |
| `native` | Stable | `agenttrace.event/1` JSONL | Producers still should minimize sensitive content before emitting. |
| Supervised child output | Stable | stdout, stderr, exit lifecycle, and portable process samples from `run` | The child is not sandboxed; unstructured output cannot invent tool semantics. |
| `mcp` | Experimental | Previously captured MCP JSON-RPC 2.0 records | No passive stdio/HTTP attachment, Streamable HTTP proxy, or general MCP interception. |
| `hermes` | Experimental | Documented Hermes session-export JSONL | Upstream export shape drift may require a pinned adapter update. |
| `openhands` | Experimental | Current documented OpenHands SDK event dumps | Upstream event shape drift may require a pinned adapter update. |
| `auto` | Detection mode | One of the supported captured shapes | Explicit selection is preferable for repeatable automation. |

“Stable” refers to the project's v0.1 contract, not to every external producer. Experimental
adapters preserve sanitized unknown field names/shape, replace arbitrary unknown scalar content,
and issue diagnostics so a shape change is visible instead of silently accepted.

## Native traces

Use native JSONL when you control the emitter:

```bash
uv run agent-observe import ./trace.jsonl --adapter native --json
uv run agent-observe watch ./trace.jsonl --adapter native
```

Native producers follow the [event schema](event-schema.md). File watch consumes complete appended
lines and reports partial writes, truncation, rotation, malformed data, or source loss.

## Captured MCP JSON-RPC

The `mcp` adapter parses records representing already-captured JSON-RPC 2.0 requests, responses,
errors, and notifications and maps available request/tool correlation without inventing missing
relationships.

```bash
uv run agent-observe import ./captured-mcp.jsonl --adapter mcp --json
uv run agent-observe watch ./captured-mcp.jsonl --adapter mcp
```

V0.1 does not discover a process, tap an arbitrary stdio stream, attach to an HTTP connection, or
place itself transparently between an MCP client and server. If another program writes a supported
capture file, `watch` can follow that file.

## Hermes exports

Export a session using the documented command for the installed Hermes version, inspect and
minimize the resulting data, then import the JSONL:

```bash
uv run agent-observe import ./hermes-session.jsonl --adapter hermes --json
```

Hermes compatibility is experimental until more upstream versions are pinned in tests. Include
the Hermes version and a synthetic minimized record when reporting a compatibility problem.

## OpenHands event dumps

Export events using the documented facility for the installed OpenHands SDK version, inspect and
minimize the data, then import it:

```bash
uv run agent-observe import ./openhands-events.jsonl --adapter openhands --json
```

OpenHands compatibility is experimental. A successful parse does not imply support for every
runtime, UI, server, or historical event version in the wider OpenHands ecosystem.

## Supervised commands

`run` launches exactly one child command using an argument vector and `shell=False`:

```bash
uv run agent-observe run --cwd ./agent-project -- python agent.py --task synthetic-demo
```

The supervisor drains stdout and stderr concurrently, records lifecycle events, forwards signals,
cleans up the dedicated process group it creates, and samples portable RSS/CPU when available. A
child that intentionally creates a new session/process group can escape that cleanup; this is not
a sandbox. The supervisor does not automatically turn arbitrary stdout into MCP or tool events.
Choose an adapter only when the child output matches that documented captured format:

```bash
uv run agent-observe run --adapter native -- python emit_native_trace.py
```

The child command may access the network, credentials, and filesystem according to the user's
environment. Agent Observability TUI is an observer and supervisor, not a sandbox.

## Auto detection

`auto` selects among recognized formats and records the resulting adapter and version:

```bash
uv run agent-observe import ./trace.jsonl --adapter auto --json
```

For CI, reproducible evidence collection, and regression fixtures, choose the intended adapter
explicitly so an upstream shape change cannot silently select a different interpretation.

## Adapter contribution requirements

An adapter contribution must include:

- the upstream source and exact tested version or documented schema;
- synthetic, publishable golden fixtures;
- malformed, duplicate, out-of-order, ANSI/OSC, markup, oversized, nested-secret, and unknown-field
  cases relevant to the format;
- deterministic correlation rules and explicit missing-data behavior;
- diagnostics for unsupported or ambiguous shapes; and
- documentation that distinguishes capture parsing from live attachment or proxying.

Never contribute a real agent session. See [Contributing](../CONTRIBUTING.md) for sanitized-data
rules.
