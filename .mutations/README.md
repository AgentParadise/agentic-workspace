# Mutation evidence for syntropic137/syntropic137#1911

Each patch re-introduces one bug in non-test source. Every listed test must
fail with the patch applied. The `docker_conformance` tests need a real daemon, so dispatch `mutation-evidence.yml` (see docs/mutation-evidence.md).

| Patch | Bug | Killing test(s) |
|---|---|---|
| 01-read-files-decodes-to-text.patch | NEGATIVE CONTROL: Docker `read_files` decodes content to text | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_read_files_round_trips_binary[docker-/workspace]`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_read_files_round_trips_binary[docker-/spool]` |
| 02-attach-skips-label-check.patch | `attach` never refuses a label mismatch | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_attach_with_mismatched_labels_raises[docker]` |
| 03-list-owned-matches-key-not-value.patch | `list_owned` filters on the label key only | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_list_owned_returns_exactly_the_host_workspaces[docker]` |
| 04-stream-drops-stderr.patch | the stream loses stderr instead of merging it | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_merges_stdout_and_stderr_in_write_order[docker]` |
| 05-timeout-reported-as-completed.patch | a timeout is reported as `completed` | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_timeout_stops_the_sandbox_process[docker]` |
| 06-disconnect-reported-as-completed.patch | a lost transport is reported as `completed` | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_lost_transport_is_disconnected_and_cancel_still_stops_it[docker]` |
| 07-stop-kills-only-the-local-pipe.patch | timeout/cancel never kill the sandbox process | `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_timeout_stops_the_sandbox_process[docker]`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_cancel_stops_the_sandbox_process[docker]` |
| 08-lines-inherit-readline-limit.patch | lines go back to StreamReader iteration and its 64 KiB limit | `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_stream_lines_have_no_length_limit`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_delivers_lines_of_any_length[docker]` |
| 09-client-error-exit-reported-as-completion.patch | a docker client error exit is reported as the sandbox's exit | `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_a_docker_client_error_is_disconnected_not_completion`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_unreachable_transport_is_never_completion[docker]` |
| 10-cancel-noop-after-disconnect.patch | cancel does nothing once the docker client has exited | `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_cancel_after_a_disconnect_still_stops_the_sandbox`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_lost_transport_is_disconnected_and_cancel_still_stops_it[docker]` |
| 11-read-files-drops-hard-links.patch | `read_files` drops tar hard-link entries | `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_read_files_returns_every_name_of_a_hard_link`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_read_files_returns_every_name_of_a_hard_link[docker-/workspace]`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_read_files_returns_every_name_of_a_hard_link[docker-/spool]` |
| 12-repeated-outcome-loses-completion.patch | a second `outcome()` forgets a known completion and reports `disconnected` | `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_a_recorded_sandbox_exit_survives_repeated_observation`, `lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py::test_stream_merges_stdout_and_stderr_in_write_order[docker]` |
| 13-failed-stop-published-as-completion.patch | a stop is published before the sandbox confirms it, so a failed stop reads as completion and is never retried | `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_a_failed_stop_publishes_nothing_and_can_be_retried[nonzero]`, `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_a_failed_stop_publishes_nothing_and_can_be_retried[oserror]` |
| 14-concurrent-observers-race-the-status-read.patch | concurrent `outcome()` calls race the destructive status read and one reports `disconnected` | `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_concurrent_observers_agree_on_a_recorded_exit` |
| 15-concurrent-stops-each-reach-the-sandbox.patch | concurrent stops do not share one confirmed stop | `lib/python/agentic_isolation/tests/test_remote_capabilities.py::test_concurrent_stops_share_one_confirmed_stop` |

Dispatch:

```
gh workflow run mutation-evidence.yml --ref main -f pr=85 -f tests="lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py lib/python/agentic_isolation/tests/test_remote_capabilities.py"
```

Delete `.mutations/` before merge (CI job `mutations-not-merged`).
