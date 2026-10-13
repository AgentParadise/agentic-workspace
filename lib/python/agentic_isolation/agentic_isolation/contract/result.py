"""`apss.workspace-result/v1`: the terminal record of one workspace execution.

Process, capture and finalization are separate facts. `success` is derived
from them and must agree: the process succeeded and any required capture was
captured.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agentic_isolation.contract._base import (
    MISSING,
    ContractModel,
    CredentialFreeStringMap,
    NonEmptyStr,
    RelativePath,
)
from agentic_isolation.contract.finalization import FinalizationOutcome
from agentic_isolation.contract.refs import ContentRef, EnvironmentRef
from agentic_isolation.contract.status import TERMINAL_STATES, OperationError, OperationStatus


class ResolvedCommit(ContractModel):
    """The commit a workspace destination resolved to."""

    destination: RelativePath
    commit: NonEmptyStr


class CaptureOutcome(ContractModel):
    """Whether the execution's transcript capture completed."""

    required: bool
    state: Literal["captured", "incomplete", "failed", "unknown", "disabled"]
    needs_backfill: bool


class WorkspaceResult(ContractModel):
    """One execution's result. `success` is the conjunction, never asserted alone."""

    schema_: Literal["apss.workspace-result/v1"] | MISSING = Field(MISSING, alias="schema")
    execution_id: NonEmptyStr
    metadata: CredentialFreeStringMap
    process: OperationStatus
    effective_model: str | None | MISSING = MISSING
    effective_environment: EnvironmentRef | MISSING = MISSING
    resolved_commits: tuple[ResolvedCommit, ...]
    artifacts: tuple[ContentRef, ...]
    capture: CaptureOutcome
    finalization: FinalizationOutcome
    primary_failure: OperationError | MISSING = MISSING
    teardown_failure: OperationError | MISSING = MISSING
    success: bool

    @model_validator(mode="after")
    def _success_is_derived(self) -> Self:
        if self.process.state not in TERMINAL_STATES:
            raise ValueError(f"a result's process must be terminal, not {self.process.state}")
        succeeded = self.process.state == "succeeded"
        captured = not self.capture.required or self.capture.state == "captured"
        if self.success != (succeeded and captured):
            raise ValueError(
                f"success={self.success} contradicts process {self.process.state} "
                f"and required={self.capture.required} capture {self.capture.state}"
            )
        if succeeded and self.primary_failure is not MISSING:
            raise ValueError("a succeeded process cannot carry a primary_failure")
        return self
