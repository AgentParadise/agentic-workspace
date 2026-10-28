"""A replayed phase leaves the native transcript the real harness would.

Read back with ``agentic_isolation``'s own harness readers (transcript sources,
native evidence, conversation preview), so the file is checked against the
formats the platform already harvests, not against a restatement of them.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agentic_isolation.harnesses.claude.conversation import ClaudeConversationReader
from agentic_isolation.harnesses.claude.evidence import ClaudeNativeEvidenceReader
from agentic_isolation.harnesses.claude.transcripts import ClaudeTranscriptSource
from agentic_isolation.harnesses.codex.conversation import CodexConversationReader
from agentic_isolation.harnesses.codex.evidence import CodexNativeEvidenceReader
from agentic_isolation.harnesses.codex.transcripts import CodexTranscriptSource
from agentic_isolation.providers.base import ExecuteResult

from scripted_agent import cli
from scripted_agent.cli import replay
from scripted_agent.errors import RecordingRefused, SideEffectFailed, TranscriptFailed
from scripted_agent.profile import SCRIPTED_AGENT_MODEL, ScriptedStream
from scripted_agent.recording import Recording
from scripted_agent.transcript import Clock, claude_transcript_path
from tests.test_codex import CODEX_RECORDING, _codex_profile, _codex_stream
from tests.test_replay import FIXTURE, RECORDING, FakeGit, _env, _models, _phase, _stream

CWD = Path("/workspace")
AT = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def _run(
    environ: dict[str, str], tmp_path: Path, session: str
) -> tuple[list[dict[str, Any]], Path]:
    out = io.StringIO()
    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True, exist_ok=True)
    path = replay(
        environ,
        tmp_path,
        FakeGit(),
        lambda _: None,
        out,
        cwd=CWD,
        new_session_id=lambda: session,
        clock=lambda: AT,
    )
    return [json.loads(line) for line in out.getvalue().splitlines()], path


def _run_clocked(environ: dict[str, str], tmp_path: Path, clock: Clock) -> tuple[None, Path]:
    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True, exist_ok=True)
    path = replay(environ, tmp_path, FakeGit(), lambda _: None, io.StringIO(), cwd=CWD, clock=clock)
    return None, path


def _verify_profile() -> dict[str, Any]:
    return {
        "tier": "platform",
        "fixture_repo": FIXTURE,
        "phases": {"verify": _phase("verify_remote_branch", "Checked {branch}.")},
    }


def _exec_fn(home: Path) -> Any:
    """The workspace's ExecFn, run locally: what the platform harvests through."""

    async def exec_fn(
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecuteResult:
        done = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            check=False,
        )
        return ExecuteResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    return exec_fn


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- claude ------------------------------------------------------------------


def test_a_replayed_claude_phase_leaves_a_native_transcript(
    tmp_path: Path, isolated_home: Path
) -> None:
    stream, path = _run(_env("review"), tmp_path, "11111111-2222-4333-8444-555555555555")

    assert path == isolated_home / ".claude/projects/-workspace" / (
        "11111111-2222-4333-8444-555555555555.jsonl"
    )
    harvested = asyncio.run(ClaudeTranscriptSource(_exec_fn(isolated_home)).extract())
    assert harvested.errors == []
    assert [(t.session_id, t.source_path) for t in harvested.transcripts] == [
        ("11111111-2222-4333-8444-555555555555", str(path))
    ]

    content = path.read_bytes()
    evidence = ClaudeNativeEvidenceReader().extract(content)
    assert evidence.native_id == "11111111-2222-4333-8444-555555555555"
    assert evidence.issues == ()

    # The rows carry the replayed stream's messages, in order, then the verdict.
    rows = _rows(path)
    replayed = [e["message"] for e in stream if e["type"] in ("user", "assistant")]
    assert [r["message"] for r in rows[:-1]] == replayed
    assert rows[-1]["message"]["content"] == [{"type": "text", "text": stream[-1]["result"]}]
    assert [r["parentUuid"] for r in rows[1:]] == [r["uuid"] for r in rows[:-1]]
    assert {r["version"] for r in rows} == {"2.0.74"}
    assert set(_models(rows)) == {SCRIPTED_AGENT_MODEL}

    preview = ClaudeConversationReader().conversation(content)
    assert preview.messages[-1].role == "assistant"
    assert preview.messages[-1].text == stream[-1]["result"]


