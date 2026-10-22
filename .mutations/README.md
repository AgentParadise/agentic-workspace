# Mutation patches for #95 (E2B `SupportsOperations`)

Each patch re-introduces one bug in
`lib/python/agentic_isolation/agentic_isolation/providers/e2b_operations.py`.
Node ids are relative to `lib/python/agentic_isolation`.

| Patch | Bug | Killing test |
|---|---|---|
| `01-credential-binding-not-scrubbed.patch` | resolved binding values are not registered with the spool's redactor | `tests/test_e2b_operations.py::test_a_credential_binding_reaches_the_workload_env_scrubbed_and_never_argv` |
| `02-launch-marker-dropped.patch` | the marker line is not written to stdin | `tests/test_operations_conformance.py::test_e_marker_line_is_first_and_a_forged_one_is_not[e2b]`, `tests/test_e2b_operations.py::test_the_launch_marker_travels_on_stdin_not_the_command_line` |
| `03-cancel-is-a-noop.patch` | cancel never signals the process group | `tests/test_operations_conformance.py::test_k_cancel_kills_leader_grandchild_and_term_trapper[e2b]` (fails at the test's 30 s bound: the operation never ends) |
| `04-status-exit-code-hardcoded.patch` | status reports exit code 0 whatever the SDK said | `tests/test_operations_conformance.py::test_m_fake_exit_file_does_not_change_true_status[e2b]` |

Locally (not trusted evidence) each patch made its named tests fail and the
unpatched baseline passed all five.
