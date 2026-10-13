"""Port of syntropic137's ``ScriptedAgentProfile`` contract (loadtest, #1717).

The replayer is the reading half of a contract syntropic137 writes: the driver
builds a profile from its ``WorkflowDefinition`` and every stub workspace reads
it back here. The model, docstrings and validators are copied from the source
recorded in ``contract/PROVENANCE.md`` so that ``model_json_schema()`` is
byte-for-byte the vendored schema (``tests/test_contract_parity.py``), which is
what keeps the two ends from drifting. Left out: ``for_workflow`` and
``to_env`` (driver side only), and the ``syn_domain`` import, whose
``next_phase`` rule is ported in ``_next_phase`` below.

Edit the source upstream, then re-vendor; do not edit the models here alone.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping  # noqa: TC003 - pydantic resolves the field type at runtime
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

ReviewVerdictName = Literal["certified", "blocked"]
"""The words a review phase may report as ``review_verdict`` (``ReviewVerdict``)."""

FULL_SHA: Final = re.compile(r"[0-9a-f]{40}")

SCRIPTED_AGENT_PROFILE_ENV: Final = "SYN_SCRIPTED_AGENT_PROFILE"
"""The one environment variable a stub workspace reads its profile from."""

SCRIPTED_AGENT_MODEL: Final = "syn-scripted-agent"
"""The model id every replayed stream reports in place of the recorded one.

