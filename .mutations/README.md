# Mutation patches for syntropic137#1605 (upstream failure classifiers)

Each patch puts back one bug. Run all of them against:

```
lib/python/agentic_isolation/tests/test_claude_upstream_failure.py
lib/python/agentic_isolation/tests/test_codex_upstream_failure.py
```

| patch | bug | killing test |
|---|---|---|
| `01-claude-quota-in-429-read-as-rate-limited.patch` | the quota sentence goes unread inside a `rate_limit_error` body, so quota is misread as RATE_LIMITED (transient) | `test_claude_upstream_failure.py::test_quota_inside_a_429_body_is_quota_not_rate_limited` |
| `02-claude-reset-year-never-rolls-over.patch` | the reset always lands in the observation's own year, so the December -> January rollover is wrong | `test_claude_upstream_failure.py::test_reset_rolls_over_december_to_january` (also `test_reset_already_passed_this_year_is_next_year`) |
| `03-claude-unknown-collapsed-to-none.patch` | claude UNKNOWN collapses to None | `test_claude_upstream_failure.py::test_unplaced_fault_is_unknown_never_none` |
| `04-codex-unknown-collapsed-to-none.patch` | codex UNKNOWN collapses to None | `test_codex_upstream_failure.py::test_unrecognised_fault_is_unknown_never_none` |
| `05-codex-usage-limit-read-as-rate-limited.patch` | codex usage-limit quota is misread as RATE_LIMITED (transient) | `test_codex_upstream_failure.py::test_recorded_faults_are_classified` |

All five were killed in a local run in the agent workspace. That run is not evidence: the evidence is the trusted `mutation-evidence.yml` run.
