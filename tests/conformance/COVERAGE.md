# Workspace Conformance Coverage

MUST ids are the `W-*` clause ids from the provider operation contract plan
(syntropic137 `docs/plans/20261013_provider-operation-contract.md`, section 3.2),
which number the MUST clauses of the EXP-V1-0006 spec.

Local and Docker are the Rust implementations. Python is
`agentic_isolation.providers.docker.WorkspaceDockerProvider`; PENDING there means
no Python test claims the row yet.

| EXP-V1-0006 area | MUST ids | Level | Local | Docker | Python |
|---|---|---|---:|---:|---:|
| Strict manifest round trip | W-2-a | MUST | PASS | PASS | N/A |
| Explicit security profile | W-3.2-d | MUST | PASS | PASS | PENDING |
| Production rejects Local | W-3.2-e, W-6.2-a..d | MUST | PASS | N/A | N/A |
| Provision and working directory | W-3.2-a, W-5-b1 | MUST | PASS | PASS | PENDING |
| File hydration and traversal rejection | W-3.2-b, W-3.3-c, W-3.3-d, W-5-b2 | MUST | PARTIAL | PARTIAL | PENDING |
| Process execution and timeout | W-3.8-b, W-5-b3 | MUST | PASS | PASS | PARTIAL |
| Artifact boundary and collection | W-3.8-a, W-5-b7 | MUST | PASS | PASS | PENDING |
| Idempotent teardown | W-4-b, W-5-b8 | MUST | PASS | PASS | PASS |
| Repository hydration | W-3.3-a, W-3.3-b | MUST | PENDING | PENDING | PENDING |
| Skill digest verification | W-3.5-a, W-3.5-b, W-3.5-c | MUST | PENDING | PENDING | PENDING |
| Tool-policy enforcement or refusal | W-3.6-a, W-3.6-b | MUST | PENDING | PENDING | PENDING |
| APS-V1-0004 transcript envelope | W-3.7-a, W-3.7-b | MUST | PENDING | PENDING | PENDING |
| Filesystem, process, network isolation | W-3.2-f, W-6.3-a | MUST for isolated | N/A | PASS | PENDING |
| Credential and resource enforcement | W-3.8-c, W-3.8-d, W-6.3-a | MUST for isolated | N/A | PARTIAL | PARTIAL |
| Operation start, idempotency and conflict | W-5-b3 | MUST | PENDING | PENDING | PASS |
| Operation status from a trusted channel | W-5-b5 | MUST | PENDING | PENDING | PASS |
| Operation observation pages, gaps and cursors | W-5-b6 | MUST | PENDING | PENDING | PASS |
| Operation cancellation of the process tree | W-5-b4, W-4-a | MUST | PENDING | PENDING | PARTIAL |

No implementation claims full conformance while a MUST row is pending.

## Python evidence

Python rows are claimed by these tests in `lib/python/agentic_isolation/tests/`:

| Row | Test ids |
|---|---|
| Idempotent teardown | `test_staged_teardown.py` |
| Process execution and timeout | `test_docker_operations.py::test_regression_execute_runs_through_the_operation_contract`, `::test_run_to_completion_returns_terminal_status_and_bytes` |
| Credential and resource enforcement | `test_docker_operations.py::test_d_binding_reaches_env_but_never_argv_or_pages` |
| Operation start, idempotency and conflict | `test_docker_operations.py::test_a_*`, `::test_b_*`, `::test_c_*`, `::test_unknown_ref_is_unknown_operation` |
| Operation status from a trusted channel | `test_docker_operations.py::test_l_*`, `::test_m_*` |
| Operation observation pages, gaps and cursors | `test_docker_operations.py::test_e_*`, `::test_f_*`, `::test_g_*`, `::test_h_*`, `::test_i_*`, `::test_j_*`; `test_contract_schemas.py::test_behaviour_document_accepted_by_schema_and_model` |
| Operation cancellation of the process tree | `test_docker_operations.py::test_k_*` |

`test_docker_operations.py` drives the real provider code through a stand-in
`docker` executable that runs `exec` as host processes. It proves the
provider's logic, not daemon behaviour (namespaces, `-e NAME` delivery across
the daemon), so PARTIAL rows stay PARTIAL until the Docker-backed run in
`tests/integration/test_docker_operations_live.py` has passed in CI with
Docker present. Cancellation is PARTIAL for the same reason, and because the
finalize-on-cancel half of W-4-a belongs to PR 6.
