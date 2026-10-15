# Mutation evidence for LH-6 `loss_detected` tests

Removed before merge (CI job `mutations-not-merged`).

| Patch | Bug | Killing tests |
|---|---|---|
| `01-drop-python-loss-detected.patch` | Python spool never sets `loss_detected` | `lib/python/agentic_isolation/tests/test_spool_loss_detected.py` (rules b, c, d) |
| `02-drop-rust-then-some.patch` | Rust spool never sets `loss_detected` | Rust, run locally: `loss_is_sticky_from_a_redacted_gap_to_the_final_empty_page`. `mutation-evidence.yml` runs pytest only, so the result is recorded in the PR body. |
