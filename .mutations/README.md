# Mutations for #103 (E2B operations reattach, public pause/resume)

Each patch re-introduces one bug in
`lib/python/agentic_isolation/agentic_isolation/providers/e2b_operations.py`
or, for 04, `providers/e2b.py`. The killing tests run over the e2b-double
(`tests/e2b_fake.py`) in its executing mode, so no E2B account is needed.

Named tests to run (repo-relative; 11 node ids, none parametrised):

```
lib/python/agentic_isolation/tests/test_e2b_reattach.py
```

| Patch | Bug | Killing tests |
|---|---|---|
| `01-reattach-memory-registry-only.patch` | `reattach` consults only the in-memory registry, so a fresh instance refuses every ref another one started | `test_fresh_instance_reads_running_then_the_exit_status`, `test_fresh_instance_cancel_kills_the_workload`, `test_a_probe_that_does_not_answer_is_unavailable_not_stopped`, `test_a_workload_gone_without_a_status_is_lost_not_cancelled`, `test_a_cancel_that_is_not_confirmed_raises_and_is_never_cancelled`, `test_a_reattached_operation_does_not_pretend_to_have_its_output` |
| `02-fresh-cancel-stopped-without-kill.patch` | a fresh-instance cancel reports `cancelled` without running the termination | `test_fresh_instance_cancel_kills_the_workload` (the workload's pid is still alive; a third instance reads `running`) |
| `03-reattach-ownership-unchecked.patch` | the probe accepts any token, so a forged token, or another operation's, reattaches and can cancel | `test_a_wrong_token_cannot_reattach_or_cancel` |
| `04-pause-noop.patch` | `E2BWorkspaceProvider.pause` returns without pausing the sandbox | `test_pause_and_resume_reach_the_sandbox` |
| `05-gone-without-status-reads-cancelled.patch` | a workload gone with no exit status and no confirmed termination reads `cancelled` instead of `lost` | `test_a_workload_gone_without_a_status_is_lost_not_cancelled` |
| `06-unconfirmed-cancel-reads-cancelled.patch` | a reattached cancel is marked confirmed before the termination, so a refused one still reads `cancelled` | `test_a_cancel_that_is_not_confirmed_raises_and_is_never_cancelled` |

Results of a local run against these tests are in the PR description. That
run is not mutation evidence; the trusted run is `mutation-evidence.yml` on
a GitHub runner.