def test_the_transcript_and_stdout_share_one_fresh_session_id(tmp_path: Path) -> None:
    recorded = Recording.load(cli.load_profile(_env("review")).phases["review"].stream)
    stream, path = _run(_env("review"), tmp_path, "fresh-session")

    ids = {e["session_id"] for e in stream if "session_id" in e}
    assert ids == {"fresh-session"}
    assert recorded.session_id not in json.dumps(stream)
    assert {r["sessionId"] for r in _rows(path)} == {"fresh-session"}


def test_two_replays_of_one_recording_are_two_sessions(tmp_path: Path, isolated_home: Path) -> None:
    out = io.StringIO()
    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True)
    first = replay(_env("review"), tmp_path, FakeGit(), lambda _: None, out, cwd=CWD)
    second = replay(_env("review"), tmp_path, FakeGit(), lambda _: None, out, cwd=CWD)
    assert first != second
    assert first.parent == second.parent
    assert len(list(first.parent.iterdir())) == 2


def test_claude_config_dir_moves_the_transcript_root(tmp_path: Path, isolated_home: Path) -> None:
    environ = {**_env("review"), "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
    _, path = _run(environ, tmp_path, "s1")
    assert path == tmp_path / "cfg/projects/-workspace/s1.jsonl"
    assert not (isolated_home / ".claude").exists()


def test_the_transcript_lands_in_the_session_store_spool(
    tmp_path: Path, isolated_home: Path
) -> None:
    """session-store init links ~/.claude/projects into the spool partition."""
    partition = tmp_path / "spool" / "ws-1" / "claude"
    partition.mkdir(parents=True)
    (isolated_home / ".claude").mkdir()
    (isolated_home / ".claude" / "projects").symlink_to(partition)

    _run(_env("review"), tmp_path, "s1")

    assert [p.relative_to(partition) for p in partition.rglob("*.jsonl")] == [
        Path("-workspace/s1.jsonl")
    ]


def test_the_project_directory_is_claude_codes_cwd_slug() -> None:
    assert claude_transcript_path({"HOME": "/h"}, Path("/a/b.c_d"), "s") == Path(
        "/h/.claude/projects/-a-b-c-d/s.jsonl"
    )


# --- codex -------------------------------------------------------------------


def test_a_replayed_codex_phase_leaves_a_native_rollout(
    tmp_path: Path, isolated_home: Path
) -> None:
    session = "01a13602-e877-7301-9f67-000000000001"
    stream, path = _run(_env("reverify", _codex_profile()), tmp_path, session)

    assert path == isolated_home / ".codex/sessions/2026/10/27" / (
        f"rollout-2026-10-27T12-00-00-{session}.jsonl"
    )
    harvested = asyncio.run(CodexTranscriptSource(_exec_fn(isolated_home)).extract())
    assert harvested.errors == []
    assert [t.session_id for t in harvested.transcripts] == [session]

    content = path.read_bytes()
    evidence = CodexNativeEvidenceReader().extract(content)
    assert evidence.native_id == session
    assert stream[0] == {**stream[0], "type": "thread.started", "thread_id": session}

    rows = _rows(path)
    assert rows[0]["type"] == "session_meta"
    assert rows[0]["payload"]["cli_version"] == "0.160.1"
    assert [r["ordinal"] for r in rows] == list(range(len(rows)))
    assert set(_models(rows)) == {SCRIPTED_AGENT_MODEL}
    items = [r["payload"]["item"] for r in rows if r["payload"].get("type") == "item_completed"]
    assert [i["type"] for i in items] == [
        "CommandExecution",
        "CommandExecution",
        "AgentMessage",
        "AgentMessage",
    ]
    assert items[0]["command"] == ["/bin/bash", "-lc", "printf one"]

    verdict = next(
        e["item"]["text"]
        for e in reversed(stream)
        if e.get("type") == "item.completed" and e["item"].get("type") == "agent_message"
    )
    assert rows[-1]["payload"] == {**rows[-1]["payload"], "type": "task_complete"}
    assert rows[-1]["payload"]["last_agent_message"] == verdict
    assert rows[0]["payload"]["model_provider"] == SCRIPTED_AGENT_MODEL
    said = [r["payload"]["content"][0]["text"] for r in rows if r["type"] == "response_item"]
    assert said == ["FINAL_ANSWER", verdict]
    preview = CodexConversationReader().conversation(content)
    assert preview.messages[-1].role == "assistant"
    assert preview.messages[-1].text == verdict


def test_codex_home_moves_the_rollout_root(tmp_path: Path, isolated_home: Path) -> None:
    environ = {**_env("reverify", _codex_profile()), "CODEX_HOME": str(tmp_path / "ch")}
    _, path = _run(environ, tmp_path, "s1")
    assert path.is_relative_to(tmp_path / "ch/sessions/2026/10/27")


# --- failure -----------------------------------------------------------------


def test_an_unwritable_transcript_root_fails_the_phase(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    isolated_home: Path,
) -> None:
    """Opened before the first event, so a phase with nowhere to write its
    transcript prints nothing and exits 74: it never looks like a clean run."""
    (isolated_home / ".claude").write_text("not a directory")
    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True)
    monkeypatch.setattr(cli, "WORKSPACE", tmp_path)
    monkeypatch.setattr(cli, "_subprocess", FakeGit())
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.os, "environ", _env("review"))
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == TranscriptFailed.exit_code == 74
    captured = capsys.readouterr()
    assert "claude transcript" in captured.err
    assert captured.out == "", "nothing is printed for a session that cannot be recorded"


