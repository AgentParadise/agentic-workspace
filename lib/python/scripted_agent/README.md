# scripted-agent

The `claude` and `codex` of a syntropic137 load-test workspace: replays one
phase of a `ScriptedAgentProfile` so N executions run without spending a token
(#44, syntropic137#1717). Shipped by the
[`scripted-agent` image](../../../implementations/docker/images/scripted-agent/Dockerfile);
never part of a production image.

## What one invocation does

1. Loads the profile from `SYN_SCRIPTED_AGENT_PROFILE` (JSON), else from the
   file at `SYN_SCRIPTED_AGENT_PROFILE_PATH`. The variable wins: it is the name
   upstream's `ScriptedAgentProfile.to_env` writes. The file covers stacks that
   mount the driver's `loadtest-profile.json` because syntropic137 has no
   workspace env passthrough yet.
2. Picks the phase and execution from the `phase_id:` and `execution_id:`
   entries of `AGENTIC_SESSION_STORE_TAGS`, the only place syntropic137 hands
   either to a workspace today.
3. Refuses the phase's recording unless it is UTF-8 JSONL of objects opening
   with a `_recording` header and closing with exactly one terminal event
   (claude `result`, codex `turn.completed`), and its `_recording.cli_version`
   and tool-use count equal the profile's `cli_version` and `tool_uses`. A
   claude tool use is a `tool_use` block; a codex one is an `item.started` of a
   `command_execution` or `file_change`, as syntropic137's
   `CodexStreamProcessor` counts them.
4. Prints the recording's events at recorded timing x `pacing_factor`, every
   `model` (and `modelUsage` key) rewritten to `syn-scripted-agent` (codex
   names no model on stdout, so its first event carries one), then
   sleeps the recorded delay before the closing event. The phase's work runs
   after that delay rather than inside it, so the verdict is never earlier
   than its scaled schedule.
5. Runs the workload (`none`, `synthetic`, or `just preflight-agent` in the
   cloned fixture repo), performs the side effect with git/gh, writes
   `artifacts/output/<phase_id>.md` via `render_artifact`, and prints
   `task_result(phase_id)`: as the claude `result` event's `result`, or as a
   last codex `agent_message` before the recording's `turn.completed`.

It opens no network connection itself. git and gh talk only to the fixture
repository, with the workspace's credential.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | the phase ran |
| 78 | no profile, an invalid one, or no phase/execution id to read it for |
| 65 | the recording is not the declared stream (malformed, version, tool uses, subagents) |
| 69 | a workload or git/gh step failed |

## Limits

- The one codex recording, `v0.160.1_codex_exec_two-commands.jsonl`, was
  captured from the real codex 0.160.1 against a local fixture model server
  (`tests/fixtures/record_codex_exec.py`), so its timing is a real CLI's but
  not a real model's.
- Recordings that launch subagents (`Task` or `Agent` tool uses) are refused.
  The session observer records those as subagent starts, not tool executions,
  so the driver's read-back count could never equal `tool_uses`.
- Recordings ship at `/opt/scripted-agent/recordings/` in the image; a profile's
  `stream.recording` names a path there.

## Contract

`scripted_agent/profile.py` is a port of syntropic137's model at the commit in
[`contract/PROVENANCE.md`](scripted_agent/contract/PROVENANCE.md);
`tests/test_contract_parity.py` holds it to the vendored schema byte for byte.

```bash
uv sync --locked --all-extras
uv run pytest -q
```
