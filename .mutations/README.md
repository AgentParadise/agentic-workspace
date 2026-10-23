# Mutations for #100 (bounded, link-safe `read_files`)

Each patch re-introduces one bug in
`lib/python/agentic_isolation/agentic_isolation/providers/file_transfer.py`,
in the host's plan and check and, where the reader decides, in the
in-sandbox reader script. 01-04 were regenerated when the tar archive was
replaced by the descriptor-checked reader; each keeps the defect it names.
Named tests to run (repo-relative):

```
lib/python/agentic_isolation/tests/test_file_transfer.py
lib/python/agentic_isolation/tests/test_remote_capabilities.py
lib/python/agentic_isolation/tests/test_e2b_provider.py
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

Locally (no Docker), against the E2B double and the unit tests only, at the
commit that added 05-07: 01 failed 5 tests, 02 failed 5, 03 failed 3, 04
failed 20, 05 failed 2, 06 failed 2, 07 failed 1. 07's killer runs a `find`
wrapper from its tmp dir, so it needs an executable `TMPDIR` (a GitHub
runner's `/tmp` is). The Docker parametrisations need the runner. This is
not mutation evidence; the trusted run is the orchestrator's.
