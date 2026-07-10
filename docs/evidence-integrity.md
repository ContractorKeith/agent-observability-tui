# Evidence integrity and its limits

Agent Observability TUI chains canonical sanitized event records with SHA-256 in deterministic
ingest order. The chain is a local consistency check designed to expose accidental edits,
insertion, deletion, or reordering after capture.

It is **not** adversarially tamper-proof evidence, a signature, a timestamp authority, or proof
that a source told the truth.

## What is chained

The integrity input is the canonical representation of each accepted **sanitized** event together
with policy and version context and the preceding chain value. Raw unbounded source bytes are not
the evidence record because they are not allowed to bypass redaction and hostile-text
sanitization.

Conceptually:

```text
record_hash[n] = SHA-256(
  canonical_sanitized_event[n]
  + policy_and_schema_context
  + record_hash[n - 1]
)
```

The implementation's canonical serializer defines the exact bytes. Field ordering or whitespace
in an exported presentation is not substituted for that serializer.

The scope is deliberately narrow: canonical sanitized events plus the redaction policy are
chained. Session projections, derived summaries, process resource samples, and export formatting
metadata are outside the chain and are labeled that way in exports.

## Ordering and finalization

Events are chained by collector-assigned `ingest_sequence`, not by source timestamp. This keeps
verification deterministic when source timestamps are absent, equal, delayed, or out of order.

A finalized session records the expected chain state and completion context. An interrupted or
still-live capture may be explicitly incomplete. “Incomplete” is not equivalent to “invalid,” and
neither state should be silently rewritten to “verified.”

## What verification can detect

Given the stored finalized chain state and canonicalization policy, verification can identify:

- an edited canonical event;
- a record inserted into the sequence;
- a record deleted from the sequence;
- records reordered relative to ingest sequence;
- a broken preceding-link relationship; or
- missing or inconsistent policy/version context needed to recompute the chain.

Run verification directly:

```bash
uv run agent-observe verify SESSION_ID
uv run agent-observe verify SESSION_ID --json
```

Use `--db PATH` when verifying a non-default store.

## What verification cannot prove

The chain does not prove:

- who produced or collected an event;
- that source timestamps are accurate;
- that tool output, usage, model identity, or process metrics are truthful;
- that redaction retained every fact relevant to an investigation;
- that no event was omitted before the collector accepted input;
- that a local attacker did not rewrite the database and recompute every hash;
- that an export has been independently witnessed or externally timestamped; or
- that a cost estimate matches a provider invoice.

A user with sufficient local access can modify both records and chain metadata. Stronger claims
would require signing, protected keys, or an external anchor and a separate threat model.

## Exports

JSON and Markdown evidence exports are written atomically from sanitized stored data. They include
the applicable schema, filters, safety-policy context, provenance, and chain status so a reader can
understand what was included and what remains unknown.

```bash
uv run agent-observe export SESSION_ID ./evidence.json --format json
uv run agent-observe export SESSION_ID ./evidence.md --format markdown
uv run agent-observe verify-export ./evidence.json --json
```

An atomic write avoids presenting a partially written file as complete. It does not stop a later
edit. Standalone machine verification applies to JSON exports; Markdown includes human-readable
`agenttrace.export/1` context. Preserve the original file, verification output, application
version, and database context when evidence matters.

## Recommended interpretation

Use these phrases:

- “The stored sanitized chain verified under the recorded schema and policy.”
- “Verification detected a local consistency failure.”
- “The capture is incomplete, so the finalized-chain check is unavailable.”

Avoid these claims:

- “The evidence is tamper-proof.”
- “This proves the agent performed the action.”
- “The timestamp is notarized.”
- “The export contains the unmodified raw trace.”

For the wider trust boundary, read [Privacy and threat model](privacy-and-threat-model.md).
