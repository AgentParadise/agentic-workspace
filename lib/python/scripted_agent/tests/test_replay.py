"""The replayer, driven end to end the way a load-test workspace runs it."""

from __future__ import annotations

import io
import json
import os
import socket
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from scripted_agent import cli
from scripted_agent.cli import PROFILE_PATH_ENV, TAGS_ENV, replay
from scripted_agent.errors import RecordingRefused, SideEffectFailed
from scripted_agent.profile import SCRIPTED_AGENT_MODEL, SCRIPTED_AGENT_PROFILE_ENV
from tests.fixtures.consumer_verdict import claude_verdict, phase_verdict, read_verdict

REPO = Path(__file__).resolve().parents[4]
RECORDING = (
    REPO
    / "implementations/docker/images/claude-cli/fixtures/recordings"
    / "v2.0.74_claude-sonnet-4-5_multi-tool.jsonl"
)
"""Captured from CLI 2.0.74; two tool uses; models claude-sonnet-4-5 and claude-haiku-4-5."""

FIXTURE = "example-org/loadtest-fixture"
HEAD = "3b7f2bd49a4609f24a516bb4617aadbc1edf751e"
PR_URL = f"https://github.com/{FIXTURE}/pull/4217"
BASE_URL = "http://model-sink.invalid:10000"


def _stream(**overrides: Any) -> dict[str, Any]:
    return {
        "harness": "claude",
        "recording": str(RECORDING),
        "cli_version": "2.0.74",
        "tool_uses": 2,
        "pacing_factor": 0.5,
        **overrides,
    }


def _phase(side_effect: str, artifact: str, **overrides: Any) -> dict[str, Any]:
    return {
        "stream": _stream(),
        "workload": {"kind": "none"},
        "side_effect": {"kind": side_effect},
        "artifact": artifact,
        **overrides,
    }


def _profile(**phase_overrides: Any) -> dict[str, Any]:
    return {
        "tier": "node",
        "fixture_repo": FIXTURE,
        "phases": {
            "implement": _phase(
                "open_draft_pull_request",
                "Pushed {branch} at {head_sha}, PR {pull_request}.",
                **phase_overrides,
            ),
            "review": _phase("report_only", "Verdict {review_verdict}.", review_verdict="blocked"),
            "fix": _phase("push_branch", "Fixed on {branch}."),
            "reverify": _phase(
                "report_only", "Verdict {review_verdict}.", review_verdict="certified"
            ),
            "fix_2": _phase("push_branch", "Never runs."),
            "finalize_pr": _phase(
                "mark_pull_request_ready",
                "{outcome} after {repair_rounds} repair round(s); {pull_request}.",
            ),
        },
    }


class FakeGit:
    """Git and gh as the fixture repository answers them; records every call."""

    def __init__(self, *, branch_exists: bool = True) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.branch_exists = branch_exists

    def __call__(self, argv: Sequence[str], cwd: Path | None) -> str:
        self.calls.append((list(argv), cwd))
        match list(argv):
            case ["git", "ls-remote", *_]:
                return f"{HEAD}\trefs/heads/x\n" if self.branch_exists else ""
            case ["git", "push", *_]:
                self.branch_exists = True
            case ["gh", "pr", "create", *_]:
                return f"Creating draft pull request\n{PR_URL}\n"
            case ["gh", "pr", "view", *_]:
                return f"{PR_URL}\n"
        return ""

    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


def _env(phase_id: str, profile: dict[str, Any] | None = None) -> dict[str, str]:
    return {
        SCRIPTED_AGENT_PROFILE_ENV: json.dumps(profile or _profile()),
        TAGS_ENV: f"execution_id:exec-1a2b,workspace_id:ws-1,phase_id:{phase_id}",
        "ANTHROPIC_BASE_URL": BASE_URL,
        # The isolated HOME tests/conftest.py sets: every replay writes a transcript there.
        "HOME": os.environ["HOME"],
    }


