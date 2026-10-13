"""A codex phase, replayed from a real ``codex exec --json`` capture and read back the
way syntropic137's ``CodexStreamProcessor`` reads it.

``fixtures/codex_stream.py`` is syntropic137's ``syn_shared/codex_stream.py`` @
``1017ec7b9c514afb1eceda0773c0a53031ff867a``, byte for byte. ``_observed`` applies the
processor's rules from the same commit: the first top-level ``model`` is the model
(``_process_line``), an ``item.started`` of a command or file change is a tool start
(``_handle_item_started``), and every ``agent_message`` text is fed to ``VerdictReader``
(``_handle_item_completed``), which is syntropic137's own ``VerdictReader`` from
``fixtures/consumer_verdict.py``, so the verdict the tests check is the one the
consumer settles, not a restatement of its grammar or its precedence.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from scripted_agent import cli
from scripted_agent.cli import replay
from scripted_agent.errors import RecordingRefused
from scripted_agent.profile import SCRIPTED_AGENT_MODEL
from scripted_agent.recording import CODEX_TOOL_ITEM_TYPES
from tests.fixtures.codex_stream import CodexItemType, CodexStreamType
from tests.fixtures.consumer_verdict import phase_verdict, read_verdict
from tests.test_replay import RECORDING, FakeGit, _env, _exit, _models, _profile, _stream

CODEX_RECORDING = RECORDING.parent / "v0.160.1_codex_exec_two-commands.jsonl"
"""Captured by ``fixtures/record_codex_exec.py`` from npm @openai/codex@0.160.1."""
VENDORED_CODEX_STREAM_SHA256 = "1039dac7048456f16b8b9b67053a82a802c9e20dc3c652fcedc2f049b5718da1"


def _codex_stream(**overrides: Any) -> dict[str, Any]:
    codex = {
        "harness": "codex",
        "recording": str(CODEX_RECORDING),
        "cli_version": "0.160.1",
        "tool_uses": 2,
    }
    return _stream(**{**codex, **overrides})


def _codex_profile(**stream: Any) -> dict[str, Any]:
    profile = _profile()
    for phase in profile["phases"].values():
        phase["stream"] = _codex_stream(**stream)
    return profile


def _replay_codex(
    tmp_path: Path, phase_id: str = "reverify", **stream: Any
) -> tuple[list[dict[str, Any]], list[float]]:
    slept: list[float] = []
    out = io.StringIO()
    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True, exist_ok=True)
    replay(_env(phase_id, _codex_profile(**stream)), tmp_path, FakeGit(), slept.append, out)
    return [json.loads(line) for line in out.getvalue().splitlines()], slept


def _observed(events: list[dict[str, Any]]) -> tuple[str | None, int, Any]:
    """The model, the tool-start count, and the ``AgentVerdict`` the processor's
    ``VerdictReader`` settles from every agent message."""
    model: str | None = None
    starts = 0
    said: list[str] = []
    for event in events:
        if model is None and isinstance(event.get("model"), str):
            model = event["model"]
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if event["type"] == CodexStreamType.ITEM_STARTED and item.get("type") in (
            CodexItemType.COMMAND_EXECUTION,
            CodexItemType.FILE_CHANGE,
        ):
            starts += 1
        if (
            event["type"] == CodexStreamType.ITEM_COMPLETED
            and item.get("type") == CodexItemType.AGENT_MESSAGE
        ):
            said.append(str(item.get("text", "")))
    return model, starts, read_verdict(*said)


def _is_the_profiles(verdict: Any) -> bool:
    """The reverify phase's profile verdict: SUCCESS, review certified."""
    return bool(
        verdict.status == phase_verdict.VerdictStatus.SUCCESS
        and verdict.reported_review_verdict == "certified"
    )


def test_the_vendored_consumer_vocabulary_is_upstreams() -> None:
    vendored = Path(__file__).parent / "fixtures" / "codex_stream.py"
    assert hashlib.sha256(vendored.read_bytes()).hexdigest() == VENDORED_CODEX_STREAM_SHA256
    assert {CodexItemType.COMMAND_EXECUTION, CodexItemType.FILE_CHANGE} == CODEX_TOOL_ITEM_TYPES


