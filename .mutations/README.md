# Mutation patches for PR #74 (verify round 1, finding 4)

Named tests: `lib/python/agentic_isolation/tests/test_output_redaction.py` and
`lib/python/agentic_isolation/tests/integration/test_docker_operations_live.py::test_d_resolved_and_registered_values_never_reach_a_page_or_the_spool`.

| Patch | Bug | Expected killer |
|---|---|---|
| 01 | Docker registers only the env map's final values | `test_docker_registers_a_value_overwritten_by_a_repeated_target` |
| 02 | Marker-overlap rule disabled | `test_a_registration_replacement_could_leave_or_rebuild_is_refused`, Docker/Local refusal tests |
| 03 | Local checks registration only after spawn | `test_local_refuses_a_resolved_marker_overlapping_value_before_launch` |
| 04 | Per-channel copy returned unredacted | `test_docker_pages_spool_and_channels_never_carry_the_canary` |
| 05 | Merged stream not rescanned | `test_channels_merged_into_storage_never_form_a_value` |
| 06 | `finish` drops held tails at EOF | `test_held_bytes_are_neither_counted_nor_complete_until_finished` |
| 07 | Docker drops caller-registered values | `test_docker_pages_spool_and_channels_never_carry_the_canary`, live test |
| 08 | Local drops resolved values | `test_local_final_output_never_carries_the_canary` |
| 09 | Docker replay ignores the redaction policy | `test_docker_replay_is_bound_to_its_redaction_values` |
| 10 | Refusal message spells the marker, echoing a value inside it (verify round 2) | `test_a_registration_replacement_could_leave_or_rebuild_is_refused[value-equals-marker]`, `[value-inside-marker]`, `[value-one-marker-byte]` |

Rust mutations cannot run through this workflow (pytest node ids only); they
were run locally and are recorded in the fix report. Delete `.mutations/`
before merge.
