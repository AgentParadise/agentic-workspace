# Mutation evidence

Proves that the tests a PR adds catch the bug they claim to, on a GitHub
runner. Agent workspaces have no Docker, and local Docker verifier runs fire
permission prompts, so this is the place to run it. Ported from syntropic137
(syntropic137/syntropic137#1941); `scripts/mutation_evidence.py` is the runner
and its docstring is the contract.

## Use

1. On the PR branch, write each mutation as a patch of NON-test source under
   `.mutations/` (one bug per file), and commit it:

   ```
   # re-introduce the bug, then:
   git diff -- lib/python/agentic_isolation/agentic_isolation/x.py > .mutations/01-drop-guard.patch
   git checkout -- lib/python/agentic_isolation/agentic_isolation/x.py
   ```

   A `.mutations/README.md` mapping each patch to its killing test helps review;
   the runner reads only `*.patch`.
2. Push, then dispatch from a machine with `gh`:

   ```
   gh workflow run mutation-evidence.yml --ref main -f pr=66 \
     -f tests="lib/python/agentic_isolation/tests/test_harness_credentials.py"
   ```

   Node ids are repo-relative. Each runs in its own uv project
   (`lib/python/<pkg>`, `tests/consumer_contracts`) after
   `uv sync --locked --all-extras`, as `python-compatibility` runs it. Docker is
   available, so `integration`-marked tests run rather than skip.
3. Read the table in the job summary or the single PR comment
   (`<!-- mutation-evidence -->`, updated in place on re-runs). The run fails
   unless every patch is `killed`.
4. Delete `.mutations/` in a final commit before merge. CI job
   `mutations-not-merged` fails any PR into main that still tracks it. The
   comment, which names the head SHA, is the record.

## Verdicts

| Result | Meaning |
|---|---|
| `killed` | A named test failed (or collection broke) and pytest listed it |
| `SURVIVED` | Every named test passed with the patch applied |
| `APPLY FAILED` / `NOT APPLIED` | The patch is stale or changes nothing; regenerate it |
| `REFUSED` | The patch touches a named test, test-shaped code, pytest/build/Cargo config, `.github/`, `scripts/`, a symlinked or git-ignored path, renames or copies, or changes paths it did not declare |
| `ERROR` | pytest exited with no failure on record (interrupt, timeout, usage) |

A red or empty baseline, or a named test that skips, xfails or xpasses (none is
a pass, and each would read as one under every mutant), voids the run before
any patch is applied.

## What it does not prove

- A survival is against the NAMED tests only.
- A kill proves the test is real, not that the fix is right for every input
  shape production produces.
- PR code runs on the runner host (the tests need the Docker daemon), so a PR
  written to forge its own verdict could. The evidence is for honest PR code.