The stub rewrites the ``model`` of every event it replays to this, so the
server records it as the session's observed ``agent_model``. The driver holds
every session of a run to it: any other model means a real harness ran and
spent tokens (#1717)."""

LOADTEST_BRANCH_PREFIX: Final = "loadtest/"
"""Every load-test branch is ``loadtest/<execution_id>`` on the fixture repo."""

_FORBIDDEN_FIXTURE_REPOS: Final = frozenset({"syntropic137/syntropic137"})

#: The only fields an artifact template may name. Anything else is a typo the
#: stub could not fill, so it is refused when the profile is built rather than
#: when the hundredth workspace tries to render it.
_ARTIFACT_FIELDS: Final = frozenset(
    {
        "execution_id",
        "branch",
        "head_sha",
        "pull_request",
        "review_verdict",
        "outcome",
        "repair_rounds",
    }
)

_NO_PULL_REQUEST: Final = "none (the platform tier opens no pull request)"
"""What ``{pull_request}`` renders as on the platform tier."""

# Same shape the workflow parser accepts for a phase id
# (``PHASE_ID_PATTERN`` in syn_domain's workflow_definition).
PhaseId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")]


class _Contract(BaseModel):
    # JSON has no infinity or NaN: pydantic writes them as ``null``, which
    # ``from_env`` then refuses, so a profile that validated here would fail in
    # every workspace it was sent to.
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def _named_fields(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


class ScriptedStream(_Contract):
    """The recorded session a phase replays, and how fast.

    The harness decides the format - ``claude`` replays stream-json, ``codex``
    replays JSONL - so it is not declared twice. ``cli_version`` is the
    version the recording was captured from; the driver refuses a run whose
    pinned image carries a different one (plan section 8), because a stream
    from another CLI version exercises a parser production no longer runs.
    """

    harness: Literal["claude", "codex"]
    recording: str = Field(min_length=1)
    """Path of the recording inside the stub image."""
    cli_version: str = Field(min_length=1)
    tool_uses: int = Field(ge=1)
    """Tool-use events in the recording. The stub refuses to replay a recording
    whose count differs, and the driver holds the server's stored count for
    the phase's session to it, so a dropped observation shows as a shortfall
    (#1717). Never zero: a recording with no tool use proves nothing about
    observation loss."""
    pacing_factor: float = Field(default=1.0, ge=0)
    """Recorded inter-event timing multiplied by this. ``0`` replays at once."""


class NoWorkload(_Contract):
    kind: Literal["none"] = "none"


class SyntheticWorkload(_Contract):
    """Burn this much CPU, hold this much memory, write this much disk.

    Disk goes outside the repository or to an ignored path, so a phase that
    must leave its tree clean still does.
    """

    kind: Literal["synthetic"] = "synthetic"
    cpu_seconds: float = Field(ge=0)
    mem_bytes: int = Field(ge=0)
    disk_bytes: int = Field(ge=0)


class GatesWorkload(_Contract):
    """Run ``just preflight-agent`` in the cloned repository - the real gates."""

    kind: Literal["gates"] = "gates"


Workload = Annotated[NoWorkload | SyntheticWorkload | GatesWorkload, Field(discriminator="kind")]


class ReportOnly(_Contract):
    """Write the artifact and leave the working tree exactly as cloned.

    The real phase declares ``delivers_repo_changes: false``, which does not
    yet exempt a dirty tree from the unpushed-work guard (nothing mounts the
    repository read-only, ``unpushed_work_guard.py``), so a stub that left a
    file behind would fail where the real phase passes.
    """

    kind: Literal["report_only"] = "report_only"


class PushBranch(_Contract):
    """Commit one deterministic file and push ``loadtest/<execution_id>``.

    The file is ``loadtest/<phase_id>.txt`` holding ``<execution_id>
    <phase_id>``, committed on top of the branch if an earlier phase already
    pushed it, so implement and fix both land on one branch as they do for
    real. The artifact names the branch.
    """

    kind: Literal["push_branch"] = "push_branch"


class OpenDraftPullRequest(_Contract):
    """Push exactly as ``PushBranch`` does, then ``gh pr create --draft`` from it.

    Node tier only, and only the first phase that delivers repository changes:
    the real implement phase opens a draft on its first push and records its
    number and URL, and every later pushing phase lands on that same PR. The
    driver closes it afterwards.
    """

    kind: Literal["open_draft_pull_request"] = "open_draft_pull_request"


class MarkPullRequestReady(_Contract):
    """``gh pr ready`` the draft an earlier phase opened on the fixture repo.

    Node tier only. The real finalize_pr phase is the only one allowed to mark
    the PR ready (#1197). The phase has no working tree (``clone_repos:
    false``).
    """

    kind: Literal["mark_pull_request_ready"] = "mark_pull_request_ready"


class VerifyRemoteBranch(_Contract):
    """``git ls-remote`` the execution's branch and fail if it is absent.

    The platform-tier stand-in for the PR lifecycle, so 100 executions do not
    open 100 PRs. The phase has no working tree (``clone_repos: false``).
    """

    kind: Literal["verify_remote_branch"] = "verify_remote_branch"


SideEffect = Annotated[
    ReportOnly | PushBranch | OpenDraftPullRequest | MarkPullRequestReady | VerifyRemoteBranch,
    Field(discriminator="kind"),
]

_NO_WORKING_TREE: Final = (MarkPullRequestReady, VerifyRemoteBranch)
_TOUCHES_A_PR: Final = (OpenDraftPullRequest, MarkPullRequestReady)

#: The PR side effects a profile may run, in phase order: none (platform tier),
#: a draft nobody marks ready, or the real lifecycle.
_PR_LIFECYCLES: Final = frozenset(
    {(), ("open_draft_pull_request",), ("open_draft_pull_request", "mark_pull_request_ready")}
)


class ScriptedPhase(_Contract):
    """Everything the stub does in one phase."""

    stream: ScriptedStream
    workload: Workload
    side_effect: SideEffect
    artifact: str = Field(min_length=1)
    """Markdown written to ``artifacts/output/<phase_id>.md``. May name
    ``{execution_id}``, ``{branch}``, ``{head_sha}``, ``{pull_request}``,
    ``{review_verdict}`` (this phase's, upper-cased), and ``{outcome}`` and
    ``{repair_rounds}`` (how the planned run ends); literal braces are doubled."""
    review_verdict: ReviewVerdictName | None = None
    """The ``review_verdict`` this phase's ``TASK_RESULT`` reports, or none.

    The stub ends the phase with ``ScriptedAgentProfile.task_result`` as its
    last message and does not replay the recording's own ``TASK_RESULT``, so
    this field, not the recording, is what the engine's verdict reader sees."""

    @model_validator(mode="after")
    def _consistent(self) -> ScriptedPhase:
        if isinstance(self.workload, GatesWorkload) and isinstance(
            self.side_effect, _NO_WORKING_TREE
        ):
            msg = (
                f"a {self.side_effect.kind} phase has no working tree, so it "
                "cannot run the gates workload"
            )
            raise ValueError(msg)
        if unknown := _named_fields(self.artifact) - _ARTIFACT_FIELDS:
            msg = (
                f"artifact names {sorted(unknown)}; only {sorted(_ARTIFACT_FIELDS)} "
                "can be filled (double a literal brace)"
            )
            raise ValueError(msg)
        return self


class ScriptedAgentProfile(_Contract):
    """One load-test run's instructions to the stub agent, per phase id."""

    tier: Literal["platform", "node"]
    fixture_repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    """``owner/name`` of the dedicated repository the stub pushes to."""
    phases: Annotated[
        Mapping[PhaseId, ScriptedPhase],
        Field(min_length=1),
        AfterValidator(MappingProxyType),
        PlainSerializer(dict, return_type=dict[PhaseId, ScriptedPhase]),
    ]
    """Read-only once validated: ``frozen`` stops reassigning the field, and
    this stops editing it in place, so no phase can be added or swapped after
    the checks below have passed it."""

    @model_validator(mode="after")
    def _safe_to_run_at_scale(self) -> ScriptedAgentProfile:
        if self.fixture_repo.lower() in _FORBIDDEN_FIXTURE_REPOS:
            msg = f"{self.fixture_repo} cannot be the load-test fixture repository"
            raise ValueError(msg)
        if self.tier == "platform":
            touching = sorted(
                phase_id
                for phase_id, phase in self.phases.items()
                if isinstance(phase.side_effect, _TOUCHES_A_PR)
            )
            if touching:
                msg = (
                    f"platform tier must not touch pull requests (phases {touching}); "
                    "use push_branch and verify_remote_branch"
                )
                raise ValueError(msg)
        lifecycle = tuple(
            phase.side_effect.kind
            for phase in self.phases.values()
            if isinstance(phase.side_effect, _TOUCHES_A_PR)
        )
        if lifecycle not in _PR_LIFECYCLES:
            msg = (
                f"pull request side effects run {list(lifecycle)}; exactly one phase "
                "opens the draft pull request, before the one that marks it ready"
            )
            raise ValueError(msg)
        run = self.planned_run()
        ran = [self.phases[p].side_effect for p in run.ran]
        opened = any(isinstance(s, OpenDraftPullRequest) for s in ran)
        readied = any(isinstance(s, MarkPullRequestReady) for s in ran)
        if readied != (opened and run.ended_on == "certified"):
            msg = (
                f"the run is {list(run.ran)} and ends {run.ended_on or 'with no verdict'}; "
                "a phase that runs marks the draft ready exactly when the run ends certified"
            )
            raise ValueError(msg)
        return self

    def planned_run(self) -> PlannedRun:
        """The phases this profile's verdicts make the engine run, and how it ends."""
        ran, ended_on = _walk({p: phase.review_verdict for p, phase in self.phases.items()})
        pushes = sum(
            isinstance(self.phases[p].side_effect, (PushBranch, OpenDraftPullRequest)) for p in ran
        )
        return PlannedRun(ran=ran, ended_on=ended_on, repair_rounds=max(pushes - 1, 0))

    def task_result(self, phase_id: str) -> str:
        """The ``TASK_RESULT`` block the stub writes as its last message in ``phase_id``."""
        phase = self.phases[phase_id]
        report = _TaskResult(
            side_effects="none" if isinstance(phase.side_effect, ReportOnly) else "succeeded",
            comments=f"Load-test stub for phase {phase_id}; no agent ran.",
            review_verdict=phase.review_verdict,
        )
        return f"TASK_RESULT: {report.model_dump_json(exclude_none=True)}\nTASK_RESULT_END"

    @staticmethod
    def branch(execution_id: str) -> str:
        return f"{LOADTEST_BRANCH_PREFIX}{execution_id}"

    def render_artifact(
        self,
        phase_id: str,
        execution_id: str,
        head_sha: str | None = None,
        pr_url: str | None = None,
    ) -> str:
        """The exact artifact text the stub writes for this phase and execution.

        ``head_sha`` is the full commit the execution's branch is at when the
        phase writes its artifact: ``git rev-parse HEAD`` after pushing
        (implement, fix) or checking out (verify, reverify), the ``git
        ls-remote`` result where there is no tree (finalize_pr). A template
        that names it will not render without a full 40-character SHA.

        ``pr_url`` is the draft the node tier opened on the fixture repository;
        ``{pull_request}`` renders as its number and URL. The platform tier
        opens none, so it takes no URL and says so.
        """
        phase = self.phases[phase_id]
        template = phase.artifact
        named = _named_fields(template)
        run = self.planned_run()
        if "head_sha" in named and not (head_sha and FULL_SHA.fullmatch(head_sha)):
            msg = f"phase {phase_id} artifact names the head; {head_sha!r} is not a full SHA"
            raise ValueError(msg)
        return template.format(
            execution_id=execution_id,
            branch=self.branch(execution_id),
            head_sha=head_sha,
            pull_request=self._pull_request(phase_id, pr_url) if "pull_request" in named else None,
            review_verdict=(phase.review_verdict or "no verdict").upper(),
            outcome="READY" if run.ended_on == "certified" else "DRAFT",
            repair_rounds=run.repair_rounds,
        )

    def _pull_request(self, phase_id: str, pr_url: str | None) -> str:
        if self.tier == "platform":
            if pr_url is not None:
                msg = f"phase {phase_id}: the platform tier opens no pull request, got {pr_url}"
                raise ValueError(msg)
            return _NO_PULL_REQUEST
        on_fixture = re.fullmatch(
            rf"https://github\.com/{re.escape(self.fixture_repo)}/pull/([1-9][0-9]*)",
            pr_url or "",
            re.IGNORECASE,
        )
        if not on_fixture:
            msg = (
                f"phase {phase_id} artifact names the pull request; {pr_url!r} is not "
                f"a pull request on {self.fixture_repo}"
            )
            raise ValueError(msg)
        return f"#{on_fixture[1]} {pr_url}"


@dataclass(frozen=True)
class PlannedRun:
    """What a profile's declared verdicts make the engine do."""

    ran: tuple[str, ...]
    """The phases that run, in order; the rounds a certification skips are absent."""
    ended_on: ReviewVerdictName | None
    """The last verdict any phase that ran reported, as the aggregate records it."""
    repair_rounds: int
    """Pushing phases that ran after the first: the fix rounds."""


class _TaskResult(_Contract):
    success: Literal[True] = True
    side_effects: Literal["none", "succeeded"]
    comments: str
    review_verdict: ReviewVerdictName | None = None


def _walk(
    verdicts: Mapping[str, ReviewVerdictName | None],
) -> tuple[tuple[str, ...], ReviewVerdictName | None]:
    """Sequence ``verdicts`` (phase id -> verdict, in phase order) with the aggregate's rule."""
    order = list(verdicts)
    ran: list[str] = []
    ended_on: ReviewVerdictName | None = None
    at: int | None = 0 if order else None
    while at is not None:
        ran.append(order[at])
        said = verdicts[order[at]]
        ended_on = said or ended_on
        at = _next_phase(len(order), at, said)
    return tuple(ran), ended_on


def _next_phase(count: int, completed: int, verdict: ReviewVerdictName | None) -> int | None:
    """syn_domain's ``review_rounds.next_phase`` over phase indices.

    Certified jumps to the final phase, skipping every round between; anything
    else advances by order; the last phase has no successor.
    """
    if completed >= count - 1:
        return None
    return count - 1 if verdict == "certified" else completed + 1
