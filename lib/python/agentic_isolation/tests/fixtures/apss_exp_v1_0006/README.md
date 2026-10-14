# Agentic Workspace Fixtures

Shared fixtures for the 0.2.x operation contract. Implementations in any
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
- `behaviour/hazard-*.json`: lifecycle hazards (spec 5.1.11). Each names its
  `hazard` id. Steps may carry `given` (conditions the provider harness
  establishes before the call), `settles_within_seconds` (the call returns
  within this bound), `expect_one_of` (each correct response, in place of
  `expect`), and `descendant_alive` on a response (whether any workload
  process, detached descendants included, survives the call). A response
  with `descendant_alive: true` is never `cancelled`. The unconfirmed cancel
  outcome is `OperationError` kind `timeout` carrying the `ref`. Further step
  fields:
  - `operation_records`: the provider's operation records for the workspace
    once the call returns. Required, and 0, under a destroy condition.
  - `repeat_until_complete`: on `observe` only, repeat following
    `next_cursor` until a page is complete or the call errors; the bound
    covers the whole loop.
  - Calls `file_exists` and `read_file` take `workspace` and a relative
    `path`. A probe result matches `file-probe.schema.json` (fixture-level,
    not a contract schema); a read of a non-regular file is an
    `OperationError`.
  Bounds are enforced by a watchdog outside the provider's event loop.
  Conditions:
  - `detached_descendant`: the workload double-forked a descendant that
    called `setsid` and was reparented when its parent exited.
  - `caller_cancelled_during_confirmation`: the caller abandons the call
    while the provider is confirming termination.
  - `launch_header_not_seen`: the provider has not yet read the launch
    header, so it does not know the workload leader.
  - `destroyed_during_credential_resolution`: `destroy` runs while this
    start is blocked resolving a credential binding.
  - `workspace_destroyed`: the workspace has been destroyed.
  - `backend_stuck_removing`: the backend reports removal in progress on
    every query, forever. Its first step is a timeout drain.
  - `timeout_elapsed`: the operation's `timeout_seconds` has passed.
  - `observation_gap`: output overran retention before this read.
  - `fifo_in_workspace`: the workload left a FIFO with no writer at the
    step's `path`. A scenario giving it drives both `file_exists` and
    `read_file`.
- `valid/workspace-provider/small-retention.json` is a descriptor whose
  16-byte retention lets a drain lose bytes on one page and complete on a
  later one, which then carries `loss_detected`.
- Cursors in these fixtures use the fixture provider's encoding
  `g<generation>:<byte offset>`. Only the fixture oracle parses them; portable
  consumers treat cursors as opaque. On every page, `next_cursor` equals
  `cursor` plus `gap.lost_bytes` plus the decoded length of `data_b64`.
- `launch-v1/`: launch manifests with known verdicts, and the frozen 0.1.0
  launch schema used to prove 0.2.0 verdicts are unchanged.
