# Mutation patches for PR #87 (syntropic137/syntropic137#1915, verify round 1)

Every patch is killed by `lib/python/agentic_isolation/tests/test_e2b_adapter.py`.
Remove this directory before merge (`mutations-not-merged`).

| Patch | Finding | Killing test(s) |
|---|---|---|
| 01-cancel-no-signal | R2 | test_cancel_of_a_running_command_reports_the_kill_signal, test_cancel_after_a_lost_stream_still_kills_and_confirms |
| 02-cancel-ignores-refused-kill | R2 | test_refused_cancel_raises_and_claims_nothing |
| 03-timeout-awaits-unbounded | R4 | test_timeout_with_a_kill_but_no_end_ends_disconnected |
| 04-create-no-cleanup | R5 | test_failed_initialisation_kills_the_sandbox[*], test_cancelled_initialisation_kills_the_sandbox, test_failed_cleanup_after_failed_initialisation_names_the_sandbox |
| 05-create-cleanup-unconfirmed | R5 | test_failed_cleanup_after_failed_initialisation_names_the_sandbox |
| 06-wait-narrow-except | R3 | test_sdk_stream_failure_is_disconnected[reset,truncated] |
| 07-disconnect-is-cancel | R3 | test_sdk_disconnect_is_disconnected_not_a_cancellation |
| 08-start-raises | R3 | test_start_on_an_unreachable_sandbox_is_a_disconnected_command |
| 09-pause-one-attempt | R8 | test_pause_retries_a_busy_refusal_a_bounded_number_of_times[4,5] |
| 10-build-drops-cpu | R8 | test_build_template_forwards_the_spec_and_returns_the_receipt |

The R7 reporter fix lives in `tests/integration/conftest.py`, which the runner
REFUSES to mutate (test-shaped code); its mutation was run locally only.