def _replay(
    environ: dict[str, str], tmp_path: Path, git: FakeGit | None = None
) -> tuple[list[dict[str, Any]], list[float], FakeGit]:
    git = git or FakeGit()
    slept: list[float] = []
    out = io.StringIO()
    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True, exist_ok=True)
    replay(environ, tmp_path, git, slept.append, out)
    return [json.loads(line) for line in out.getvalue().splitlines()], slept, git


def _models(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "model" and isinstance(v, str):
                yield v
            if k == "modelUsage" and isinstance(v, dict):
                yield from v
            yield from _models(v)
    elif isinstance(value, list):
        for v in value:
            yield from _models(v)


# --- zero tokens -------------------------------------------------------------


def test_the_network_guard_trips_on_a_model_provider(no_network: list[str]) -> None:
    with pytest.raises(AssertionError, match=r"api\.anthropic\.com"):
        socket.create_connection(("api.anthropic.com", 443))


@pytest.mark.parametrize("phase_id", ["implement", "review", "finalize_pr"])
def test_a_full_phase_contacts_no_host(
    no_network: list[str], tmp_path: Path, phase_id: str
) -> None:
    _replay(_env(phase_id), tmp_path)
    assert no_network == []


def test_every_replayed_model_is_the_scripted_one(tmp_path: Path) -> None:
    events, _, _ = _replay(_env("review"), tmp_path)

    models = [m for event in events for m in _models(event)]
    assert len(events) == 8
    # the recording reports claude-sonnet-4-5 in five places and two modelUsage keys
    assert len(models) >= 6
    assert set(models) == {SCRIPTED_AGENT_MODEL}


@pytest.mark.parametrize(
    ("factor", "expected"),
    [
        # offsets 0 x6, 2837 ms, then the held-back result at 2926 ms (89 ms later)
        (0.5, [0, 0, 0, 0, 0, 0, 1.4185, 0.0445]),
        (0.0, [0, 0, 0, 0, 0, 0, 0, 0]),
    ],
)
def test_replays_at_recorded_timing_times_the_pacing_factor(
    tmp_path: Path, factor: float, expected: list[float]
) -> None:
    _, slept, _ = _replay(_env("review", _profile_paced(factor)), tmp_path)

    assert slept == pytest.approx(expected)


def test_the_verdict_waits_for_the_recorded_terminal_delay(tmp_path: Path) -> None:
    """Every sleep comes before the verdict, the terminal one included."""
    out = io.StringIO()
    timeline: list[str] = []

    def sleep(seconds: float) -> None:
        timeline.append(f"sleep {seconds:.4f}")

    def emit_spy(text: str) -> int:
        timeline.extend(json.loads(line)["type"] for line in text.splitlines())
        return len(text)

    out.write = emit_spy  # type: ignore[method-assign]
    (tmp_path / "repos" / "loadtest-fixture").mkdir(parents=True)
    replay(_env("review"), tmp_path, FakeGit(), sleep, out)

    assert timeline[-2:] == ["sleep 0.0445", "result"]


def _profile_paced(factor: float) -> dict[str, Any]:
    profile = _profile()
    profile["phases"]["review"]["stream"]["pacing_factor"] = factor
    return profile


# --- the phase's work ---------------------------------------------------------


def test_last_message_is_the_profiles_task_result_not_the_recordings(tmp_path: Path) -> None:
    events, _, _ = _replay(_env("reverify"), tmp_path)

    assert events[-1]["type"] == "result"
    assert events[-1]["result"] == (
        'TASK_RESULT: {"success":true,"side_effects":"none",'
        '"comments":"Load-test stub for phase reverify; no agent ran.",'
        '"review_verdict":"certified"}\nTASK_RESULT_END'
    )
    assert all("result" not in e or e["type"] != "result" for e in events[:-1])


RECORDED_SUCCESS = 'TASK_RESULT: {"success": true, "comments": "fine"}\nTASK_RESULT_END'
RECORDED_FAILURE = 'TASK_RESULT: {"success": false, "comments": "tests failed"}\nTASK_RESULT_END'


def _recorded(tmp_path: Path, edit: Any) -> dict[str, Any]:
    """The reverify profile replaying the shipped recording with ``edit`` applied to its
    rows; the edits keep its version, tool uses and terminal, so it is accepted."""
    rows = [json.loads(line) for line in RECORDING.read_text().splitlines()]
    edit(rows)
    edited = tmp_path / "edited-run.jsonl"
    edited.write_text("".join(json.dumps(row) + "\n" for row in rows))
    profile = _profile()
    profile["phases"]["reverify"]["stream"]["recording"] = str(edited)
    return profile


def _is_the_profiles(verdict: Any) -> bool:
    """The reverify phase's profile verdict: SUCCESS, review certified."""
    return bool(
        verdict.status == phase_verdict.VerdictStatus.SUCCESS
        and verdict.reported_review_verdict == "certified"
    )


def test_the_consumers_verdict_rules_are_syntropic137s() -> None:
    # The oracle every verdict test below runs, held to the rules it was vendored for.
    status = phase_verdict.VerdictStatus
    assert read_verdict('TASK_RESULT: {"success": true}').status == status.UNREADABLE
    assert read_verdict(RECORDED_SUCCESS).status == status.SUCCESS
    assert read_verdict(f"{RECORDED_SUCCESS}\n{RECORDED_FAILURE}").status == status.FAILURE
    assert read_verdict(RECORDED_FAILURE, RECORDED_SUCCESS).status == status.FAILURE
    assert read_verdict("done").status == status.NOT_REPORTED


def test_the_recorded_runs_own_verdict_never_reaches_the_consumer(tmp_path: Path) -> None:
    # EventStreamProcessor feeds every assistant text block to VerdictReader, which ranks
    # FAILURE over SUCCESS whatever the order; only the profile's report may carry one.
    # Each text block reports: a closed success first, so a replay that withholds only
    # the first report leaves the failure after it for the consumer to read.
    def fail(rows: list[dict[str, Any]]) -> None:
        said = [b for r in rows if r.get("type") == "assistant" for b in r["message"]["content"]]
        texts = [b for b in said if b["type"] == "text"]
        assert len(texts) >= 2
        texts[0]["text"] += f"\n{RECORDED_SUCCESS}"
        for block in texts[1:]:
            block["text"] += f"\n{RECORDED_FAILURE}"
        assert claude_verdict(rows[1:]).status == phase_verdict.VerdictStatus.FAILURE

    events, _, _ = _replay(_env("reverify", _recorded(tmp_path, fail)), tmp_path)

    assert _is_the_profiles(claude_verdict(events))
    assert all("tests failed" not in json.dumps(e) for e in events)


def test_a_recorded_harness_error_does_not_swallow_the_profiles_verdict(
    tmp_path: Path,
) -> None:
    # The consumer reads an is_error result as the harness's failure text and never
    # hands it to VerdictReader; the profile's closing result is not the recording's.
    def harness_error(rows: list[dict[str, Any]]) -> None:
        rows[-1].update(is_error=True, subtype="error_during_execution", result="API Error")

    events, _, _ = _replay(_env("reverify", _recorded(tmp_path, harness_error)), tmp_path)

    assert _is_the_profiles(claude_verdict(events))
    assert (events[-1]["is_error"], events[-1]["subtype"]) == (False, "success")


def test_open_draft_pushes_the_execution_branch_and_opens_the_pr(tmp_path: Path) -> None:
    _, _, git = _replay(_env("implement"), tmp_path, FakeGit(branch_exists=False))

    repo = tmp_path / "repos" / "loadtest-fixture"
    assert (repo / "loadtest" / "implement.txt").read_text() == "exec-1a2b implement\n"
    argvs = git.argvs()
    assert ["git", "checkout", "-B", "loadtest/exec-1a2b"] in argvs
    assert ["git", "push", "origin", "HEAD:refs/heads/loadtest/exec-1a2b"] in argvs
    create = next(a for a in argvs if a[:3] == ["gh", "pr", "create"])
    assert "--draft" in create and FIXTURE in create
    assert all(
        cwd == repo for argv, cwd in git.calls if argv[0] == "git" and argv[1] != "ls-remote"
    )


def test_artifact_is_rendered_from_the_side_effects_results(tmp_path: Path) -> None:
    _replay(_env("implement"), tmp_path)

    artifact = (tmp_path / "artifacts" / "output" / "implement.md").read_text()
    assert artifact == f"Pushed loadtest/exec-1a2b at {HEAD}, PR #4217 {PR_URL}."


def test_finalize_marks_ready_and_reports_the_planned_run(tmp_path: Path) -> None:
    _, _, git = _replay(_env("finalize_pr"), tmp_path)

    assert ["gh", "pr", "ready", "loadtest/exec-1a2b", "--repo", FIXTURE] in git.argvs()
    artifact = (tmp_path / "artifacts" / "output" / "finalize_pr.md").read_text()
    # implement, review, fix, reverify(certified) -> finalize_pr: fix_2 is skipped,
    # so two pushing phases ran and that is one repair round.
    assert artifact == f"READY after 1 repair round(s); #4217 {PR_URL}."


def test_push_branch_builds_on_the_branch_an_earlier_phase_pushed(tmp_path: Path) -> None:
    _, _, git = _replay(_env("fix"), tmp_path)

    argvs = git.argvs()
    assert ["git", "fetch", "origin", "loadtest/exec-1a2b"] in argvs
    assert ["git", "checkout", "-B", "loadtest/exec-1a2b", "FETCH_HEAD"] in argvs
    assert not any(a[:2] == ["gh", "pr"] for a in argvs)


def test_report_only_leaves_the_tree_alone(tmp_path: Path) -> None:
    _, _, git = _replay(_env("review"), tmp_path)

    assert git.argvs() == []
    assert not (tmp_path / "repos" / "loadtest-fixture" / "loadtest").exists()


def test_gates_workload_runs_the_repositorys_gates(tmp_path: Path) -> None:
    profile = _profile(workload={"kind": "gates"})
    _, _, git = _replay(_env("implement", profile), tmp_path)

    assert (["just", "preflight-agent"], tmp_path / "repos" / "loadtest-fixture") in git.calls


_SYNTHETIC = {"kind": "synthetic", "cpu_seconds": 2.5, "mem_bytes": 4096, "disk_bytes": 3 << 20}


class _Held:
    """A spy allocation: knows its size and whether it has been released."""

    def __init__(self, size: int, log: list[str]) -> None:
        self.size, self.log = size, log
        log.append(f"alloc {size}")

    def __del__(self) -> None:
        self.log.append("free")


def _run_synthetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[float], Path]:
    """The phase's synthetic workload under a fake clock, allocator and scratch file."""
    log: list[str] = []
    ticks: list[float] = []
    scratch = tmp_path / "scratch.bin"

    def clock() -> float:
        ticks.append(len(ticks) * 0.5)
        log.append("tick")
        return ticks[-1]

    def synthetic(workload: Any) -> None:
        real_synthetic(
            workload,
            clock=clock,
            allocate=lambda n: _Held(n, log),
            scratch=lambda: scratch.open("w+b"),
        )

    real_synthetic = cli._synthetic
    monkeypatch.setattr("scripted_agent.cli._synthetic", synthetic)
    _replay(_env("implement", _profile(workload=_SYNTHETIC)), tmp_path)
    return log, ticks, scratch