def test_no_home_is_refused_not_resolved_from_the_password_database(tmp_path: Path) -> None:
    environ = _env("review")
    del environ["HOME"]
    with pytest.raises(TranscriptFailed, match="HOME is not set"):
        _run(environ, tmp_path, "s1")


def test_a_phase_that_fails_part_way_leaves_its_partial_session(
    tmp_path: Path, isolated_home: Path
) -> None:
    """As a harness that died mid-run: what was printed is on disk, the
    verdict is not, and the replay fails with the side effect's error."""
    out = io.StringIO()
    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True)
    with pytest.raises(SideEffectFailed):
        replay(
            _env("verify", _verify_profile()),
            tmp_path,
            FakeGit(branch_exists=False),
            lambda _: None,
            out,
            cwd=CWD,
            new_session_id=lambda: "s1",
        )
    printed = [json.loads(line) for line in out.getvalue().splitlines()]
    assert printed[-1]["type"] != "result"
    rows = _rows(isolated_home / ".claude/projects/-workspace/s1.jsonl")
    assert [r["message"] for r in rows] == [
        e["message"] for e in printed if e["type"] in ("user", "assistant")
    ]


def test_the_closing_events_are_recorded_before_they_are_printed(
    tmp_path: Path, isolated_home: Path
) -> None:
    seen_on_disk: list[int] = []

    class Out(io.StringIO):
        def write(self, s: str) -> int:
            if '"type": "result"' in s:
                path = isolated_home / ".claude/projects/-workspace/s1.jsonl"
                seen_on_disk.append(len(_rows(path)))
            return super().write(s)

    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True)
    path = replay(
        _env("review"),
        tmp_path,
        FakeGit(),
        lambda _: None,
        Out(),
        cwd=CWD,
        new_session_id=lambda: "s1",
    )
    assert seen_on_disk == [len(_rows(path))]


def test_rows_are_stamped_when_they_are_printed(tmp_path: Path) -> None:
    ticks = iter(datetime(2026, 10, 27, 12, 0, s, tzinfo=UTC) for s in range(60))
    _, path = _run_clocked(_env("reverify", _codex_profile()), tmp_path, lambda: next(ticks))
    rows = _rows(path)
    stamps = [r["timestamp"] for r in rows]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert rows[-1]["payload"]["duration_ms"] > 0


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("defect", ["missing", "conflicting"])
def test_a_recording_must_name_exactly_one_session(
    tmp_path: Path, harness: str, defect: str
) -> None:
    source = CODEX_RECORDING if harness == "codex" else RECORDING
    key = "thread_id" if harness == "codex" else "session_id"
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    if defect == "missing":
        for row in rows[1:]:
            row.pop(key, None)
    else:
        rows[-1][key] = "another-session"  # the terminal event names a second session
    edited = tmp_path / "edited.jsonl"
    edited.write_text("".join(json.dumps(r) + "\n" for r in rows))
    stream = _codex_stream if harness == "codex" else _stream
    with pytest.raises(RecordingRefused, match="session|thread"):
        Recording.load(ScriptedStream.model_validate(stream(recording=str(edited))))
