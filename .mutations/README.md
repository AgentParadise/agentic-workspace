# Mutation evidence for the always-named `--runtime` fix

Delete this directory in a final commit before merge (`mutations-not-merged`).

| Patch | Bug it restores | Killing tests |
|---|---|---|
| `01-omit-runtime-for-runc.patch` | `to_docker_run_args()` omits `--runtime` when the resolved runtime is runc | see node ids below |

Dispatch:

```
gh workflow run mutation-evidence.yml --ref main -f pr=79 \
  -f tests="lib/python/agentic_isolation/tests/test_security_config.py::TestSecurityConfig::test_to_docker_run_args_includes_all_flags lib/python/agentic_isolation/tests/test_security_config.py::TestResolveRuntime::test_explicit_runc_never_probes_docker lib/python/agentic_isolation/tests/test_security_config.py::TestResolveRuntime::test_every_explicit_runtime_is_named_in_docker_run_args lib/python/agentic_isolation/tests/test_security_config.py::TestResolveRuntime::test_auto_on_runc_only_host_emits_runtime_runc lib/python/agentic_isolation/tests/test_security_config.py::TestResolveRuntime::test_auto_ignores_kata lib/python/agentic_isolation/tests/test_security_config.py::TestResolveRuntime::test_auto_with_runsc_alias_running_runc_is_runc lib/python/agentic_isolation/tests/test_docker_runtime.py::test_auto_on_runc_only_host_runs_runc lib/python/agentic_isolation/tests/test_docker_runtime.py::test_explicit_runc_runs_under_runc_and_is_recorded lib/python/agentic_isolation/tests/integration/test_docker_contract_conformance.py::test_container_runs_under_the_resolved_runtime"
```

The integration test checks `docker inspect .HostConfig.Runtime` and the
`docker run` argv. On a runner whose daemon default-runtime is runc, inspect
reports `runc` with or without the flag, so under this patch the argv
assertion is what fails; the inspect assertion is the end-to-end check that
the named runtime is the one Docker ran.
