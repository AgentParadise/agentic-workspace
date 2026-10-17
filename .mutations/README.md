# Mutation evidence for syntropic137/syntropic137#1911

Each patch re-introduces one bug in non-test source. Every listed test must
fail with the patch applied. All tests are `docker_conformance` and need a
real daemon, so dispatch `mutation-evidence.yml` (see docs/mutation-evidence.md).

| Patch | Bug | Killing test(s) |
|---|---|---|
| 01-read-files-decodes-to-text.patch | NEGATIVE CONTROL: Docker `read_files` decodes content to text | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_read_files_round_trips_binary[docker-/workspace]`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_read_files_round_trips_binary[docker-/spool]` |
| 02-attach-skips-label-check.patch | `attach` never refuses a label mismatch | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_attach_with_mismatched_labels_raises[docker]` |
| 03-list-owned-matches-key-not-value.patch | `list_owned` filters on the label key only | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_list_owned_returns_exactly_the_host_workspaces[docker]` |
| 04-stream-drops-stderr.patch | the stream loses stderr instead of merging it | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_merges_stdout_and_stderr_in_write_order[docker]` |
| 05-timeout-reported-as-completed.patch | a timeout is reported as `completed` | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_timeout_stops_the_sandbox_process[docker]` |
| 06-disconnect-reported-as-completed.patch | a lost transport is reported as `completed` | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_lost_transport_is_disconnected_and_process_lives[docker]` |
| 07-stop-kills-only-the-local-pipe.patch | timeout/cancel never kill the sandbox process | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_timeout_stops_the_sandbox_process[docker]`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_cancel_stops_the_sandbox_process[docker]` |

Dispatch:

```
gh workflow run mutation-evidence.yml --ref main -f pr=85 -f tests="lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py"
```

Delete `.mutations/` before merge (CI job `mutations-not-merged`).
