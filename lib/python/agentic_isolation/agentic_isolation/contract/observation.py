"""`apss.workspace-observation/v1`: one bounded page of an operation's output."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from agentic_isolation.contract._base import MISSING, ContractModel, NonEmptyStr
from agentic_isolation.contract.operation import OperationRef
from agentic_isolation.contract.status import TERMINAL_STATES, OperationState

_BASE64 = r"^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"


class ObservationGap(ContractModel):
    """Bytes lost before this page. `None` means the extent is unknown."""

    lost_bytes: Annotated[int, Field(ge=1)] | None


class ObservationPage(ContractModel):
    """A page addressed by opaque cursors. A complete page has a terminal state."""

    schema_: Literal["apss.workspace-observation/v1"] | MISSING = Field(MISSING, alias="schema")
    ref: OperationRef
    state: OperationState
    cursor: NonEmptyStr
    next_cursor: NonEmptyStr
    data_b64: Annotated[str, Field(pattern=_BASE64)]
    gap: ObservationGap | MISSING = MISSING
    complete: bool

    @model_validator(mode="after")
    def _complete_only_when_terminal(self) -> Self:
        if self.complete and self.state not in TERMINAL_STATES:
            raise ValueError(f"a complete page cannot have non-terminal state {self.state}")
        return self
