"""``claude`` in a load-test workspace: replay one phase of a ScriptedAgentProfile.

Arguments are ignored except ``--version``, which reports the phase's
recorded ``cli_version``. Exit codes: 0 the phase ran; 78 no usable profile,
phase or execution id; 65 the recording is not the declared stream; 69 a
workload or git/gh step failed.

Where the inputs come from:

- **Profile.** ``SYN_SCRIPTED_AGENT_PROFILE`` (the JSON itself), else the file
  named by ``SYN_SCRIPTED_AGENT_PROFILE_PATH``. The variable wins because it is
  the name upstream's ``ScriptedAgentProfile.to_env`` writes; the file exists
  because nothing in syntropic137 passes workspace env through yet, while its
  driver already writes ``loadtest-profile.json`` that a stack can mount.
- **Phase and execution id.** ``AGENTIC_SESSION_STORE_TAGS`` (``phase_id:`` and
  ``execution_id:``), the only place syntropic137 hands either to a workspace
  today. Without them the phase cannot be chosen, so the replayer refuses.

Nothing here opens a network connection. Git and gh run as subprocesses with
the workspace's own credential and only talk to the fixture repository.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, TextIO
from urllib.parse import unquote

from pydantic import ValidationError

from scripted_agent.errors import ProfileRefused, Refusal, SideEffectFailed
from scripted_agent.profile import (
    SCRIPTED_AGENT_PROFILE_ENV,
    GatesWorkload,
    MarkPullRequestReady,
    OpenDraftPullRequest,
    PushBranch,
    ScriptedAgentProfile,
    ScriptedPhase,
    SyntheticWorkload,
    VerifyRemoteBranch,
    _named_fields,
)
from scripted_agent.recording import Recording

PROFILE_PATH_ENV: Final = "SYN_SCRIPTED_AGENT_PROFILE_PATH"
TAGS_ENV: Final = "AGENTIC_SESSION_STORE_TAGS"
_CHUNK: Final = 1 << 20
WORKSPACE: Final = Path("/workspace")

Run = Callable[[Sequence[str], Path | None], str]
"""Run a command in a directory and return its stdout; raise ``SideEffectFailed``."""


def main() -> None:
    try:
        if "--version" in sys.argv[1:]:
            print(f"{cli_version(os.environ)} (Claude Code)")
            return
        replay(os.environ, WORKSPACE, _subprocess, time.sleep, sys.stdout)
    except Refusal as e:
        print(f"scripted-agent: {e}", file=sys.stderr)
        sys.exit(e.exit_code)


def replay(
    environ: Mapping[str, str],
    workspace: Path,
    run: Run,
    sleep: Callable[[float], None],
    out: TextIO,
) -> None:
    """Carry out this workspace's phase: stream, workload, side effect, artifact, verdict."""
    profile = load_profile(environ)
    target = _Target.from_tags(environ.get(TAGS_ENV, ""), profile)
    phase = profile.phases[target.phase_id]
    recording = Recording.load(phase.stream)

    previous = 0
    for event, offset in zip(recording.events, recording.offsets_ms, strict=True):
        sleep(max(offset - previous, 0) / 1000 * phase.stream.pacing_factor)
        previous = offset
        _emit(out, event)
    # The recorded delay before the closing event is slept here, before the work,
    # so the verdict can never precede its scaled schedule; the phase's own work
    # comes on top of it rather than overlapping it.
    sleep(max(recording.result_offset_ms - previous, 0) / 1000 * phase.stream.pacing_factor)

    repo = workspace / "repos" / profile.fixture_repo.split("/")[1]
    _run_workload(phase, repo, run)
    pr_url = _side_effect(profile, phase, target, repo, run)
    artifact = _render(profile, phase, target, pr_url, run)
    output = workspace / "artifacts" / "output" / f"{target.phase_id}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(artifact, encoding="utf-8")

    verdict = profile.task_result(target.phase_id)
    _emit(out, {**recording.result, "result": verdict})


def cli_version(environ: Mapping[str, str]) -> str:
    """The CLI version this workspace's phase was recorded from, for ``--version``."""
    profile = load_profile(environ)
    target = _Target.from_tags(environ.get(TAGS_ENV, ""), profile)
    return profile.phases[target.phase_id].stream.cli_version


def load_profile(environ: Mapping[str, str]) -> ScriptedAgentProfile:
    """The profile from the env var, else from the mounted file; refuse without one."""
    if SCRIPTED_AGENT_PROFILE_ENV in environ:
        source, raw = SCRIPTED_AGENT_PROFILE_ENV, environ[SCRIPTED_AGENT_PROFILE_ENV]
    elif PROFILE_PATH_ENV in environ:
        source = environ[PROFILE_PATH_ENV]
        try:
            raw = Path(source).read_bytes().decode("utf-8")
        except OSError as e:
            raise ProfileRefused(f"{PROFILE_PATH_ENV}={source} cannot be read: {e}") from e
        except UnicodeDecodeError as e:
            raise ProfileRefused(f"{PROFILE_PATH_ENV}={source} is not UTF-8: {e}") from e
    else:
        raise ProfileRefused(
            f"neither {SCRIPTED_AGENT_PROFILE_ENV} nor {PROFILE_PATH_ENV} is set; "
            "a scripted-agent workspace cannot run without its profile"
        )
    try:
        return ScriptedAgentProfile.model_validate_json(raw)
    except ValidationError as e:
        raise ProfileRefused(
            f"profile from {source} is not a valid ScriptedAgentProfile: {e}"
        ) from e


