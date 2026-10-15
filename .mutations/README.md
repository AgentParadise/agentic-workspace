# Mutation evidence for PR #76 (`providers/contained_files.py`)

Each patch reintroduces one bug in
`lib/python/agentic_isolation/agentic_isolation/providers/contained_files.py`
and nothing else. The runner reads only `*.patch`. This directory is deleted
before merge (CI job `mutations-not-merged`).

The verifier's original nine probes were lost with its workspace; only their
one-line descriptions survive in the PR body. These were rewritten from the
code and cover all nine: old 01 is 16, 02 is 12, 03 is 13, 04 is 17, 05 is 06
and 07, 06 is 10, 07 is 11, 08 is 15 (base `Exception` here, `RuntimeError`
there; the same bug), and 09 is 01. Patches 02-05, 08, 09 and 14 are new. Node ids below are relative to
`lib/python/agentic_isolation`; the workflow's `tests` input (repo-relative) is
at the end.

| Patch | Bug it reintroduces | Killed by |
|---|---|---|
| `01-leaf-open-blocks-on-a-fifo` | Leaf open drops `O_NONBLOCK`, so a FIFO swapped in after the check is waited on | `tests/test_contained_files.py::test_a_fifo_swapped_in_after_the_check_is_still_not_regular`, `tests/test_docker_contract_debt.py::test_a_fifo_swapped_in_after_the_check_is_never_waited_on[file_exists]`, `[read_file]`, `[write_file]` |
| `02-special-file-opened-before-it-is-checked` | Pre-open `os.stat` check removed, so a FIFO (or device node) is opened before it is refused | `tests/test_contained_files.py::test_a_fifo_is_refused_before_it_is_ever_opened` |
| `03-descriptor-not-checked-after-the-open` | Post-open `fstat` check removed, so a FIFO swapped in after the check is returned as an open file | `tests/test_docker_contract_debt.py::test_a_fifo_swapped_in_after_the_check_is_never_waited_on[file_exists]`, `[read_file]` |
| `04-fifo-not-special` | `_is_special` no longer counts a FIFO | `tests/test_contained_files.py::test_a_fifo_is_refused_before_it_is_ever_opened`, `tests/test_docker_contract_debt.py::test_a_workload_fifo_never_blocks_the_event_loop[file_exists]`, `[read_file]`, `tests/test_docker_contract_debt.py::test_a_fifo_swapped_in_after_the_check_is_never_waited_on[file_exists]`, `[read_file]` |
| `05-enxio-not-mapped-to-not-regular` | `ENXIO` from the leaf open (FIFO with no reader) escapes as a raw `OSError` | `tests/test_docker_contract_debt.py::test_a_fifo_swapped_in_after_the_check_is_never_waited_on[write_file]` |
| `06-directory-walk-follows-symlinks` | Directory components opened without `O_NOFOLLOW` | `tests/test_contained_files.py::test_a_symlink_escaping_the_root_is_refused[link/secret]` |
| `07-leaf-open-follows-symlinks` | Leaf opened without `O_NOFOLLOW` | `tests/test_contained_files.py::test_a_symlink_escaping_the_root_is_refused[secret]` |
| `08-symlinked-directory-not-reported-as-escape` | `ELOOP`/`ENOTDIR` on a directory component escapes as a raw `OSError`, not the escape `ValueError` | `tests/test_contained_files.py::test_a_symlink_escaping_the_root_is_refused[link/secret]` |
| `09-symlinked-leaf-not-reported-as-escape` | `ELOOP` on the leaf escapes as a raw `OSError`, not the escape `ValueError` | `tests/test_contained_files.py::test_a_symlink_escaping_the_root_is_refused[secret]` |
| `10-dotdot-climbs-above-the-root` | `..` at the root is skipped instead of refused | `tests/test_contained_files.py::test_dotdot_above_the_root_is_refused` |
| `11-absolute-path-allowed` | Absolute-path check removed | `tests/test_contained_files.py::test_an_absolute_path_is_refused` |
| `12-exists-reports-a-special-file-as-regular` | `exists_contained` answers `(True, True)` for a FIFO | `tests/test_contained_files.py::test_a_fifo_exists_but_is_not_regular_and_is_never_waited_on`, `::test_a_fifo_is_refused_before_it_is_ever_opened`, `::test_a_fifo_swapped_in_after_the_check_is_still_not_regular` |
| `13-exists-reports-a-directory-as-regular` | `exists_contained` skips the `S_ISREG` check on the descriptor | `tests/test_contained_files.py::test_a_directory_exists_but_is_not_regular` |
| `14-exists-raises-for-a-missing-file` | `exists_contained` lets `FileNotFoundError` out instead of answering `(False, False)` | `tests/test_contained_files.py::test_a_missing_file_does_not_exist[missing.md]`, `[missing/plan.md]` |
| `15-not-regular-error-is-not-a-valueerror` | `NotARegularFileError` loses its `ValueError` base | `tests/test_contained_files.py::test_docker_still_exports_the_error_it_raises` |
| `16-exists-reports-a-regular-file-as-not-regular` | `exists_contained` answers `(True, False)` for every opened file | `tests/test_contained_files.py::test_a_regular_file_exists_and_is_regular`, `::test_dotdot_that_stays_inside_the_root_resolves` |
| `17-exists-reports-a-missing-file-as-existing` | `exists_contained` answers `(True, False)` for a missing leaf or parent | `tests/test_contained_files.py::test_a_missing_file_does_not_exist[missing.md]`, `[missing/plan.md]` |

## The hang, and why patch 01 is safe to ship

Without `O_NONBLOCK`, the leaf open of a FIFO waits for its other end.
`test_a_fifo_swapped_in_after_the_check_is_still_not_regular` used to give no
other end, so under patch 01 it waited forever, and the runner has no per-test
timeout. The test now opens the far end after 3 s (`_open_far_end_later`, the
helper the contract-debt FIFO tests already use), so the mutant's open returns
late and the test fails its `< 1.0` s check instead of hanging.

## Special files are covered by two test files

`exists_contained` re-checks `S_ISREG` on the descriptor, so through it alone
dropping the `fstat` check (03) or the `ENXIO` mapping (05) changes no answer.
Those are observed through the Docker provider's `read_file`/`write_file`
FIFO tests in `test_docker_contract_debt.py`, which is why the `tests` input
names them. Patch 02 is observed by the new
`test_a_fifo_is_refused_before_it_is_ever_opened`, which records every
`os.open`; nothing else can see it, because a device node needs `CAP_MKNOD`.

## Workflow input

```
gh workflow run mutation-evidence.yml --ref main -f pr=76 -f tests="lib/python/agentic_isolation/tests/test_contained_files.py lib/python/agentic_isolation/tests/test_docker_contract_debt.py::test_a_workload_fifo_never_blocks_the_event_loop lib/python/agentic_isolation/tests/test_docker_contract_debt.py::test_a_fifo_swapped_in_after_the_check_is_never_waited_on"
```
