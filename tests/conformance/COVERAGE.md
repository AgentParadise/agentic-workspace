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
| Operation start, idempotency and conflict | W-5-b3 | MUST | PENDING | PASS | PASS | PASS |
| Operation status from a trusted channel | W-5-b5 | MUST | PENDING | PASS | PASS | PASS |
| Operation observation pages, gaps and cursors | W-5-b6 | MUST | PENDING | PASS | PASS | PASS |
| Operation cancellation of the process tree | W-5-b4, W-4-a | MUST | PENDING | PARTIAL | PARTIAL | PARTIAL |

No implementation claims full conformance while a MUST row is pending.

## Rust evidence

Rust Docker operation rows are claimed by
`agentic_workspace_docker::operations::DockerOperations`, the
`SupportsOperations` implementation, through these tests in
`implementations/docker/`. `tests/operations.rs` drives it through a stand-in
`docker` whose `exec` runs as host processes (run by `rust-test`);
`tests/conformance.rs` runs it against a real container (run by
`docker-conformance`). The APSS 0.2.0 valid and invalid documents, and the
documents behaviour fixtures expect, are parsed against the Rust models by
`crates/workspace-conformance/tests/apss_fixtures.rs`; that is schema
evidence, not behaviour evidence.

`agentic_workspace_conformance::behaviour::drive` executes a behaviour
fixture from its file against any `SupportsOperations` behind a
`BehaviourHarness`, which supplies the fixture's `argv[0]` workload and a
launch count. Each step runs with the fixture's own spec (credential binding
and launch marker included, resolved by an injected `CredentialResolver`) and
checks the issued ref, error `kind`, status `state` and `exit_code`, each
page's bytes at the fixture's offset, exact `gap`, cursor progression, page
state and `complete`, a result's derived `success` (and rejection of the
opposite claim), and the launch count after each step. Docker runs all seven
fixtures through `tests/operations.rs::behaviour_fixture_*` (`rust-test`,
Linux leg). Three readings are made explicit, with their provenance:

- Fixture offsets count the workload's output. The launch marker line the
  contract requires first on the stream (plan section 4.2) is read and
  checked before the fixture's `g1:0`.
- A page's length is the provider's: base64 pads 10 bytes to the width of
  12, so a provider filling its bound may return more than a fixture page.
  The driver checks the fixture's bytes, then that any more are the
  workload's next bytes, and refuses to reuse a fixture cursor the provider
  skipped past.
- A result is assembled by the orchestrator from the provider's terminal
  status; `WorkspaceResult::success_of` is the one derivation of `success`.

The Rust spool follows `late-read-drains-then-complete`: only the empty page
at the end of a terminal operation is `complete`. The Python spool marks the
page carrying the last bytes complete; see DISC-005.

| Row | Test ids |
|---|---|
| Operation start, idempotency and conflict | `tests/operations.rs::start_same_key_one_launch_under_50_concurrent_tasks`, `::same_key_different_spec_is_conflict_and_launches_nothing`, `::unknown_ref_is_a_typed_unknown_operation`, `::credential_bindings_reach_the_workload_env_but_never_the_client_argv`, `::credential_bindings_without_a_resolver_are_rejected_policy`, `::behaviour_fixture_start_same_key_one_launch`, `::behaviour_fixture_same_key_different_spec_conflict`, `::behaviour_fixture_unknown_ref_typed_error` |
| Operation status from a trusted channel | `tests/operations.rs::timeout_settles_as_timed_out_with_the_final_output`, `::cancel_kills_leader_grandchild_and_term_trapper_and_is_idempotent`, `::a_workload_whose_container_is_removed_is_lost`, `::an_unreachable_daemon_is_provider_unavailable_and_a_real_failure_stays_failed`, `::behaviour_fixture_cancel_twice_terminal_once`; `src/operations.rs::tests::signalled_exits_are_named`, `::container_inspect_answers_are_classified` |
| Operation observation pages, gaps and cursors | `tests/operations.rs::behaviour_fixture_cursor_after_retention_gap`, `::behaviour_fixture_late_read_drains_then_complete`, `::behaviour_fixture_required_capture_unknown_success_false`, `::cursor_after_retention_reports_the_exact_gap_then_drains`, `::late_read_drains_bounded_pages_then_completes`, `::a_complete_page_stays_the_end_when_a_descendant_writes_late`; `src/operations.rs::tests::a_stream_error_stops_collection_and_is_reported_not_taken_for_eof`, `::a_clean_eof_reports_no_output_failure`, `::a_stdout_error_before_the_leader_line_is_reported_and_stops_reading`; core `operations::spool::tests::*` |
| Operation cancellation of the process tree | `tests/operations.rs::cancel_kills_leader_grandchild_and_term_trapper_and_is_idempotent`, `::finalize_ends_running_operations_and_forgets_them`, `::failed_cleanup_stays_resolvable_and_a_retry_cleans_up`; `src/operations.rs::tests::*` (process table, escapees, reused leaders, init and own-group refusal); `tests/conformance.rs::operations_cancel_the_whole_tree_in_a_real_container` |

