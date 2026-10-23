# Mutations for #100 (bounded, link-safe `read_files`)

Each patch re-introduces one bug in
`lib/python/agentic_isolation/agentic_isolation/providers/file_transfer.py`.
Named tests to run (repo-relative):

```
lib/python/agentic_isolation/tests/test_file_transfer.py
lib/python/agentic_isolation/tests/test_remote_capabilities.py
lib/python/agentic_isolation/tests/integration/test_remote_capabilities_conformance.py
```

| Patch | Bug | Killing tests |
|---|---|---|
| `01-per-file-limit-unenforced.patch` | `max_file_bytes` is checked neither in the plan nor in the archive | `test_read_files_refuses_a_file_over_the_per_file_limit`, `test_read_files_without_limits_is_bounded_by_default` (Docker and E2B double), `test_a_file_grown_after_the_listing_is_held_to_the_limits` |
| `02-hard-link-followed.patch` | `st_nlink > 1` files are read, and tar hard-link members are returned | `test_read_files_refuses_every_name_of_a_hard_link`, `test_read_files_refuses_hard_links_and_returns_exact_bytes` |
| `03-escaping-symlink-followed.patch` | a symlink that escapes the root is planned and the archive dereferences it | `test_read_files_refuses_symlinks_that_escape_the_root` |
| `04-refusal-swallowed.patch` | refusals are dropped from the result | every refusal case above |

Locally (no Docker), against the E2B double and the unit tests only: 01 failed
4 tests, 02 failed 3, 03 failed 1, 04 failed 13. The Docker parametrisations
need the runner.
