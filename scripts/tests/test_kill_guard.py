"""Tests for workspace/kill-guard/kill_guard.py (syntropic137/syntropic137#2355).

The incident: an agent's ``ps | grep | awk | xargs kill`` matched the Claude
CLI itself (its argv carried the prompt) and SIGTERMed it. The guard refuses a
kill whose targets, resolved at hook time, include the hook's ancestors.

Unit tests inject the process table, the producer runner and pgrep. The last
test runs the real hook against real /proc on Linux.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

GUARD_DIR = Path(__file__).resolve().parents[2] / "workspace" / "kill-guard"
sys.path.insert(0, str(GUARD_DIR))

import kill_guard as kg  # noqa: E402
from kill_guard import ProcInfo  # noqa: E402

HOOK = ProcInfo(500, 744, 500, "sh", ("/bin/sh", "-c", "kill-guard"))
HARNESS = ProcInfo(
    744, 700, 700, "node", ("claude", "-p", "fix it; run pytest -m unit", "--verbose")
)
LAUNCHER = ProcInfo(700, 1, 700, "bash", ("bash", "-c", "claude -p ..."))
INIT = ProcInfo(1, 0, 1, "tini", ("/sbin/tini", "--", "bash"))
PROTECTED = {p.pid: p for p in (HOOK, HARNESS, LAUNCHER, INIT)}

#: A fake container: protected chain plus unrelated processes.
OTHERS = [
    ProcInfo(900, 744, 900, "bash", ("bash", "-c", "pytest -m unit")),
    ProcInfo(901, 900, 900, "python3", ("python3", "-m", "pytest", "-m", "unit")),
    ProcInfo(950, 744, 950, "sleep", ("sleep", "100")),
]
ALL = [*PROTECTED.values(), *OTHERS]


def _fake_ps() -> str:
    lines = ["USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND"]
    for p in ALL:
        lines.append(f"agent {p.pid} 0.0 0.1 12345 6789 ? S 10:00 0:00 {p.cmdline}")
    return "\n".join(lines) + "\n"


def _pgrep(args: Sequence[str]) -> list[int] | None:
    """Enough of pgrep: -f PATTERN, -x NAME, -P PPID, or a bare name."""
    args = list(args)
    full = "-f" in args
    rest = [a for a in args if a not in ("-f", "-x")]
    if "-P" in rest:
        ppid = int(rest[rest.index("-P") + 1])
        return [p.pid for p in ALL if p.ppid == ppid]
    if not rest:
        return None
    pat = rest[0]
    return [p.pid for p in ALL if re.search(pat, p.cmdline if full else p.comm)]


def _producer(body: str) -> str | None:
    """Simulate the read-only producers the tests use, against ALL."""
    m = re.fullmatch(r"pgrep (-f )?(\"[^\"]+\"|'[^']+'|\S+)", body.strip())
    if m:
        argv = shlex.split(body)[1:]
        return "\n".join(str(p) for p in _pgrep(argv) or []) + "\n"
    if body.startswith("ps aux | grep"):
        pat = shlex.split(body.split("|")[1])[1]
        rows = [r for r in _fake_ps().splitlines()[1:] if re.search(pat, r)]
        if "awk" in body:
            return "\n".join(r.split()[1] for r in rows) + "\n"
        return "\n".join(rows) + "\n"
    if body.startswith("echo "):
        return body[5:] + "\n"
    raise AssertionError(f"unexpected producer: {body!r}")


def _decide(
    command: str, tool: str = "Bash", protected: dict[int, ProcInfo] | None = None
) -> kg.Refusal | None:
    payload = {"tool_name": tool, "tool_input": {"command": command}}
    return kg.decide(
        payload, PROTECTED if protected is None else protected, _producer, _pgrep
    )


# ---------------------------------------------------------------------------
# Refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # The incident, verbatim.
        """ps aux | grep "[p]ytest -m unit" | awk '{print $2}' | xargs kill""",
        """ps aux | grep "[p]ytest -m unit" | awk '{print $2}' | xargs -r kill -9""",
        """ps aux | grep "[p]ytest -m unit" | awk '{print $2}' | xargs -I{} kill -TERM {}""",
        'kill $(pgrep -f "pytest -m unit")',
        'kill -9 `pgrep -f "pytest -m unit"`',
        'kill "$(pgrep -f "pytest -m unit")"',
        "kill 744",
        "kill -9 744",
        "kill -KILL 744",
        "kill -SIGTERM 744",
        "kill -s TERM 744",
        "kill -n 9 744",
        "/bin/kill 744",
        "kill -- -700",
        "kill -9 -700",
        "kill -- -1",
        "kill -9 -1",
        "kill 1",
        "kill 0",
        "kill $PPID",
        "P=$(pgrep -f x); kill $P",
        'P=$(pgrep -f "pytest -m unit"); kill $P',
        "P=744 && kill -9 $P",
        "export P=744; kill $P",
        "for p in $(pgrep -f unit); do kill $p; done",
        'pkill -f "pytest -m unit"',
        'pkill -9 -f "pytest -m unit"',
        "pkill node",
        "killall node",
        "killall -9 claude",
        "killall -r '^no'",
        # Behind separators, substitutions and wrappers.
        "cd /tmp && kill 744",
        "true; kill 744",
        "false || kill 744",
        "sleep 1 & kill 744",
        "echo $(kill 744)",
        "echo `kill 744`",
        "sudo kill 744",
        "sudo -u root kill -9 744",
        "command kill 744",
        "builtin kill 744",
        "exec kill 744",
        "nohup kill 744",
        "timeout 5 kill 744",
        "timeout -s KILL 5 kill 744",
        "nice -n 10 kill 744",
        "env FOO=1 kill 744",
        "FOO=bar kill 744",
        "bash -c 'kill 744'",
        'sh -lc "kill -9 744"',
        "eval kill 744",
        "if true; then kill 744; fi",
        "( kill 744 )",
        "{ kill 744; }",
        "kill 744 2>/dev/null",
    ],
)
def test_refused(command: str) -> None:
    assert _decide(command) is not None, command


def test_refusal_names_the_pid_and_cmdline() -> None:
    r = _decide("kill -9 744")
    assert r is not None
    msg = r.message()
    assert "744" in msg and "claude -p" in msg
    assert "kill %1" in msg and "pkill -P $$" in msg and "$!" in msg


def test_process_group_refusal_names_a_member() -> None:
    r = _decide("kill -- -700")
    assert r is not None and "process group 700" in r.reason


def test_killall_node_depends_on_the_protected_comm() -> None:
    renamed = {**PROTECTED, 744: ProcInfo(744, 700, 700, "claude", ("claude",))}
    assert _decide("killall node", protected=renamed) is None
    assert _decide("killall claude", protected=renamed) is not None


# ---------------------------------------------------------------------------
# Allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "kill 950",
        "kill -9 950 901",
        "kill -- -900",
        "kill %1",
        "kill -9 %2",
        "kill -l",
        "kill -L",
        "kill -l 9",
        "kill -0 744",
        "kill $!",
        "kill $$",
        "pkill -P $$",
        "pkill -P 900",
        "pkill -f 'python3 -m pytest'",
        "killall sleep",
        "kill $UNKNOWN",
        "kill $(docker ps -q)",
        "kill $(pgrep -f x > /tmp/out)",
        "ls -la",
        "echo killing time",
        "git log --grep kill",
        "grep -r pkill .",
        "pytest -m unit",
        "sleep 30 & pid=$!; kill $pid",
        "",
    ],
)
def test_allowed(command: str) -> None:
    assert _decide(command) is None, command


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Task"])
def test_non_bash_tools_pass(tool: str) -> None:
    assert _decide("kill 744", tool=tool) is None


def test_producers_off_the_allowlist_are_never_run() -> None:
    ran: list[str] = []

    def spy(body: str) -> str | None:
        ran.append(body)
        return "744\n"

    for command in (
        "kill $(docker ps -q)",
        "kill $(pgrep x; rm -rf /)",
        "kill $(pgrep x > /tmp/f)",
        "kill $(awk 'BEGIN{system(\"id\")}')",
        "kill $(sed -i s/a/b/ f)",
        "kill $(sed 's/a/b/e' f)",
        "kill $(sort -o out f)",
        "kill $(cat $(echo f))",
        "kill $(FOO=1 pgrep x)",
        "kill $(/tmp/ps)",
    ):
        kg.decide(
            {"tool_name": "Bash", "tool_input": {"command": command}},
            PROTECTED,
            spy,
            _pgrep,
        )
    assert ran == []


def test_pgrep_is_called_without_pkill_signal_args() -> None:
    seen: list[list[str]] = []

    def spy(args: Sequence[str]) -> list[int] | None:
        seen.append(list(args))
        return []

    kg.decide(
        {"tool_name": "Bash", "tool_input": {"command": "pkill -9 -e -f 'x y'"}},
        PROTECTED,
        _producer,
        spy,
    )
    assert seen == [["-f", "x y"]]


# ---------------------------------------------------------------------------
# main(): fail open, exit codes
# ---------------------------------------------------------------------------


def _lookup(pid: int) -> ProcInfo | None:
    return PROTECTED.get(pid)


@pytest.mark.parametrize(
    "stdin",
    ["not json", "{", "[]", '"x"', '{"tool_name": "Bash", "tool_input": "kill 744"}'],
)
def test_main_fails_open_on_garbage(
    stdin: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert kg.main([], stdin=stdin, lookup=_lookup, self_ppid=500) == 0


def test_main_fails_open_on_internal_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(kg, "decide", boom)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "kill 744"}})
    assert kg.main([], stdin=payload, lookup=_lookup, self_ppid=500) == 0
    assert "kill-guard: internal error" in capsys.readouterr().err


def test_main_blocks_with_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "kill -9 744"}}
    )
    assert kg.main([], stdin=payload, lookup=_lookup, self_ppid=500) == 2
    assert "744" in capsys.readouterr().err


def test_main_unparseable_command_fails_open(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "kill $(pgrep 'unterminated"}}
    )
    assert kg.main([], stdin=payload, lookup=_lookup, self_ppid=500) == 0


def test_protected_set_walks_to_pid_1() -> None:
    assert set(kg.protected_set(500, _lookup)) == {500, 744, 700, 1}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_merges_into_existing_managed_settings(tmp_path: Path) -> None:
    settings = tmp_path / "etc/claude-code/managed-settings.json"
    settings.parent.mkdir(parents=True)
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "dcg"}]}
    settings.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [other]}}))
    kg.register(str(settings))
    kg.register(str(settings))  # idempotent
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    groups = data["hooks"]["PreToolUse"]
    assert groups[0] == other
    assert groups[1:] == [
        {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": kg.HOOK_COMMAND,
                    "timeout": kg.HOOK_TIMEOUT_S,
                }
            ],
        }
    ]
    assert settings.stat().st_mode & 0o777 == 0o644


def test_register_creates_the_file(tmp_path: Path) -> None:
    settings = tmp_path / "managed-settings.json"
    kg.register(str(settings))
    assert (
        json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    )


def test_hook_command_does_not_trust_the_agent_environment() -> None:
    assert kg.HOOK_COMMAND.startswith(
        "/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin "
    )
    assert " -I -S " in kg.HOOK_COMMAND and kg.HOOK_COMMAND.endswith(kg.INSTALL_PATH)


def test_dockerfile_installs_and_registers_the_guard() -> None:
    dockerfile = (
        GUARD_DIR.parents[1] / "implementations/docker/images/omni-agent/Dockerfile"
    ).read_text()
    assert f"COPY workspace/kill-guard/kill_guard.py {kg.INSTALL_PATH}" in dockerfile
    assert f"{kg.INSTALL_PATH} --register" in dockerfile


# ---------------------------------------------------------------------------
# Real /proc (Linux only)
# ---------------------------------------------------------------------------

HARNESS_SH = """\
#!/bin/bash
# Stands in for the Claude CLI: the hook runs as its child, so this process is
# in the hook's ancestor chain. Its argv carries "pytest -m unit", like the
# prompt did in the incident.
for f in "$CASES"/*.json; do
  "$PY" "$GUARD" < "$f" 2> "${f%.json}.err"
  echo $? > "${f%.json}.rc"
done
touch "$CASES/done"
while :; do sleep 1; done
"""


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs Linux /proc")
def test_real_proc_refuses_the_incident_and_harness_survives(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    commands = {
        "a_incident": """ps aux | grep "[p]ytest -m unit" | awk '{print $2}' | xargs kill""",
        "b_pgrep": 'kill $(pgrep -f "pytest -m unit")',
        "c_pkill": 'pkill -f "pytest -m unit"',
        "d_ppid_chain": "kill -9 1",
        "e_allowed": "kill 999999",
    }
    for name, command in commands.items():
        (cases / f"{name}.json").write_text(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        )
    script = tmp_path / "harness.sh"
    script.write_text(HARNESS_SH)
    env = {
        **os.environ,
        "CASES": str(cases),
        "PY": sys.executable,
        "GUARD": str(GUARD_DIR / "kill_guard.py"),
    }
    harness = subprocess.Popen(  # noqa: S603
        ["bash", "-c", 'exec -a "claude -p run pytest -m unit" bash "$0"', str(script)],
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not (cases / "done").exists():
            assert harness.poll() is None, "harness died"
            assert time.monotonic() < deadline, "hook did not finish"
            time.sleep(0.1)
        cmdline = Path(f"/proc/{harness.pid}/cmdline").read_bytes()
        assert b"pytest -m unit" in cmdline
        rcs = {name: (cases / f"{name}.rc").read_text().strip() for name in commands}
        errs = {name: (cases / f"{name}.err").read_text() for name in commands}
        assert rcs == {
            "a_incident": "2",
            "b_pgrep": "2",
            "c_pkill": "2",
            "d_ppid_chain": "2",
            "e_allowed": "0",
        }, errs
        assert str(harness.pid) in errs["a_incident"]
        assert harness.poll() is None, "the hook must not have signalled the harness"
    finally:
        try:
            os.killpg(harness.pid, 9)
        except ProcessLookupError:
            pass
        harness.wait(timeout=10)
