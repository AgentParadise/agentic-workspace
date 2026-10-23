# Upstream failure fixtures

Inputs to `classify_upstream_failure` in `agentic_isolation.harnesses.{claude,codex}`
(syntropic137#1605). Ids and request ids are invented; nothing here is a secret.

`claude.jsonl` holds claude CLI `result` events; the classifier reads `result`.

- `quota-oct-15`: recorded live (syntropic137 exec-8ad0e101db7b), quoted on syntropic137#1605.
- `quota-oct-23`: recorded live, quoted in the syntropic137#1605 brief.
- `overloaded`: the `API Error: 529 {...}` shape syntropic137's `EventStreamProcessor`
  documents as the CLI's own; the other `API Error` lines follow that shape with the
  Anthropic API's documented error types. They are not captures.
- `unplaced`: syntropic137's own plain-text example of a fault with no kind.

`codex.jsonl` holds `codex exec --json` `turn.failed` events; the classifier reads
`error.message`.

- capacity: codex `ServerOverloaded`, seen live in syntropic137#1303.
- usage limit: the line recorded 2026-10-06 (syntropic137 PC-83).
- cybersecurity refusal: syntropic137 `tests/fixtures/codex/codex_turn_failed.jsonl`
  (exec-61dad6055e6f).

Claude's 5-hour session limit has no recorded text, so it has no fixture and reads
as `unknown`.
