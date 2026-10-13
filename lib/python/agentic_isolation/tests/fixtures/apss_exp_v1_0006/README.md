# Agentic Workspace Fixtures

Shared fixtures for the 0.2.0 operation contract. Implementations in any
language consume these files; `tests/schema_conformance.rs` is the reference
consumer.

- `valid/<schema>/`: documents that MUST satisfy `schemas/<schema>.schema.json`.
- `invalid/`: each file names a `base` success fixture plus `set` and
  `remove` JSON Pointer edits. The edited document MUST be rejected.
- `behaviour/`: scripted call sequences. Each step names the call, its
  arguments, the expected response and its schema, and the total number of
  launches after the step. A scenario that observes a gap seeds `output`
  (`generation`, `produced_bytes`); with the descriptor's `retention_bytes`
  this fixes the oldest retained offset, so every `lost_bytes` is derived,
  not asserted.
- Cursors in these fixtures use the fixture provider's encoding
  `g<generation>:<byte offset>`. Only the fixture oracle parses them; portable
  consumers treat cursors as opaque. On every page, `next_cursor` equals
  `cursor` plus `gap.lost_bytes` plus the decoded length of `data_b64`.
- `launch-v1/`: launch manifests with known verdicts, and the frozen 0.1.0
  launch schema used to prove 0.2.0 verdicts are unchanged.
