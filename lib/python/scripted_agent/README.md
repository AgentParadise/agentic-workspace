# scripted-agent

The `claude` (and `codex`) of a syntropic137 load-test workspace: replays one
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
3. Refuses the phase's recording unless its `_recording.cli_version` and its
   `tool_use` count equal the profile's `cli_version` and `tool_uses`.
4. Prints the recording's events at recorded timing x `pacing_factor`, every
   `model` (and `modelUsage` key) rewritten to `syn-scripted-agent`.
5. Runs the workload (`none`, `synthetic`, or `just preflight-agent` in the
   cloned fixture repo), performs the side effect with git/gh, writes
   `artifacts/output/<phase_id>.md` via `render_artifact`, and prints the
   recording's closing `result` event with `result` replaced by
   `task_result(phase_id)`.

It opens no network connection itself. git and gh talk only to the fixture
repository, with the workspace's credential.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | the phase ran |
| 78 | no profile, an invalid one, or no phase/execution id to read it for |
| 65 | the recording is not the declared stream (version, tool uses, unreadable, codex) |
| 69 | a workload or git/gh step failed |

## Limits

- `harness: codex` is refused: there is no codex recording yet to define what a
  codex tool-use event is, so the declared count could not be checked.
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
