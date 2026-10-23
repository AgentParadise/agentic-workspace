# Mutations for #100 (bounded, link-safe `read_files`)

Patches 01-08 each re-introduce one bug in
`lib/python/agentic_isolation/agentic_isolation/providers/file_transfer.py`,
in the host's plan and check and, where the reader decides, in the
in-sandbox reader script. 09 and 10 re-introduce a bug in the provider
transports, `providers/docker_capabilities.py` and `providers/e2b.py`. 01-04 were regenerated when the tar archive was
replaced by the descriptor-checked reader; each keeps the defect it names.
Named tests to run (repo-relative):

```
lib/python/agentic_isolation/tests/test_file_transfer.py
lib/python/agentic_isolation/tests/test_remote_capabilities.py
lib/python/agentic_isolation/tests/test_e2b_provider.py
lib/python/agentic_isolation/tests/test_e2b_adapter.py
lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py
```

| Patch | Bug | Killing tests |
|---|---|---|
| `01-per-file-limit-unenforced.patch` | `max_file_bytes` is checked neither in the plan, nor by the reader, nor in the host check | `test_content_past_the_limits_is_refused_whatever_the_sandbox_sends`, `test_the_reader_holds_no_more_of_a_file_grown_after_the_listing_than_the_limit`, `test_read_files_refuses_a_file_over_the_per_file_limit`, `test_read_files_without_limits_is_bounded_by_default`, `test_read_files_holds_a_file_grown_after_the_listing_to_the_limits` |
| `02-hard-link-followed.patch` | `st_nlink > 1` files are planned, and the reader does not check the link count of what it opened | `test_read_files_refuses_every_name_of_a_hard_link`, `test_read_files_refuses_hard_links_and_returns_exact_bytes`, `test_read_files_refuses_a_hard_link_made_after_the_listing`, `test_the_reader_refuses_a_hard_link_made_after_the_listing` |
| `03-escaping-symlink-followed.patch` | a symlink that escapes the root is planned, and the reader follows it and accepts a descriptor resolved outside the root | `test_read_files_refuses_symlinks_that_escape_the_root`, `test_read_files_refuses_an_ancestor_swapped_for_a_symlink_after_the_listing`, `test_the_reader_refuses_an_ancestor_swapped_for_a_symlink_out_of_the_root` |
| `04-refusal-swallowed.patch` | refusals are dropped from the result | every refusal case above |
| `05-ancestor-swap-followed.patch` | B1: the reader does not check where its descriptor resolved, so an ancestor swapped for a symlink after the listing is read | `test_read_files_refuses_an_ancestor_swapped_for_a_symlink_after_the_listing`, `test_the_reader_refuses_an_ancestor_swapped_for_a_symlink_out_of_the_root` |
| `06-post-listing-link-followed.patch` | B2: the reader does not check the link count of its descriptor, so a hard link made after the listing is read | `test_read_files_refuses_a_hard_link_made_after_the_listing`, `test_the_reader_refuses_a_hard_link_made_after_the_listing` |
| `07-walk-unbounded.patch` | B3: the walk spools the whole find before `head` applies `max_entries` | `test_the_walk_stops_once_the_entry_limit_is_out` |
| `08-limits-applied-before-sorting.patch` | review finding 1: the plan applies the total in the walk's order and sorts only what it kept, so which files fit depends on directory order | `test_the_total_is_allocated_in_path_order_whatever_order_the_walk_lists` (4 of its 6 permutations) |
| `09-stderr-read-after-stdout.patch` | review finding 2: the Docker transport reads stderr only after stdout ends, so a sandbox that fills the stderr pipe stalls the call | `test_a_sandbox_flooding_stderr_cannot_stall_the_read` |
| `10-timeout-removed.patch` | review finding 2: no deadline on either transport (Docker host wait and in-sandbox `timeout`, E2B command timeout and the host deadline, which also encloses the command start and its stdin), so a stalled sandbox hangs the call | `test_read_files_times_out_on_a_sandbox_that_never_answers`, `test_read_files_times_out_on_a_file_swapped_for_a_fifo_after_its_check` (in both `test_remote_capabilities.py` and `test_e2b_provider.py`), `test_read_files_times_out_while_the_paths_are_still_being_sent` |
| `11-cancelled-start-leaks-command.patch` | reverify of finding 2: `SdkE2BClient.start` catches only `Exception` around `send_stdin`, so a start cancelled by the read deadline leaves the command running | `test_a_start_cancelled_while_sending_stdin_kills_the_command` |

Locally (no Docker), against the E2B double and the unit tests only, at the
commit that added 05-07: 01 failed 5 tests, 02 failed 5, 03 failed 3, 04
failed 20, 05 failed 2, 06 failed 2, 07 failed 1. 07's killer runs a `find`
wrapper from its tmp dir, so it needs an executable `TMPDIR` (a GitHub
runner's `/tmp` is). The Docker parametrisations need the runner. This is
not mutation evidence; the trusted run is the orchestrator's.

Locally, at the commit that regenerated 01 and 03 for the per-call total
and the `vanished` kind (only their context changed), with the same named
tests: 01 failed 5, 02 failed 5, 03 failed 3, 04 failed 21 (the added
`vanished` case is one more refusal it drops), 05 failed 2, 06 failed 2, 07
failed 1, 08 failed 4, 09 failed 1 and 10 failed 3.
Locally, at the commit that put the E2B command start under the read
deadline and added 11 (10 regenerated for it), with the five named tests:
01 failed 6, 02 failed 5, 03 failed 4, 04 failed 21, 05 failed 2, 06 failed
3, 07 failed 1, 08 failed 4, 09 failed 1, 10 failed 4 and 11 failed 1 (in
one of three runs, 11 also failed an unrelated symlink conformance case that
did not fail again in eight reruns of that file).
09 and 10 make their killers hang, so those fail on the test's own 20 s
`asyncio.wait_for` bound, not on an assertion.
