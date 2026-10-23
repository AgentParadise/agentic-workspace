# Mutations for #103 (E2B operations reattach, public pause/resume)

Each patch re-introduces one bug in
`lib/python/agentic_isolation/agentic_isolation/providers/e2b_operations.py`
or, for 04, `providers/e2b.py`. The killing tests run over the e2b-double
(`tests/e2b_fake.py`) in its executing mode, so no E2B account is needed.

Named tests to run (repo-relative; 10 node ids, none parametrised):

```
lib/python/agentic_isolation/tests/test_e2b_reattach.py
```

| Patch | Bug | Killing tests |
|---|---|---|
| `01-reattach-memory-registry-only.patch` | `reattach` consults only the in-memory registry, so a fresh instance refuses every ref another one started | `test_fresh_instance_reads_running_then_the_exit_status`, `test_fresh_instance_cancel_kills_the_workload`, `test_a_probe_that_does_not_answer_is_unavailable_not_stopped`, `test_a_workload_gone_without_a_status_is_lost_not_cancelled`, `test_a_cancel_that_is_not_confirmed_raises_and_is_never_cancelled`, `test_a_reattached_operation_does_not_pretend_to_have_its_output` |
| `02-fresh-cancel-stopped-without-kill.patch` | a fresh-instance cancel reports `cancelled` without running the termination | `test_fresh_instance_cancel_kills_the_workload` (the workload's pid is still alive), `test_a_cancel_that_is_not_confirmed_raises_and_is_never_cancelled` (the refused termination is never run, so nothing raises) |
| `03-reattach-ownership-unchecked.patch` | the probe accepts any token, so a forged token, or another operation's, reattaches and can cancel | `test_a_wrong_token_cannot_reattach_or_cancel` |
| `04-pause-noop.patch` | `E2BWorkspaceProvider.pause` returns without pausing the sandbox | `test_pause_and_resume_reach_the_sandbox` |
| `05-gone-without-status-reads-cancelled.patch` | a workload gone with no exit status and no confirmed termination reads `cancelled` instead of `lost` | `test_a_workload_gone_without_a_status_is_lost_not_cancelled` |
| `06-unconfirmed-cancel-reads-cancelled.patch` | a reattached cancel is marked confirmed before the termination, so a refused one still reads `cancelled` | `test_a_cancel_that_is_not_confirmed_raises_and_is_never_cancelled` |

Locally, at the commit that added them, over the e2b-double: 01 failed 6
tests, 02 failed 2, 03 1, 04 1, 05 1 (2 in one of two runs), 06 1. That
run is not mutation evidence; the trusted run is `mutation-evidence.yml` on
a GitHub runner.

## Added by the verification fix (B1, B2)

The fix for verification findings B1 (an exited leader hid a live writer)
and B2 (workload-writable state authorised cancellation) changed the lines
patches 03 and 05 touch, so they no longer apply. They are left as they
were. 10 and 11 make the same mutations against the fixed code. The
selection is still the one file above, now 14 node ids, none parametrised.

| Patch | Bug | Killing tests |
|---|---|---|
| `07-exit-marker-outranks-live-writer.patch` | an exit status or `stopped` word in the reattach directory outranks a live process carrying the marker | `test_a_writer_left_by_an_exited_leader_is_running_until_cancel_kills_it`, `test_a_forged_stopped_word_does_not_hide_a_live_workload` |
| `08-owner-digest-not-bound-to-id.patch` | the owner digest leaves out the operation id, so a workload's copy of its own directory under a new id passes reattach | `test_a_workload_cannot_aim_its_token_at_another_operation` |
| `09-recorded-group-trusted-on-reattach.patch` | a reattached cancel signals the group read from the workload's directory without checking its leader, so a rewritten group kills another operation | `test_a_workload_cannot_aim_its_token_at_another_operation` |
| `10-reattach-ownership-unchecked-rebased.patch` | 03 against the fixed code: the probe accepts any owner digest | `test_a_wrong_token_cannot_reattach_or_cancel`, `test_a_workload_cannot_aim_its_token_at_another_operation` |
| `11-gone-without-status-reads-cancelled-rebased.patch` | 05 against the fixed code: gone with no status reads `cancelled` | `test_a_workload_gone_without_a_status_is_lost_not_cancelled` |

Locally, over the e2b-double at the fix commit: 07 failed 2 tests, 08 1,
09 1, 10 2, 11 1. As above, that is not mutation evidence.
