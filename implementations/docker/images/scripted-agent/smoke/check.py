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
    check_refuses_without_profile()
    print("SCRIPTED AGENT SMOKE PASS")


if __name__ == "__main__":
    main()