@dataclass(frozen=True)
class _Target:
    phase_id: str
    execution_id: str

    @classmethod
    def from_tags(cls, tags: str, profile: ScriptedAgentProfile) -> _Target:
        pairs = dict(tag.split(":", 1) for tag in tags.split(",") if ":" in tag)
        phase_id, execution_id = (unquote(pairs.get(k, "")) for k in ("phase_id", "execution_id"))
        if not phase_id or not execution_id:
            raise ProfileRefused(
                f"{TAGS_ENV} must carry phase_id: and execution_id: tags to pick the "
                f"phase to replay; got {tags!r}"
            )
        if phase_id not in profile.phases:
            raise ProfileRefused(
                f"phase {phase_id!r} is not in the profile (phases {sorted(profile.phases)})"
            )
        return cls(phase_id=phase_id, execution_id=execution_id)


def _emit(out: TextIO, event: Mapping[str, object]) -> None:
    out.write(json.dumps(event) + "\n")
    out.flush()


def _run_workload(phase: ScriptedPhase, repo: Path, run: Run) -> None:
    workload = phase.workload
    if isinstance(workload, GatesWorkload):
        run(["just", "preflight-agent"], repo)
    elif isinstance(workload, SyntheticWorkload):
        _synthetic(workload)


def _synthetic(
    workload: SyntheticWorkload,
    clock: Callable[[], float] = time.process_time,
    allocate: Callable[[int], object] = bytearray,
    scratch: Callable[[], BinaryIO] = tempfile.TemporaryFile,
) -> None:
    """Write ``disk_bytes`` outside the repo and flush them, then burn ``cpu_seconds``
    of CPU while holding ``mem_bytes``."""
    held = allocate(workload.mem_bytes)
    with scratch() as disk:
        for offset in range(0, workload.disk_bytes, _CHUNK):
            disk.write(bytes(min(_CHUNK, workload.disk_bytes - offset)))
        disk.flush()
        os.fsync(disk.fileno())
        start = clock()
        while clock() - start < workload.cpu_seconds:
            pass
    del held


def _side_effect(
    profile: ScriptedAgentProfile, phase: ScriptedPhase, target: _Target, repo: Path, run: Run
) -> str | None:
    """Perform the phase's side effect; return the pull request URL if it opened one."""
    effect = phase.side_effect
    branch = profile.branch(target.execution_id)
    if isinstance(effect, PushBranch | OpenDraftPullRequest):
        if _remote_head(profile, branch, run):
            run(["git", "fetch", "origin", branch], repo)
            run(["git", "checkout", "-B", branch, "FETCH_HEAD"], repo)
        else:
            run(["git", "checkout", "-B", branch], repo)
        marker = repo / "loadtest" / f"{target.phase_id}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{target.execution_id} {target.phase_id}\n", encoding="utf-8")
        run(["git", "add", str(marker.relative_to(repo))], repo)
        run(["git", "commit", "-m", f"loadtest: {target.phase_id}"], repo)
        run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], repo)
    if isinstance(effect, OpenDraftPullRequest):
        created = run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--repo",
                profile.fixture_repo,
                "--head",
                branch,
                "--title",
                f"loadtest {target.execution_id}",
                "--body",
                "Load-test stub pull request; no agent ran.",
            ],
            repo,
        )
        return created.strip().splitlines()[-1]
    if isinstance(effect, MarkPullRequestReady):
        run(["gh", "pr", "ready", branch, "--repo", profile.fixture_repo], None)
    if isinstance(effect, VerifyRemoteBranch) and not _remote_head(profile, branch, run):
        raise SideEffectFailed(f"{branch} is not on {profile.fixture_repo}")
    return None


def _render(
    profile: ScriptedAgentProfile,
    phase: ScriptedPhase,
    target: _Target,
    pr_url: str | None,
    run: Run,
) -> str:
    """The artifact, looking up the head and the PR only when the template names them."""
    named = _named_fields(phase.artifact)
    branch = profile.branch(target.execution_id)
    head_sha = _remote_head(profile, branch, run) if "head_sha" in named else None
    if "pull_request" in named and profile.tier == "node" and pr_url is None:
        pr_url = run(
            [
                "gh",
                "pr",
                "view",
                branch,
                "--repo",
                profile.fixture_repo,
                "--json",
                "url",
                "--jq",
                ".url",
            ],
            None,
        ).strip()
    try:
        return profile.render_artifact(target.phase_id, target.execution_id, head_sha, pr_url)
    except ValueError as e:
        raise SideEffectFailed(f"phase {target.phase_id} artifact cannot be rendered: {e}") from e


def _remote_head(profile: ScriptedAgentProfile, branch: str, run: Run) -> str | None:
    """The commit ``branch`` is at on the fixture repository, or None if it is absent."""
    listed = run(
        [
            "git",
            "ls-remote",
            f"https://github.com/{profile.fixture_repo}.git",
            f"refs/heads/{branch}",
        ],
        None,
    )
    return listed.split()[0] if listed.strip() else None


def _subprocess(argv: Sequence[str], cwd: Path | None) -> str:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as e:
        raise SideEffectFailed(f"{argv[0]} cannot run: {e}") from e
    if done.returncode != 0:
        raise SideEffectFailed(
            f"`{' '.join(argv)}` exited {done.returncode}: {done.stderr.strip()}"
        )
    return done.stdout
