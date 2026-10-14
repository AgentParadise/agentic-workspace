# Workspace Conformance Coverage

MUST ids are the `W-*` clause ids from the provider operation contract plan
(syntropic137 `docs/plans/20261013_provider-operation-contract.md`, section 3.2),
which number the MUST clauses of the EXP-V1-0006 spec.

Local and Docker are the Rust implementations. Python Docker is
`agentic_isolation.providers.docker.WorkspaceDockerProvider` and Python Local is
`agentic_isolation.providers.local.WorkspaceLocalProvider`; PENDING there means
no Python test claims the row yet.

| EXP-V1-0006 area | MUST ids | Level | Local | Docker | Python Docker | Python Local |
|---|---|---|---:|---:|---:|---:|
| Strict manifest round trip | W-2-a | MUST | PASS | PASS | N/A | PENDING |
| Explicit security profile | W-3.2-d | MUST | PASS | PASS | PENDING | PENDING |
| Production rejects Local | W-3.2-e, W-6.2-a..d | MUST | PASS | N/A | N/A | PARTIAL |
| Provision and working directory | W-3.2-a, W-5-b1 | MUST | PASS | PASS | PENDING | PENDING |
| File hydration and traversal rejection | W-3.2-b, W-3.3-c, W-3.3-d, W-5-b2 | MUST | PARTIAL | PARTIAL | PENDING | PENDING |
| Process execution and timeout | W-3.8-b, W-5-b3 | MUST | PASS | PASS | PARTIAL | PENDING |
| Artifact boundary and collection | W-3.8-a, W-5-b7 | MUST | PASS | PASS | PENDING | PENDING |
| Idempotent teardown | W-4-b, W-5-b8 | MUST | PASS | PASS | PASS | PENDING |
| Repository hydration | W-3.3-a, W-3.3-b | MUST | PENDING | PENDING | PENDING | PENDING |
| Skill digest verification | W-3.5-a, W-3.5-b, W-3.5-c | MUST | PENDING | PENDING | PENDING | PENDING |
| Tool-policy enforcement or refusal | W-3.6-a, W-3.6-b | MUST | PENDING | PENDING | PENDING | PENDING |
| APS-V1-0004 transcript envelope | W-3.7-a, W-3.7-b | MUST | PENDING | PENDING | PENDING | PENDING |
| Filesystem, process, network isolation | W-3.2-f, W-6.3-a | MUST for isolated | N/A | PASS | PENDING | PENDING |
| Credential and resource enforcement | W-3.8-c, W-3.8-d, W-6.3-a | MUST for isolated | N/A | PARTIAL | PARTIAL | PENDING |
| Operation start, idempotency and conflict | W-5-b3 | MUST | PENDING | PENDING | PASS | PASS |
| Operation status from a trusted channel | W-5-b5 | MUST | PENDING | PENDING | PASS | PASS |
| Operation observation pages, gaps and cursors | W-5-b6 | MUST | PENDING | PENDING | PASS | PASS |
| Operation cancellation of the process tree | W-5-b4, W-4-a | MUST | PENDING | PENDING | PARTIAL | PARTIAL |

No implementation claims full conformance while a MUST row is pending.

## Python evidence

Python rows are claimed by these tests in `lib/python/agentic_isolation/tests/`:

| Row | Test ids |
|---|---|
| Idempotent teardown | `test_staged_teardown.py` |
| Process execution and timeout | `test_docker_operations.py::test_regression_execute_runs_through_the_operation_contract`, `::test_run_to_completion_returns_terminal_status_and_bytes` |
| Credential and resource enforcement | `test_docker_operations.py::test_d_binding_reaches_env_but_never_argv_or_pages` |
| Operation start, idempotency and conflict | `test_docker_operations.py::test_a_*`, `::test_b_*`, `::test_c_*`, `::test_unknown_ref_is_unknown_operation`, `::test_local_unlaunchable_spec_is_a_typed_rejection` |
| Operation status from a trusted channel | `test_docker_operations.py::test_l_*`, `::test_m_*` |
| Operation observation pages, gaps and cursors | `test_docker_operations.py::test_e_*`, `::test_f_*`, `::test_g_*`, `::test_h_*`, `::test_i_*`, `::test_j_*`; `test_contract_schemas.py::test_behaviour_document_accepted_by_schema_and_model` |
| Operation cancellation of the process tree | `test_docker_operations.py::test_k_*` |

Python Local rows are claimed by `test_operations_conformance.py`, whose
protocol-level tests run parametrised for both Docker and Local:

| Row | Test ids (Local parameter, `[local]`) |
|---|---|
| Production rejects Local | `::test_local_without_an_explicit_insecure_opt_in_raises`, `::test_agentic_workspace_cannot_reach_local_implicitly` |
| Operation start, idempotency and conflict | `::test_a_*`, `::test_b_*`, `::test_c_*`, `::test_unknown_ref_is_unknown_operation`, `::test_local_unlaunchable_spec_is_a_typed_rejection` |
| Operation status from a trusted channel | `::test_m_*`, `::test_a_signalled_workload_reports_its_signal`, `::test_timeout_settles_as_timed_out_with_the_final_output` |
| Operation observation pages, gaps and cursors | `::test_e_*`, `::test_f_*`, `::test_g_*`, `::test_h_*`, `::test_i_*`, `::test_j_*`, `::test_local_describe_matches_the_published_local_descriptor`, `::test_local_marker_comes_from_a_named_launcher_the_workload_replaces` |
| Operation cancellation of the process tree | `::test_k_*`, `::test_local_cancel_kills_a_descendant_that_left_the_group`, `::test_cancel_never_signals_a_refused_group`, `::test_unsafe_group_*`, `::test_local_cancel_kills_an_orphan_that_detached_before_cancel`, `::test_local_cancel_after_the_leader_exited_fails_closed`, `::test_local_never_signals_a_reused_leader_or_tracked_pid`, `::test_local_never_signals_a_pid_reused_between_term_and_kill` |

