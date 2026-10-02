"""Real subprocess launch, stream binding, crash, timeout and shell masking."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentic_session_store.child_journal import ChildJournal
from agentic_session_store.claude_permissions import PERMISSIONS_ENV

PARENT_GRANT = json.dumps(
    {"mode": "bypassPermissions", "tools": None, "allowed": [], "disallowed": []}
)


@pytest.fixture
def environment(tmp_path):
    spool = tmp_path / "spool"
    (spool / ".agentic-session-store/run").mkdir(parents=True)
    binary = tmp_path / "bin"
    binary.mkdir()
    # The fake codex answers `codex sandbox ...` with this exit status.
    (tmp_path / "probe-rc").write_text("0")
    # syn-delegate probes that the delegate's capture hooks reach their guard
    # with this PATH, so the recorder's interpreter must be on it.
    python = binary / "python3"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o700)
    (binary / "sleep").symlink_to(shutil.which("sleep"))
    return {
        **os.environ,
        "PATH": str(binary),
        "AGENTIC_SESSION_STORE_PROVIDER": "local",
        "AGENTIC_SESSION_STORE_SPOOL": str(spool),
        "AGENTIC_SESSION_STORE_PARTITION": "run",
        "AGENTIC_INVOCATION_ID": "invocation",
        "AGENTIC_ATTEMPT_ID": "attempt",
        "AGENTIC_PARENT_HARNESS": "claude",
        "AGENTIC_PARENT_NATIVE_ID": "parent",
        PERMISSIONS_ENV: PARENT_GRANT,
    }


def _fake(environment, body):
    """Fake codex: `codex sandbox` is the probe, anything else runs `body`."""
    binary = Path(environment["PATH"].split(os.pathsep)[0]) / "codex"
    probe = binary.parent.parent / "probe-rc"
    probes = binary.parent.parent / "probes.jsonl"
    dispatch = (
        "import json as _j, sys as _s\n"
        "if _s.argv[1:2] == ['sandbox']:\n"
        f"    open({str(probes)!r}, 'a').write(_j.dumps(_s.argv[1:]) + '\\n')\n"
        f"    _rc = int(open({str(probe)!r}).read())\n"
        "    if _rc: print('bwrap: No permissions to create a new namespace', file=_s.stderr)\n"
        "    raise SystemExit(_rc)\n"
    )
    binary.write_text(f"#!{sys.executable}\n" + dispatch + body)
    binary.chmod(0o700)


def _fake_claude(environment, body, logged_in=True):
    """Fake claude: `claude auth status` reports `logged_in`; anything else
    runs `body`."""
    binary = Path(environment["PATH"].split(os.pathsep)[0]) / "claude"
    status = json.dumps({"loggedIn": logged_in, "authMethod": "api_key"})
    binary.write_text(
        f"#!{sys.executable}\nimport sys as _s\n"
        "if _s.argv[1:3] == ['auth', 'status']:\n"
        f"    print({status!r})\n"
        f"    raise SystemExit({0 if logged_in else 1})\n" + body
    )
    binary.chmod(0o700)
    return binary


def _journal(environment):
    return ChildJournal(
        Path(environment["AGENTIC_SESSION_STORE_SPOOL"])
        / ".agentic-session-store/run/children.sqlite"
    )


def _command(timeout=5):
    return [
        sys.executable,
        "-m",
        "agentic_session_store.delegate",
        "codex",
        "--prompt",
        "PRIVATE",
        "--timeout",
        str(timeout),
    ]


@pytest.mark.parametrize("exit_code", [0, 9])
def test_real_process_identity_and_status_survive_restart(environment, exit_code):
    _fake(
        environment,
        'import json,sys\nprint(json.dumps({"type":"thread.started","thread_id":"child"}),flush=True)\nsys.exit('
        + str(exit_code)
        + ")\n",
    )
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == exit_code
    changes = _journal(environment).page().changes
    assert len(changes) == 4
    assert changes[0].intent.child_native_id is None
    assert changes[-1].intent.call.target_harness == "codex"
    assert changes[-1].intent.call.harness == "claude"
    assert changes[-1].intent.child_native_id == "child"
    assert changes[-1].intent.exit_code == exit_code
    assert b"PRIVATE" not in result.stderr


def test_shell_success_cannot_hide_delegate_failure(environment):
    import shlex

    _fake(environment, "raise SystemExit(13)\n")
    result = subprocess.run(
        ["/bin/sh", "-c", shlex.join(_command()) + " || true"],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert _journal(environment).page().changes[-1].intent.exit_code == 13


def test_missing_context_prevents_process_launch(environment, tmp_path):
    marker = tmp_path / "launched"
    _fake(environment, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    environment.pop("AGENTIC_PARENT_NATIVE_ID")
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 70
    assert not marker.exists()
    assert not _journal(environment).page().changes


def test_timeout_records_actual_signal_exit(environment):
    _fake(environment, "import time\ntime.sleep(30)\n")
    result = subprocess.run(
        _command(0.1), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 128 + signal.SIGTERM
    last = _journal(environment).page().changes[-1].intent
    assert last.status == "cancelled"
    assert last.exit_code == -signal.SIGTERM


def test_cancellation_terminates_child_and_records_outcome(environment):
    _fake(
        environment,
        'import json,time\nprint(json.dumps({"type":"thread.started","thread_id":"child"}),flush=True)\ntime.sleep(30)\n',
    )
    process = subprocess.Popen(
        _command(), env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            changes = _journal(environment).page().changes
            if changes and changes[-1].intent.child_native_id:
                break
            time.sleep(0.02)
        else:
            pytest.fail("delegate did not bind")
        process.terminate()
        process.communicate(timeout=10)
        assert process.returncode == 128 + signal.SIGTERM
        assert (
            _journal(environment).page().changes[-1].intent.exit_code == -signal.SIGTERM
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_codex_native_environment_supplies_parent_and_is_not_inherited(environment):
    environment.pop("AGENTIC_PARENT_HARNESS")
    environment.pop("AGENTIC_PARENT_NATIVE_ID")
    environment["CODEX_THREAD_ID"] = "native-codex-parent"
    environment["CLAUDECODE"] = "1"
    _fake(
        environment,
        'import os\nassert "CODEX_THREAD_ID" not in os.environ\nassert "CLAUDECODE" not in os.environ\nassert os.environ["AGENTIC_INVOCATION_ID"] == "invocation"\n',
    )
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    call = _journal(environment).page().changes[-1].intent.call
    assert call.harness == "codex"
    assert call.parent_native_id == "native-codex-parent"


def test_failed_os_launch_retains_distinct_intent(environment):
    # Claude, because a Codex binary that cannot execute already fails the
    # sandbox probe (covered below) before any launch is attempted.
    binary = Path(environment["PATH"].split(os.pathsep)[0]) / "claude"
    binary.write_text("#!/definitely-missing-interpreter\n")
    binary.chmod(0o700)
    command = _command()
    command[command.index("codex")] = "claude"
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 127
    last = _journal(environment).page().changes[-1].intent
    assert last.status == "launch_failed"
    assert last.reason == "process_start_failed"
    assert last.child_native_id is None
    assert last.exit_code is None


def _argv_recorder(tmp_path):
    record = tmp_path / "argv.json"
    return record, (
        f"import json,sys\nopen({str(record)!r},'w').write(json.dumps(sys.argv[1:]))\n"
    )


def test_codex_receives_explicit_workspace_write_sandbox(environment, tmp_path):
    record, body = _argv_recorder(tmp_path)
    _fake(environment, body)
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(record.read_text())
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


@pytest.mark.parametrize(
    ("cli", "env", "expected"),
    [
        (None, "read-only", "read-only"),
        ("workspace-write", "read-only", "workspace-write"),
        ("read-only", None, "read-only"),
    ],
)
def test_sandbox_mode_override(environment, tmp_path, cli, env, expected):
    record, body = _argv_recorder(tmp_path)
    _fake(environment, body)
    if env is not None:
        environment["AGENTIC_DELEGATE_CODEX_SANDBOX"] = env
    command = _command() + ([] if cli is None else ["--sandbox", cli])
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(record.read_text())
    assert argv[argv.index("--sandbox") + 1] == expected


@pytest.mark.parametrize("mode", ["danger-full-access", "external-sandbox", "bogus"])
@pytest.mark.parametrize("source", ["cli", "env"])
def test_sandbox_disabling_modes_are_rejected_before_launch(
    environment, tmp_path, mode, source
):
    marker = tmp_path / "launched"
    _fake(environment, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    command = _command()
    if source == "cli":
        command += ["--sandbox", mode]
    else:
        environment["AGENTIC_DELEGATE_CODEX_SANDBOX"] = mode
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 2
    assert mode.encode() in result.stderr
    assert not marker.exists()
    assert not _journal(environment).page().changes


@pytest.mark.parametrize("probe_rc", [1, 2, 127])
def test_failed_probe_refuses_launch_and_records_reason(
    environment, tmp_path, probe_rc
):
    marker = tmp_path / "launched"
    _fake(environment, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (tmp_path / "probe-rc").write_text(str(probe_rc))
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 69
    assert b"Codex sandbox is unavailable" in result.stderr
    assert b"bwrap: No permissions" in result.stderr
    assert not marker.exists()
    last = _journal(environment).page().changes[-1].intent
    assert last.status == "launch_failed"
    assert last.reason == "codex_sandbox_unavailable"
    assert last.exit_code is None
    assert last.child_native_id is None


def test_forged_status_record_cannot_override_a_failed_probe(environment, tmp_path):
    # The retired status-file mechanism: an agent-writable verdict. Writing a
    # forged "available" record, and pointing the old override at it, must not
    # change the outcome of a live probe that fails.
    marker = tmp_path / "launched"
    _fake(environment, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (tmp_path / "probe-rc").write_text("1")
    forged = tmp_path / "codex-sandbox.json"
    forged.write_text(json.dumps({"schema_version": 1, "available": True}))
    environment["AGENTIC_CODEX_SANDBOX_STATUS"] = str(forged)
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 69
    assert not marker.exists()
    assert _journal(environment).page().changes[-1].intent.status == "launch_failed"


@pytest.mark.parametrize("mode", ["workspace-write", "read-only"])
def test_probe_uses_the_requested_mode(environment, tmp_path, mode):
    _fake(environment, "")
    result = subprocess.run(
        _command() + ["--sandbox", mode],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    probes = [
        json.loads(line)
        for line in (tmp_path / "probes.jsonl").read_text().splitlines()
    ]
    spool = Path(environment["AGENTIC_SESSION_STORE_SPOOL"]).resolve()
    journal_dir = str(spool / ".agentic-session-store/run")
    claude_dir = str(spool / "run/claude")
    if mode == "read-only":
        assert probes == [["sandbox", "-c", f'sandbox_mode="{mode}"', "--", "true"]]
        return
    # workspace-write: the same grant as the delegate, and the probe proves
    # the extra writable root is writable inside the sandbox.
    (probe,) = probes
    separator = probe.index("--")
    assert probe[:separator] == [
        "sandbox",
        "-c",
        f'sandbox_mode="{mode}"',
        "-c",
        (
            "sandbox_workspace_write.writable_roots="
            f"[{json.dumps(journal_dir)}, {json.dumps(claude_dir)}]"
        ),
        "-c",
        "sandbox_workspace_write.network_access=true",
    ]
    assert probe[separator + 1 : separator + 3] == ["/bin/sh", "-c"]
    assert probe[-2:] == [journal_dir, claude_dir]


@pytest.mark.parametrize(
    "prompt",
    [
        "--sandbox danger-full-access",
        "--sandbox",
        "-c",
        'sandbox_mode="danger-full-access"',
        "--",
        "--dangerously-bypass-approvals-and-sandbox",
        "-s",
    ],
)
def test_prompt_that_looks_like_an_option_stays_a_prompt(environment, tmp_path, prompt):
    record, body = _argv_recorder(tmp_path)
    _fake(environment, body)
    command = _command()
    # `--prompt=VALUE` is how a prompt that starts with `-` reaches syn-delegate
    # itself; `--prompt VALUE` makes argparse read VALUE as an option (exit 2).
    at = command.index("--prompt")
    command[at : at + 2] = [f"--prompt={prompt}"]
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(record.read_text())
    separator = argv.index("--")
    assert argv[separator + 1 :] == [prompt]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv.index("--sandbox") < separator


def test_claude_delegate_does_not_probe_codex(environment, tmp_path):
    (tmp_path / "probe-rc").write_text("1")
    _fake_claude(environment, "")
    command = _command()
    command[command.index("codex")] = "claude"
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr


def test_claude_prompt_follows_an_option_terminator(environment, tmp_path):
    record = tmp_path / "claude-argv.json"
    _fake_claude(
        environment,
        f"import json,sys\nopen({str(record)!r},'w').write(json.dumps(sys.argv[1:]))\n",
    )
    command = _command()
    command[command.index("codex")] = "claude"
    at = command.index("--prompt")
    command[at : at + 2] = ["--prompt=--dangerously-skip-permissions"]
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(record.read_text())
    assert argv[-2:] == ["--", "--dangerously-skip-permissions"]


def test_sandbox_flag_is_codex_only(environment):
    command = _command() + ["--sandbox", "read-only"]
    command[command.index("codex")] = "claude"
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 2
    assert b"codex only" in result.stderr


def test_child_environment_drops_agent_modifiable_startup_files(tmp_path) -> None:
    from agentic_session_store.delegate import child_environment

    rc = tmp_path / "rc"
    rc.write_text("exit 0\n")
    child = child_environment({"BASH_ENV": str(rc), "ENV": str(rc), "KEEP": "1"})
    assert child == {"KEEP": "1"}


def test_delegate_refuses_when_its_capture_hooks_cannot_run(environment, tmp_path):
    """The delegate's own hooks need python3 on the child's PATH; without it a
    native spawn inside the delegate would have no durable intent."""
    _fake(environment, "raise SystemExit(0)\n")
    (tmp_path / "bin/python3").unlink()
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=30, check=False
    )
    assert result.returncode == 70, result.stderr
    intent = _journal(environment).page().changes[-1].intent
    assert (intent.status, intent.reason) == (
        "launch_failed",
        "capture_hook_unreachable",
    )


# --- agentic-workspace#19: nested delegation from a workspace-write Codex ---


def test_workspace_write_codex_gets_only_the_journal_and_claude_transcript_roots(
    environment, tmp_path
):
    """The journal partition, this partition's Claude transcript root (so a
    Claude grandchild's transcript is captured) and the network; never the
    spool, the partition root, the Codex transcript root or the namespace."""
    record, body = _argv_recorder(tmp_path)
    _fake(environment, body)
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(record.read_text())
    spool = Path(environment["AGENTIC_SESSION_STORE_SPOOL"]).resolve()
    roots = [str(spool / ".agentic-session-store/run"), str(spool / "run/claude")]
    overrides = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
    assert overrides == [
        f"sandbox_workspace_write.writable_roots=[{', '.join(map(json.dumps, roots))}]",
        "sandbox_workspace_write.network_access=true",
    ]
    for broader in (
        spool,
        spool / "run",
        spool / "run/codex",
        spool / ".agentic-session-store",
    ):
        assert json.dumps(str(broader)) not in overrides[0]
    assert "--add-dir" not in argv
    assert "danger-full-access" not in " ".join(argv)
    assert argv.index("-c") < argv.index("--")


def test_read_only_codex_gets_no_grant(environment, tmp_path):
    record, body = _argv_recorder(tmp_path)
    _fake(environment, body)
    result = subprocess.run(
        _command() + ["--sandbox", "read-only"],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(record.read_text())
    assert "-c" not in argv
    assert not any("writable_roots" in arg or "network" in arg for arg in argv)


def test_journal_denied_launch_prints_a_denial_and_starts_nothing(
    environment, tmp_path
):
    from agentic_session_store.delegate import DENIAL_MARKER, DENIAL_NONCE_ENV, denials

    marker = tmp_path / "launched"
    _fake(environment, f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    environment[DENIAL_NONCE_ENV] = "inherited-nonce"
    # The spool exists but its partition cannot be created or written.
    spool = Path(environment["AGENTIC_SESSION_STORE_SPOOL"])
    shutil.rmtree(spool)
    spool.mkdir()
    spool.chmod(0o500)
    try:
        result = subprocess.run(
            _command(), env=environment, capture_output=True, timeout=10, check=False
        )
    finally:
        spool.chmod(0o700)
    assert result.returncode == 70
    assert not marker.exists()
    (line,) = [
        line
        for line in result.stderr.decode().splitlines()
        if line.startswith(DENIAL_MARKER)
    ]
    record = json.loads(line.split(" ", 1)[1])
    assert record["target"] == "codex"
    assert record["nonce"] == "inherited-nonce"
    event = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "aggregated_output": "noise\n" + line + "\n",
            },
        }
    ).encode()
    assert denials(event, "codex", "inherited-nonce") == [(record["id"], "codex")]
    # Another delegate's nonce: not this child's denial.
    assert denials(event, "codex", "other-nonce") == []


def _codex_stream(*outputs):
    """Fake Codex stream. In each output, DENIED becomes a denial line carrying
    the nonce this delegate handed its child, FOREIGN one with another nonce."""
    return (
        "import json, os\n"
        "from agentic_session_store.delegate import DENIAL_NONCE_ENV, DENIAL_MARKER\n"
        "nonce = os.environ[DENIAL_NONCE_ENV]\n"
        "def line(id, nonce):\n"
        "    return DENIAL_MARKER + ' ' + json.dumps("
        "{'id': id, 'target': 'claude', 'nonce': nonce})\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'codex-child'}),"
        " flush=True)\n"
        f"for i, output in enumerate({list(outputs)!r}):\n"
        "    output = output.replace('DENIED', line('d1', nonce))"
        ".replace('FOREIGN', line('f1', 'foreign'))\n"
        "    print(json.dumps({'type': 'item.completed', 'item': {'id': f'item_{i}',"
        " 'type': 'command_execution', 'aggregated_output': output,"
        " 'exit_code': 70, 'status': 'failed'}}), flush=True)\n"
    )


def _by_parent(environment):
    latest = {
        change.intent.child_invocation_id: change.intent
        for change in _journal(environment).page().changes
    }
    return {intent.call.parent_native_id: intent for intent in latest.values()}


def test_enclosing_delegate_records_a_nested_denial_as_launch_failed(environment):
    # Repeated, and once inside other output: one record per denial id.
    _fake(environment, _codex_stream("DENIED", "x\nDENIED\ny", "plain output"))
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    by_parent = _by_parent(environment)
    assert set(by_parent) == {"parent", "codex-child"}
    nested = by_parent["codex-child"]
    assert nested.call.harness == "codex"
    assert nested.call.target_harness == "claude"
    assert nested.call.tool_call_id == "denied-d1"
    assert (nested.status, nested.reason) == (
        "launch_failed",
        "nested_journal_unavailable",
    )
    assert nested.child_native_id is None
    assert (nested.call.invocation_id, nested.call.attempt_id) == (
        "invocation",
        "attempt",
    )
    assert by_parent["parent"].status == "completed"


def test_denial_with_another_nonce_is_ignored(environment):
    # Output copied from elsewhere (another delegate's run, a log).
    _fake(environment, _codex_stream("FOREIGN"))
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert set(_by_parent(environment)) == {"parent"}


def test_denial_text_outside_tool_output_is_ignored(environment):
    # A correct denial line, with the right nonce, in the agent's own message.
    body = _codex_stream() + (
        "print(json.dumps({'type': 'item.completed', 'item': {'type':"
        " 'agent_message', 'text': line('d2', nonce)}}), flush=True)\n"
    )
    _fake(environment, body)
    result = subprocess.run(
        _command(), env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert set(_by_parent(environment)) == {"parent"}


def test_claude_stream_denials_are_read_from_tool_results():
    from agentic_session_store.delegate import denial_line, denials

    denied = denial_line("codex", "n")
    event = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "content": [{"type": "text", "text": denied}]}
            ]
        },
    }
    assert [t for _, t in denials(json.dumps(event).encode(), "claude", "n")] == [
        "codex"
    ]
    forged = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": denied}]},
    }
    assert denials(json.dumps(forged).encode(), "claude", "n") == []


def test_claude_delegate_from_a_subagent_is_refused(environment, tmp_path):
    # The Bash hook exports no grant for a native subagent (its own tool list
    # may be narrower than the session's), so the delegate refuses.
    from agentic_session_store.command_context import command_context

    output = command_context(
        json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "session_id": "root",
                "agent_id": "sub",
                "permission_mode": "bypassPermissions",
                "tool_input": {"command": "true"},
            }
        ).encode(),
        {**environment, "CLAUDE_PID": str(os.getppid())},
    )
    prefix = output["hookSpecificOutput"]["updatedInput"]["command"]
    assert prefix.startswith(f"unset {PERMISSIONS_ENV}\n")
    assert PERMISSIONS_ENV + "=" not in prefix


# --- agentic-workspace#20: the delegated Claude inherits its parent's grant ---


def _claude_argv(environment, tmp_path, logged_in=True):
    record = tmp_path / "claude-argv.json"
    marker = tmp_path / "claude-env.json"
    _fake_claude(
        environment,
        "import json,os,sys\n"
        f"open({str(record)!r},'w').write(json.dumps(sys.argv[1:]))\n"
        f"open({str(marker)!r},'w').write(json.dumps(dict(os.environ)))\n",
        logged_in=logged_in,
    )
    command = _command()
    command[command.index("codex")] = "claude"
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    argv = json.loads(record.read_text()) if record.exists() else None
    child_env = json.loads(marker.read_text()) if marker.exists() else None
    return result, argv, child_env


def test_claude_child_inherits_the_claude_parent_grant(environment, tmp_path):
    environment[PERMISSIONS_ENV] = json.dumps(
        {
            "mode": "dontAsk",
            "tools": ["Bash", "Read"],
            "allowed": ["Bash(git *)"],
            "disallowed": ["WebFetch"],
        }
    )
    result, argv, child_env = _claude_argv(environment, tmp_path)
    assert result.returncode == 0, result.stderr
    separator = argv.index("--")
    assert argv[:separator] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Bash,Read",
        "--allowedTools",
        "Bash(git *)",
        "--disallowedTools",
        "WebFetch",
    ]
    assert "--dangerously-skip-permissions" not in argv
    # The grant is not inherited as a marker: the child's own hook sets it.
    assert PERMISSIONS_ENV not in child_env


def test_claude_child_of_bypass_parent_gets_bypass(environment, tmp_path):
    result, argv, _ = _claude_argv(environment, tmp_path)
    assert result.returncode == 0, result.stderr
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_claude_child_of_codex_parent_gets_dont_ask_with_codex_tools(
    environment, tmp_path
):
    for key in ("AGENTIC_PARENT_HARNESS", "AGENTIC_PARENT_NATIVE_ID", PERMISSIONS_ENV):
        environment.pop(key)
    environment["CODEX_THREAD_ID"] = "codex-parent"
    result, argv, _ = _claude_argv(environment, tmp_path)
    assert result.returncode == 0, result.stderr
    separator = argv.index("--")
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--allowedTools") + 1 : separator] == [
        "Bash",
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
    ]
    assert "--tools" not in argv


@pytest.mark.parametrize("grant", [None, "not json", '{"mode":"yolo"}'])
def test_unknown_claude_parent_grant_refuses_launch(environment, tmp_path, grant):
    if grant is None:
        environment.pop(PERMISSIONS_ENV)
    else:
        environment[PERMISSIONS_ENV] = grant
    result, argv, _ = _claude_argv(environment, tmp_path)
    assert result.returncode == 70
    assert argv is None
    assert b"permission mode and tools are unavailable" in result.stderr
    intent = _journal(environment).page().changes[-1].intent
    assert (intent.status, intent.reason) == (
        "launch_failed",
        "parent_permissions_unavailable",
    )


# --- agentic-workspace#21: a Claude child without credentials is refused ---


def test_claude_child_without_credentials_is_refused(environment, tmp_path):
    result, argv, _ = _claude_argv(environment, tmp_path, logged_in=False)
    assert result.returncode == 69
    assert argv is None
    assert b"removes its OAuth token from Bash subprocesses" in result.stderr
    intent = _journal(environment).page().changes[-1].intent
    assert (intent.status, intent.reason) == (
        "launch_failed",
        "claude_nested_auth_unavailable",
    )
    assert intent.child_native_id is None


def test_auth_probe_sees_exactly_the_child_environment(environment, tmp_path):
    # The probe runs with the delegate's own environment; no credential is
    # ever added to it.
    seen = tmp_path / "probe-env.json"
    binary = Path(environment["PATH"].split(os.pathsep)[0]) / "claude"
    binary.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "if sys.argv[1:3] == ['auth', 'status']:\n"
        f"    open({str(seen)!r},'w').write(json.dumps(dict(os.environ)))\n"
        "    print(json.dumps({'loggedIn': False}))\n"
        "    raise SystemExit(1)\n"
    )
    binary.chmod(0o700)
    environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    environment["CLAUDE_PID"] = "1"
    command = _command()
    command[command.index("codex")] = "claude"
    result = subprocess.run(
        command, env=environment, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 69
    probe_env = json.loads(seen.read_text())
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in probe_env
    assert "CLAUDE_PID" not in probe_env
    assert PERMISSIONS_ENV not in probe_env
