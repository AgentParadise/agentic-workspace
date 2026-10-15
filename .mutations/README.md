# Mutations for PR #66 (harness credential presentation)

Each patch re-introduces one presentation bug in host-side source. The killer
is the live round trip in
`lib/python/agentic_isolation/tests/integration/test_harness_credential_presentation.py`,
which runs the real CLI in the release-pinned claude-cli image
(`tests/integration/claude-cli.release-pin`, pulled when absent; the tests
fail rather than skip if it cannot be pulled).

| Patch | Bug | Killer | Expected failure |
|---|---|---|---|
| `01-claude-base-path-wrong.patch` | `claude/credentials.py` presents `ANTHROPIC_BASE_URL` as `<base>/wrong` instead of `<base>/anthropic`, so Claude Code calls the wrong broker route | `[claude-api-key]` | request-path assertion in `_assert_round_trip`: `[('POST', '/wrong/v1/messages')]` |
| `02-codex-websockets-true.patch` | `codex/credentials.py` writes `supports_websockets = true`, so Codex dials a WebSocket before falling back and one turn becomes several requests | `[codex-api-key]` | same assertion: repeated `('GET', '/openai/v1/responses')` instead of one `POST` |

These are the two presentation mutations every reverify report on the PR
names. The other mutations in that thread target
`tests/integration/harness_credential_scan.py`, which the runner refuses as
test code.

## Workflow `tests` input

```
lib/python/agentic_isolation/tests/integration/test_harness_credential_presentation.py::test_pinned_cli_sends_only_the_wire_capability[claude-api-key] lib/python/agentic_isolation/tests/integration/test_harness_credential_presentation.py::test_pinned_cli_sends_only_the_wire_capability[codex-api-key]
```

Delete `.mutations/` before merge.
