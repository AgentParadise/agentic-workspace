"""`apss.workspace-status/v1`: an OperationStatus or a typed OperationError, never both."""

from __future__ import annotations

from typing import Literal, Self, get_args

from pydantic import Field, model_validator

from agentic_isolation.contract._base import (
    MISSING,
    ContractModel,
    CredentialFreeStringMap,
    JsonInt,
    NonEmptyStr,
)
from agentic_isolation.contract.operation import OperationRef

TerminalState = Literal["succeeded", "failed", "cancelled", "timed_out", "lost"]
OperationState = Literal["pending", "running"] | TerminalState
TERMINAL_STATES: frozenset[str] = frozenset(get_args(TerminalState))

ErrorKind = Literal[
    "rejected_policy",
    "unsupported_capability",
    "hydration_failed",
    "conflict",
    "unknown_operation",
    "timeout",
    "cancelled",
    "resource_lost",
    "provider_unavailable",
    "internal",
]


class OperationStatus(ContractModel):
    """Where an operation is. Exit facts exist only once it has ended."""

    schema_: Literal["apss.workspace-status/v1"] | MISSING = Field(MISSING, alias="schema")
    ref: OperationRef
    state: OperationState
    exit_code: JsonInt | None | MISSING = MISSING
    signal: str | None | MISSING = MISSING
    # `format: date-time` is an annotation the schemas do not assert, so the
    # timestamp is kept as the string the producer wrote.
    started_at: str | MISSING = MISSING
    ended_at: str | MISSING = MISSING
    execution_id: NonEmptyStr
    metadata: CredentialFreeStringMap

    @model_validator(mode="after")
    def _exit_facts_only_when_ended(self) -> Self:
        if self.state not in TERMINAL_STATES:
            present = [
                name
                for name in ("exit_code", "signal", "ended_at")
                if getattr(self, name) is not MISSING
            ]
            if present:
                raise ValueError(f"a {self.state} operation cannot carry {present}")
        return self


class OperationError(ContractModel):
    """A typed failure. Callers dispatch on `kind`, never on `message`."""

    schema_: Literal["apss.workspace-status/v1"] | MISSING = Field(MISSING, alias="schema")
    kind: ErrorKind
    message: NonEmptyStr
    ref: OperationRef | MISSING = MISSING