Rust Docker cancellation is PARTIAL until
`operations_cancel_the_whole_tree_in_a_real_container` has passed in CI with
Docker present, and because the finalize-on-cancel half of W-4-a belongs to
PR 6. Credential bindings with `env` delivery are resolved by an injected
`CredentialResolver` (`DockerOperations::with_credentials`); without one they
are `rejected_policy`, and the descriptor publishes `credential_env` only when
one is injected. Rust Local has no
`SupportsOperations` yet; its rows stay PENDING.

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
| LH-1 | `hazard-double-fork-orphan` | PASS | PASS | PASS |
| LH-2 | `hazard-interrupted-cancel` | PASS | FAIL | FAIL (unattributed) |
| LH-3 | `hazard-cancel-before-launch-header` | FAIL | PASS (condition N/A) | NOT RUN |
| LH-4 | `hazard-destroy-during-start` | FAIL | FAIL | NOT RUN |
| LH-5 | `hazard-stuck-removal-timeout-settles` | PASS | PASS (condition N/A) | NOT RUN |
| LH-6 | `hazard-drain-with-gap-reports-loss` | FAIL | FAIL | FAIL |
| LH-7 | `hazard-fifo-in-workspace` | daemon only (see below) | FAIL | NOT RUN |

Python LH-4 drives each provider's public `destroy` (for Docker, only the
container boundary `_measure_usage`/`_stop`/`_remove` is a logged stand-in),
and the harness asserts that destroy reached operation teardown before the
credential resolver was released, so the race measured is the race claimed.

Rust Docker rows come from `agentic_workspace_conformance::hazard::drive_hazard`
over `DockerOperations` behind the stand-in `docker` of
`implementations/docker/tests/operations.rs` (module `hazards`, Linux
`rust-test` job), with the test process as the container's init
(subreaper). Each case asserts its measured verdict, so a change in either
direction fails until this table changes with it.

Findings (each left to a follow-up):

- Docker LH-3: `cancel` waits `_LEADER_SECONDS` (30 s) for the launch header
  before stopping the container, so it cannot settle within the 30 s bound.
- Docker and Local LH-4: a `start` blocked in credential resolution, raced by
  the public `destroy`, still returns a ref (step 0 matched neither
  `resource_lost` nor `cancelled`).
- Docker and Local LH-6, and Rust Docker LH-6: the gap is reported correctly
  (`lost_bytes: 8`), but no page carries the 0.2.1 `loss_detected` marker.
- Local LH-2: after the caller abandons `cancel`, the retried `cancel`
  settles `failed`, neither a confirmed `cancelled` nor the unconfirmed
  `timeout` outcome.
- Rust Docker LH-2: the retried `cancel` returns `provider_unavailable`
  after 7 s with the TERM-ignoring workload still measured alive. Not yet
  attributed: it may be the stand-in's `stop` rather than the provider.
- Local LH-7: `read_file` opens a writer-less FIFO on the event loop and
  blocks; the harness's watchdog thread has to open the write end to free it.

Condition N/A: Local learns its leader at spawn, so LH-3's
`launch_header_not_seen` cannot arise and the step runs as an immediate
cancel; Local has no backend, so LH-5's `backend_stuck_removing` cannot arise
and only `timeout_elapsed` is imposed.

NOT RUN, and why (each ends its run as `NotRun`, never a pass):

- Python Docker LH-7 through the stand-in: the probes read the host workspace
  mount, which a stand-in container lacks.
  `test_docker_daemon_meets_lh7_fifo` runs it against a real container
  instead; it skips without Docker and its CI result is the row's evidence.
- Rust Docker LH-3 and LH-5: the stand-in has no late-`exec` or
  stuck-`removing` hook (`launch_header_not_seen`, `backend_stuck_removing`).
- Rust Docker LH-4: the harness does not yet race `finalize` against a gated
  resolver (`destroyed_during_credential_resolution`).
- Rust Docker LH-7: `SupportsOperations` has no `file_exists`/`read_file`;
  the run reaches step 2 and stops there.

No vacuous pass: `test_lh1_fails_a_cancel_confirmed_while_a_descendant_lives`
replaces each Python provider's cancel confirmation with one that kills only
the leader's group and reports confirmed. It asserts that the mutation ran and
that LH-1 then fails on the cancel step, where `cancelled` comes back while a
descendant is measured alive. For Rust Docker,
`hazards::lh1_fails_a_cancel_confirmed_while_a_descendant_lives` wraps the
provider in a decorator whose `cancel` terminates nothing and reports
`cancelled`, counts that it ran, and asserts LH-1 fails on step 1 `cancel`
with the descendant measured alive.

CI: the workflow edits these jobs need (APSS pin grep to 0.2.1, every Python
invocation under `setsid -w` with an outer `timeout`, the hazard file named
in its own step) cannot be pushed from the agent workspace; they ship as a
patch in the PR thread for a maintainer to apply.
