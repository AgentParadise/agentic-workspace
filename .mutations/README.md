# Mutation evidence for LH-6 `loss_detected` tests

Removed before merge (CI job `mutations-not-merged`).

`mutation-evidence.yml` runs pytest only, so the Python patches sit directly in
`.mutations/` (the default `mutations_dir`) and the Rust patch sits in
`.mutations/rust/`, which that runner never reads. Dispatch:

```
gh workflow run mutation-evidence.yml --ref main -f pr=77 \
  -f tests="lib/python/agentic_isolation/tests/test_spool_loss_detected.py"
```

| Patch | Bug | Killing tests |
|---|---|---|
| `01-drop-python-loss-detected.patch` | Python spool never sets `loss_detected` | `test_spool_loss_detected.py` rules b, c, d |
| `02-python-loss-on-origin-page.patch` | Python spool flags loss on a page read from `cursor=None` with no eviction | rule a (page 0) and rule b (first page) |
| `03-python-loss-after-first-page.patch` | Python spool flags loss on every page after the first, with no eviction | rule a: the flagged page no longer fits the page bound (`rejected_policy`), a paging failure rather than the field assertion |
| `04-python-loss-only-on-gap-page.patch` | Python loss is not sticky: only the gap page carries it | rules c and d, at page index 1 |
| `rust/01-drop-rust-then-some.patch` | Rust spool never sets `loss_detected` | Rust, run locally: `loss_is_sticky_from_a_redacted_gap_to_the_final_empty_page`. Result recorded in the PR body. |