Python Local publishes `integrity: continuity_detected`, not `exact`: its
workload runs as the caller's uid in the caller's pid namespace, so the spool
is outside the workload's mounts but not beyond its reach. "Production
rejects Local" is PARTIAL because only the explicit opt-in (construction
raises without `allow_insecure=True`) is implemented; Python has no
production-mode check yet. Cancellation is PARTIAL because the
finalize-on-cancel half of W-4-a belongs to PR 6.

`test_docker_operations.py` drives the real provider code through a stand-in
`docker` executable that runs `exec` as host processes. It proves the
provider's logic, not daemon behaviour (namespaces, `-e NAME` delivery across
the daemon), so PARTIAL rows stay PARTIAL until the Docker-backed run in
`tests/integration/test_docker_operations_live.py` has passed in CI with
Docker present. Cancellation is PARTIAL for the same reason, and because the
finalize-on-cancel half of W-4-a belongs to PR 6.

## Lifecycle hazards (EXP-V1-0006 0.2.1, APSS ee926c6)

One row per `behaviour/hazard-*.json` fixture per provider, from
`lib/python/agentic_isolation/tests/test_behaviour_hazards.py`, run as
`setsid -w uv run --extra dev pytest -q -rsx tests/test_behaviour_hazards.py`.
Each step's `given` is established for real, `descendant_alive` and
`launches` are measured (`/proc` and launch logs), and every
`settles_within_seconds` bound is enforced. FAIL rows are provider findings,
held as `xfail(strict=True)` so they keep running and a fix flips them to an
unexpected pass; this change does not alter provider behaviour. Python Docker
runs through a stand-in `docker` that models a container (its `stop` kills
exactly the run's processes; `removing` is sticky), not a daemon.

| Hazard | Fixture | Python Docker | Python Local | Rust Docker |
|---|---|---|---|---|
| LH-1 | `hazard-double-fork-orphan` | PASS | PASS | NOT RUN |
| LH-2 | `hazard-interrupted-cancel` | PASS | FAIL | NOT RUN |
| LH-3 | `hazard-cancel-before-launch-header` | FAIL | PASS (condition N/A) | NOT RUN |
| LH-4 | `hazard-destroy-during-start` | FAIL | FAIL | NOT RUN |
| LH-5 | `hazard-stuck-removal-timeout-settles` | PASS | PASS (condition N/A) | NOT RUN |
| LH-6 | `hazard-drain-with-gap-reports-loss` | FAIL | FAIL | NOT RUN |
| LH-7 | `hazard-fifo-in-workspace` | NOT RUN | FAIL | NOT RUN |

Findings (each left to a follow-up):

- Docker LH-3: `cancel` waits `_LEADER_SECONDS` (30 s) for the launch header
  before stopping the container, so it cannot settle within the 30 s bound.
- Docker and Local LH-4: a `start` blocked in credential resolution still
  launches after `destroy` began (measured 1 launch, fixture says 0), and a
  later `start` fails `unknown_operation`, not `resource_lost`.
- Docker and Local LH-6: the gap is reported correctly (`lost_bytes: 8`), but
  no page carries the 0.2.1 `loss_detected` marker.
- Local LH-2: after the caller abandons `cancel`, the retried `cancel`
  settles `failed`, neither a confirmed `cancelled` nor the unconfirmed
  `timeout` outcome.
- Local LH-7: `read_file` opens a writer-less FIFO on the event loop and
  blocks; the harness's watchdog thread has to open the write end to free it.

Condition N/A: Local learns its leader at spawn, so LH-3's
`launch_header_not_seen` cannot arise and the step runs as an immediate
cancel; Local has no backend, so LH-5's `backend_stuck_removing` cannot arise
and only `timeout_elapsed` is imposed.

NOT RUN, and why:

- Python Docker LH-7: `file_exists`, `read_file` and `destroy` go through the
  Docker SDK, which the stand-in CLI does not serve. It needs a real-daemon
  fixture.
- Rust Docker: the Rust operations trait (`SupportsOperations`) exists only
  in open PR #60. The provider-neutral half of the Rust harness,
  `agentic_workspace_conformance::hazard` (fixture loader, `judge`, and
  `/proc`-measured `live_processes`), is on main. Wiring it into #60's
  `behaviour::drive` is the follow-up once #60 merges.

No vacuous pass: `test_lh1_fails_a_cancel_confirmed_while_a_descendant_lives`
replaces each Python provider's cancel confirmation with one that kills only
the leader's group and reports confirmed. It asserts that the mutation ran and
that LH-1 then fails on the cancel step, where `cancelled` comes back while a
descendant is measured alive. The Rust `judge` has the same check:
`hazard::tests::lh1_rejects_a_confirmed_cancel_while_a_descendant_lives`.