def test_the_capture_is_what_the_consumer_reads_before_replay() -> None:
    rows = [json.loads(line) for line in CODEX_RECORDING.read_text().splitlines()]
    assert rows[0]["_recording"]["cli_version"] == "0.160.1"
    model, starts, verdict = _observed(rows[1:])
    assert (model, starts) == (None, 2)
    assert verdict.status == phase_verdict.VerdictStatus.NOT_REPORTED


def test_a_codex_phase_is_observed_as_the_profile_declares(tmp_path: Path) -> None:
    events, _ = _replay_codex(tmp_path)

    model, starts, verdict = _observed(events)
    assert model == SCRIPTED_AGENT_MODEL
    assert starts == 2
    assert _is_the_profiles(verdict)
    assert [e["type"] for e in events[-2:]] == ["item.completed", "turn.completed"]
    assert sum(e["type"] == "turn.completed" for e in events) == 1
    assert {m for e in events for m in _models(e)} == {SCRIPTED_AGENT_MODEL}


RECORDED_FAILURE = 'TASK_RESULT: {"success": false, "comments": "tests failed"}\nTASK_RESULT_END'


def test_the_recorded_runs_own_verdict_never_reaches_the_consumer(tmp_path: Path) -> None:
    # The shipped capture with its final agent_message reporting failure: same version,
    # tool uses and terminal, so the replayer accepts it.
    rows = [json.loads(line) for line in CODEX_RECORDING.read_text().splitlines()]
    for row in rows:
        if row.get("item", {}).get("type") == "agent_message":
            row["item"]["text"] = f"Two commands ran.\n{RECORDED_FAILURE}"
    failed = tmp_path / "failed-run.jsonl"
    failed.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert _observed(rows[1:])[2].status == phase_verdict.VerdictStatus.FAILURE

    events, slept = _replay_codex(tmp_path, recording=str(failed), pacing_factor=1.0)

    _, starts, verdict = _observed(events)
    assert starts == 2
    assert _is_the_profiles(verdict)
    assert all("tests failed" not in json.dumps(e) for e in events)
    assert len(slept) == 8
    assert sum(slept) == pytest.approx(1.356)


def test_a_codex_phase_replays_at_recorded_timing(tmp_path: Path) -> None:
    # offsets 625, 755, 986, 990, 1136, 1143, 1251, then turn.completed at 1356 ms
    _, slept = _replay_codex(tmp_path, pacing_factor=1.0)

    assert slept == pytest.approx([0.625, 0.130, 0.231, 0.004, 0.146, 0.007, 0.108, 0.105])


@pytest.mark.parametrize("phase_id", ["implement", "reverify", "finalize_pr"])
def test_a_codex_phase_contacts_no_host(
    no_network: list[str], tmp_path: Path, phase_id: str
) -> None:
    _replay_codex(tmp_path, phase_id)
    assert no_network == []


@pytest.mark.parametrize(
    ("stream", "reason"),
    [
        ({"tool_uses": 3}, "has 2 tool_use events but the profile declares tool_uses 3"),
        ({"cli_version": "0.156.1"}, "captured from CLI '0.160.1' but the profile declares"),
        ({"recording": str(RECORDING), "cli_version": "2.0.74"}, "exactly one 'turn.completed'"),
    ],
)
def test_refuses_a_codex_recording_that_is_not_the_declared_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stream: dict[str, Any],
    reason: str,
) -> None:
    code, err = _exit(monkeypatch, capsys, _env("implement", _codex_profile(**stream)))

    assert code == RecordingRefused.exit_code == 65
    assert reason in err


def test_version_reports_the_codex_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scripted_agent.cli.os.environ", _env("implement", _codex_profile()))
    monkeypatch.setattr("sys.argv", ["codex", "--version"])
    cli.main()
    assert capsys.readouterr().out == "codex-cli 0.160.1\n"
