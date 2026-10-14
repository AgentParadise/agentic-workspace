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
`docker-conformance`). The APSS 0.2.0 valid, invalid and behaviour documents
are checked against the Rust models by
`crates/workspace-conformance/tests/apss_fixtures.rs`.

| Row | Test ids |
|---|---|
| Operation start, idempotency and conflict | `tests/operations.rs::start_same_key_one_launch_under_50_concurrent_tasks`, `::same_key_different_spec_is_conflict_and_launches_nothing`, `::unknown_ref_is_a_typed_unknown_operation`, `::credential_bindings_are_refused_until_a_resolver_exists` |
| Operation status from a trusted channel | `tests/operations.rs::timeout_settles_as_timed_out_with_the_final_output`, `::cancel_kills_leader_grandchild_and_term_trapper_and_is_idempotent`; `src/operations.rs::tests::signalled_exits_are_named` |
| Operation observation pages, gaps and cursors | `tests/operations.rs::cursor_after_retention_reports_the_exact_gap_then_drains`, `::late_read_drains_bounded_pages_then_completes`; core `operations::spool::tests::*` |
| Operation cancellation of the process tree | `tests/operations.rs::cancel_kills_leader_grandchild_and_term_trapper_and_is_idempotent`, `::finalize_ends_running_operations_and_forgets_them`; `src/operations.rs::tests::*` (process table, escapees, reused leaders, init and own-group refusal); `tests/conformance.rs::operations_cancel_the_whole_tree_in_a_real_container` |

Rust Docker cancellation is PARTIAL until
`operations_cancel_the_whole_tree_in_a_real_container` has passed in CI with
Docker present, and because the finalize-on-cancel half of W-4-a belongs to
PR 6. Credential bindings are refused with `unsupported_capability` (no
credential resolver is wired into the Rust provider yet), so the Rust Docker
descriptor does not publish `credential_env`. Rust Local has no
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