def test_synthetic_workload_burns_the_configured_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ticks, _ = _run_synthetic(tmp_path, monkeypatch)

    # the clock is read until cpu_seconds have passed on it, and no longer
    assert ticks[-1] - ticks[0] >= _SYNTHETIC["cpu_seconds"]
    assert ticks[-2] - ticks[0] < _SYNTHETIC["cpu_seconds"]


def test_synthetic_workload_holds_its_memory_through_the_cpu_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, _, _ = _run_synthetic(tmp_path, monkeypatch)

    assert log[0] == f"alloc {_SYNTHETIC['mem_bytes']}"
    assert log.count("tick") > 1
    assert log[-1] == "free"
    assert "free" not in log[:-1]


def test_synthetic_workload_writes_and_flushes_its_disk_bytes_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, scratch = _run_synthetic(tmp_path, monkeypatch)

    assert scratch.stat().st_size == _SYNTHETIC["disk_bytes"]
    repo = tmp_path / "repos" / "loadtest-fixture"
    # the only file in the fixture tree is the side effect's marker
    assert [p.relative_to(repo) for p in repo.rglob("*") if p.is_file()] == [
        Path("loadtest/implement.txt")
    ]


def test_synthetic_workload_really_runs(tmp_path: Path) -> None:
    workload = {"kind": "synthetic", "cpu_seconds": 0.01, "mem_bytes": 1024, "disk_bytes": 1024}
    _replay(_env("implement", _profile(workload=workload)), tmp_path)


