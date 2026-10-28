"""Scripted-agent image smoke, run INSIDE the image by the replayer's own Python.

Proves the shipped image, as the agent user with no network, replays a tiny
two-phase profile through both harness names:

- ``claude`` and ``codex`` on PATH are the replayer shims, and neither real
  CLI package is installed,
- ``--version`` reports each phase's recorded CLI version,
- ``claude -p`` replays a baked claude recording and ``codex exec`` a baked
  codex recording, every model reported as ``syn-scripted-agent``, each
  stream closing with the profile's ``TASK_RESULT``,
- each phase writes its rendered artifact under ``/workspace/artifacts/output``,
- each phase leaves the native transcript its harness would, under ``$HOME``
  (``.claude/projects/-workspace/<session>.jsonl``,
  ``.codex/sessions/.../rollout-*-<thread>.jsonl``), named for the session id
  on its stream,
- the baked ``apss-session-exporter`` (0.6.0) archives both into a local spool
  as one ClaudeCode and one Codex session with those ids, each recording the
  scripted model,
- no profile at all is refused with exit 78 (EX_CONFIG).

Prints ``SCRIPTED AGENT SMOKE PASS`` as its last line only when all hold.
Standard library only, so it needs nothing the image does not already ship.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

RECORDINGS = Path("/opt/scripted-agent/recordings")
OUTPUT = Path("/workspace/artifacts/output")
MODEL = "syn-scripted-agent"
EXECUTION = "smoke-1"

PROFILE = {
    "tier": "platform",
    "fixture_repo": "example-org/loadtest-fixture",
    "phases": {
        "implement": {
            "stream": {
                "harness": "claude",
                "recording": str(
                    RECORDINGS / "v2.0.74_claude-sonnet-4-5_multi-tool.jsonl"
                ),
                "cli_version": "2.0.74",
                "tool_uses": 2,
                "pacing_factor": 0,
            },
            "workload": {"kind": "none"},
            "side_effect": {"kind": "report_only"},
            "artifact": "implement {execution_id} on {branch}",
        },
        "review": {
            "stream": {
                "harness": "codex",
                "recording": str(RECORDINGS / "v0.160.1_codex_exec_two-commands.jsonl"),
                "cli_version": "0.160.1",
                "tool_uses": 2,
                "pacing_factor": 0,
            },
            "workload": {"kind": "none"},
            "side_effect": {"kind": "report_only"},
            "artifact": "review {review_verdict}",
            "review_verdict": "certified",
        },
    },
}


def fail(message: str) -> None:
    print(f"[smoke] FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def env_for(phase_id: str | None) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("SYN_SCRIPTED_AGENT_")
    }
    if phase_id is not None:
        env["SYN_SCRIPTED_AGENT_PROFILE"] = json.dumps(PROFILE)
        env["AGENTIC_SESSION_STORE_TAGS"] = (
            f"phase_id:{phase_id},execution_id:{EXECUTION}"
        )
    return env


def run(argv: list[str], phase_id: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=env_for(phase_id),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def events(
    done: subprocess.CompletedProcess[str], what: str
) -> list[dict[str, object]]:
    if done.returncode != 0:
        fail(f"{what} exited {done.returncode}: {done.stderr.strip()}")
    try:
        parsed = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as e:
        fail(f"{what} printed a non-JSON line: {e}")
    if not parsed:
        fail(f"{what} printed no events")
    return parsed


def models(event: object) -> set[str]:
    """Every ``model`` value anywhere in an event."""
    found: set[str] = set()
    if isinstance(event, dict):
        for key, value in event.items():
            if key == "model" and isinstance(value, str):
                found.add(value)
            found |= models(value)
    elif isinstance(event, list):
        for item in event:
            found |= models(item)
    return found


def strings(event: object) -> list[str]:
    """Every string value anywhere in an event."""
    if isinstance(event, str):
        return [event]
    if isinstance(event, dict):
        return [s for value in event.values() for s in strings(value)]
    if isinstance(event, list):
        return [s for item in event for s in strings(item)]
    return []


def task_result(text: str) -> dict[str, object]:
    """The JSON object of a ``TASK_RESULT: {...}\\nTASK_RESULT_END`` block."""
    body = text.removeprefix("TASK_RESULT:").split("TASK_RESULT_END", 1)[0]
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        fail(f"TASK_RESULT is not JSON ({e}): {text!r}")
    return parsed if isinstance(parsed, dict) else {}


def check_shims() -> None:
    for name in ("claude", "codex"):
        path = shutil.which(name)
        if path is None:
            fail(f"{name} is not on PATH")
        if "/opt/scripted-agent/venv/bin/scripted-agent" not in Path(path).read_text():
            fail(f"{path} is not the scripted-agent shim")
    for package in ("@anthropic-ai/claude-code", "@openai/codex"):
        if Path("/usr/local/lib/node_modules", package).exists():
            fail(f"the real {package} is still installed")
    print("[smoke] claude and codex are the replayer shims; no real CLI installed")


def check_versions() -> None:
    for argv, phase_id, want in (
        (["claude", "--version"], "implement", "2.0.74 (Claude Code)"),
        (["codex", "--version"], "review", "codex-cli 0.160.1"),
    ):
        done = run(argv, phase_id)
        if done.returncode != 0 or done.stdout.strip() != want:
            fail(
                f"{argv} printed {done.stdout.strip()!r} rc={done.returncode}, want {want!r}"
            )
    print("[smoke] --version reports each phase's recorded CLI version")


HOME = Path(os.environ.get("HOME", "/home/agent"))
SESSIONS: dict[str, str] = {}
"""Native session id per agent, as each phase's stream named it."""


def rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check_claude_transcript(stream: list[dict[str, object]]) -> None:
    session = str(stream[0].get("session_id"))
    path = HOME / ".claude" / "projects" / "-workspace" / f"{session}.jsonl"
    if not path.is_file():
        fail(f"no claude transcript at {path}")
    written = rows(path)
    if {r.get("sessionId") for r in written} != {session}:
        fail(f"claude transcript rows are not all session {session}")
    if set().union(*(models(r) for r in written)) != {MODEL}:
        fail("claude transcript reports a model other than the scripted one")
    last = written[-1].get("message")
    want = [{"type": "text", "text": stream[-1].get("result")}]
    if not isinstance(last, dict) or last.get("content") != want:
        fail("claude transcript does not end with the stream's result")
    SESSIONS["ClaudeCode"] = session
    print(f"[smoke] claude transcript {path.name}: {len(written)} rows")


def check_codex_transcript(stream: list[dict[str, object]]) -> None:
    session = str(stream[0].get("thread_id"))
    found = sorted((HOME / ".codex" / "sessions").rglob(f"rollout-*-{session}.jsonl"))
    if len(found) != 1:
        fail(f"want one codex rollout for {session}, found {found}")
    written = rows(found[0])
    header = written[0]
    payload = header.get("payload")
    if header.get("type") != "session_meta" or not isinstance(payload, dict):
        fail("codex rollout does not open with session_meta")
    if payload.get("id") != session or payload.get("session_id") != session:
        fail(f"codex session_meta does not name {session}: {payload}")
    SESSIONS["Codex"] = session
    print(f"[smoke] codex rollout {found[0].name}: {len(written)} rows")


def exporter(*args: str) -> str:
    env = {**os.environ, "EXPORTER_SPOOL_DIR": "/tmp/scripted-agent-smoke-spool"}
    done = subprocess.run(
        ["apss-session-exporter", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if done.returncode != 0:
        fail(f"apss-session-exporter {' '.join(args)} exited {done.returncode}")
    return done.stdout


def check_exporter() -> None:
    """The real exporter captures what the replayer wrote, as session-store would."""
    if not exporter("--version").startswith("apss-session-exporter 0.6.0 "):
        fail("apss-session-exporter is not 0.6.0")
    counts = json.loads(exporter("--spool-only"))
    if counts.get("discovered") != 2 or counts.get("stored") != 2:
        fail(f"exporter sweep found {counts}, want 2 discovered and stored")
    listed = json.loads(exporter("--spool-list", "0"))
    got = {e["agent"]: e["native_session_id"] for e in listed["entries"]}
    if got != SESSIONS:
        fail(f"exporter archived {got}, want {SESSIONS}")
    for entry in listed["entries"]:
        envelope = json.loads(exporter("--spool-read", str(entry["sequence"])))
        model = (envelope.get("metadata") or {}).get("model")
        if model != MODEL:
            fail(f"{entry['agent']} envelope records model {model!r}, want {MODEL}")
    print("[smoke] apss-session-exporter 0.6.0 archived both sessions, model scripted")


def check_claude_phase() -> None:
    argv = ["claude", "-p", "smoke", "--output-format", "stream-json"]
    stream = events(run(argv, "implement"), "claude")
    seen = set().union(*(models(e) for e in stream))
    if seen != {MODEL}:
        fail(f"claude stream reported models {sorted(seen)}, want only {MODEL}")
    last = stream[-1]
    result = last.get("result")
    if last.get("type") != "result" or not isinstance(result, str):
        fail(f"claude stream did not close with a result event: {last}")
    if task_result(result).get("success") is not True:
        fail(f"claude result is not the profile's TASK_RESULT: {result!r}")
    artifact = (OUTPUT / "implement.md").read_text(encoding="utf-8")
    if artifact != f"implement {EXECUTION} on loadtest/{EXECUTION}":
        fail(f"implement artifact is {artifact!r}")
    print(f"[smoke] claude replayed {len(stream)} events; artifact written")
    check_claude_transcript(stream)


def check_codex_phase() -> None:
    stream = events(run(["codex", "exec", "--json", "smoke"], "review"), "codex")
    seen = set().union(*(models(e) for e in stream))
    if seen != {MODEL}:
        fail(f"codex stream reported models {sorted(seen)}, want only {MODEL}")
    if stream[-1].get("type") != "turn.completed":
        fail(f"codex stream did not close with turn.completed: {stream[-1]}")
    verdicts = [
        task_result(s)
        for e in stream
        for s in strings(e)
        if s.startswith("TASK_RESULT:")
    ]
    if not any(v.get("review_verdict") == "certified" for v in verdicts):
        fail("codex stream carries no TASK_RESULT with review_verdict certified")
    artifact = (OUTPUT / "review.md").read_text(encoding="utf-8")
    if artifact != "review CERTIFIED":
        fail(f"review artifact is {artifact!r}")
    print(f"[smoke] codex replayed {len(stream)} events; verdict and artifact written")
    check_codex_transcript(stream)


def check_refuses_without_profile() -> None:
    done = run(["claude", "-p", "smoke"], None)
    if done.returncode != 78:
        fail(
            f"claude without a profile exited {done.returncode}, want 78: {done.stderr.strip()}"
        )
    print("[smoke] no profile refused with exit 78")


def main() -> None:
    check_shims()
    check_versions()
    check_claude_phase()
    check_codex_phase()
    check_exporter()
    check_refuses_without_profile()
    print("SCRIPTED AGENT SMOKE PASS")


if __name__ == "__main__":
    main()