def test_platform_verify_refuses_an_absent_branch(tmp_path: Path) -> None:
    profile = {
        "tier": "platform",
        "fixture_repo": FIXTURE,
        "phases": {"verify": _phase("verify_remote_branch", "Checked {branch}.")},
    }
    with pytest.raises(SideEffectFailed, match="loadtest/exec-1a2b is not on"):
        _replay(_env("verify", profile), tmp_path, FakeGit(branch_exists=False))


# --- refusals: exit non-zero with the reason ----------------------------------


@pytest.fixture(autouse=True)
def _never_the_real_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``main()`` writes under the workspace root; never let a test reach /workspace."""
    monkeypatch.setattr("scripted_agent.cli.WORKSPACE", tmp_path / "main-workspace")


def _exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], environ: dict[str, str]
) -> tuple[int, str]:
    monkeypatch.setattr("scripted_agent.cli.os.environ", environ)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert isinstance(stopped.value.code, int)
    captured = capsys.readouterr()
    assert captured.out == "", "a refusal replays nothing"
    return stopped.value.code, captured.err


def test_refuses_without_a_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = _exit(monkeypatch, capsys, {TAGS_ENV: "execution_id:e,phase_id:implement"})

    assert code == 78
    assert SCRIPTED_AGENT_PROFILE_ENV in err and PROFILE_PATH_ENV in err


def test_refuses_an_invalid_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = _profile()
    profile["fixture_repo"] = "syntropic137/syntropic137"
    code, err = _exit(monkeypatch, capsys, _env("implement", profile))

    assert code == 78
    assert "not a valid ScriptedAgentProfile" in err
    assert "cannot be the load-test fixture repository" in err


def test_refuses_without_phase_and_execution_tags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _env("implement")
    environ[TAGS_ENV] = "execution_id:exec-1a2b"
    code, err = _exit(monkeypatch, capsys, environ)

    assert code == 78
    assert "phase_id: and execution_id:" in err


def test_refuses_a_phase_the_profile_does_not_have(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = _exit(monkeypatch, capsys, _env("research"))

    assert code == 78
    assert "phase 'research' is not in the profile" in err


@pytest.mark.parametrize(
    ("stream", "reason"),
    [
        ({"tool_uses": 3}, "has 2 tool_use events but the profile declares tool_uses 3"),
        ({"cli_version": "2.1.29"}, "captured from CLI '2.0.74' but the profile declares"),
        ({"recording": "/nonexistent.jsonl"}, "cannot be read"),
    ],
)
def test_refuses_a_recording_that_is_not_the_declared_stream(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stream: dict[str, Any],
    reason: str,
) -> None:
    code, err = _exit(monkeypatch, capsys, _env("implement", _profile(stream=_stream(**stream))))

    assert code == RecordingRefused.exit_code == 65
    assert reason in err


def _capture(tmp_path: Path, rows: list[Any], *, raw: bytes | None = None) -> str:
    path = tmp_path / "capture.jsonl"
    if raw is None:
        path.write_text("\n".join(json.dumps(row) for row in rows))
    else:
        path.write_bytes(raw)
    return str(path)


def _rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in RECORDING.read_text().splitlines()]


def _terminal_duplicated() -> list[dict[str, Any]]:
    rows = _rows()
    return [*rows, dict(rows[-1], result="TASK_RESULT: {}\nTASK_RESULT_END")]


def _terminal_reordered() -> list[dict[str, Any]]:
    rows = _rows()
    return [*rows[:-2], rows[-1], rows[-2]]


@pytest.mark.parametrize(
    ("rows", "raw", "reason"),
    [
        ([[]], None, "must open with a"),
        ([None], None, "must open with a"),
        ([{"_recording": []}], None, "must open with a"),
        ([{"_recording": {"cli_version": "2.0.74"}}, []], None, "line 2 is not a JSON object"),
        ([], b"\xff\n", "is not UTF-8"),
        ([], b"{not json\n", "is not JSONL"),
        (_terminal_duplicated(), None, "exactly one 'result' event; found 2"),
        (_terminal_reordered(), None, "exactly one 'result' event; found 1 at event(s) [7] of 8"),
        (_rows()[:-1], None, "exactly one 'result' event; found 0"),
    ],
    ids=[
        "array-header",
        "null-header",
        "non-object-metadata",
        "non-object-event",
        "invalid-utf8",
        "not-json",
        "duplicate-result",
        "result-before-last",
        "no-result",
    ],
)
def test_refuses_a_malformed_recording_before_replaying_anything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rows: list[Any],
    raw: bytes | None,
    reason: str,
) -> None:
    stream = _stream(recording=_capture(tmp_path, rows, raw=raw))
    code, err = _exit(monkeypatch, capsys, _env("implement", _profile(stream=stream)))

    assert code == RecordingRefused.exit_code
    assert reason in err
    assert "Traceback" not in err


def test_a_refused_recording_replays_nothing(tmp_path: Path) -> None:
    out = io.StringIO()
    with pytest.raises(RecordingRefused):
        replay(
            _env("implement", _profile(stream=_stream(tool_uses=1))),
            tmp_path,
            FakeGit(),
            lambda _: None,
            out,
        )
    assert out.getvalue() == ""


# --- where the profile comes from ---------------------------------------------


def test_reads_the_profile_from_the_mounted_file(tmp_path: Path) -> None:
    path = tmp_path / "loadtest-profile.json"
    path.write_text(json.dumps(_profile()))
    environ = _env("review")
    del environ[SCRIPTED_AGENT_PROFILE_ENV]
    environ[PROFILE_PATH_ENV] = str(path)

    events, _, _ = _replay(environ, tmp_path)
    assert "phase review" in events[-1]["result"]


def test_the_env_var_wins_over_the_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "loadtest-profile.json"
    path.write_text(json.dumps(_profile()))
    environ = _env("review")
    environ[SCRIPTED_AGENT_PROFILE_ENV] = "{}"
    environ[PROFILE_PATH_ENV] = str(path)

    code, err = _exit(monkeypatch, capsys, environ)
    assert code == 78
    assert f"profile from {SCRIPTED_AGENT_PROFILE_ENV} is not a valid" in err


def test_refuses_a_profile_file_that_is_not_utf8(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "loadtest-profile.json"
    path.write_bytes(b"\xff")
    environ = _env("review")
    del environ[SCRIPTED_AGENT_PROFILE_ENV]
    environ[PROFILE_PATH_ENV] = str(path)

    code, err = _exit(monkeypatch, capsys, environ)
    assert code == 78
    assert f"{PROFILE_PATH_ENV}={path} is not UTF-8" in err


def test_refuses_an_unreadable_profile_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    environ = _env("review")
    del environ[SCRIPTED_AGENT_PROFILE_ENV]
    environ[PROFILE_PATH_ENV] = str(tmp_path / "absent.json")

    code, err = _exit(monkeypatch, capsys, environ)
    assert code == 78
    assert "cannot be read" in err


def test_version_reports_the_phases_recorded_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scripted_agent.cli.os.environ", _env("implement"))
    monkeypatch.setattr("sys.argv", ["claude", "--version"])
    cli.main()
    assert capsys.readouterr().out == "2.0.74 (Claude Code)\n"
