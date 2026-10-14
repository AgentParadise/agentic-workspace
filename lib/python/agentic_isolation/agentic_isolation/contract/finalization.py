"""`apss.workspace-finalization/v1`: the outcome of finalizing a workspace."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agentic_isolation.contract._base import MISSING, ContractModel
from agentic_isolation.contract.refs import WorkspaceRef
from agentic_isolation.contract.status import OperationError


class FinalizationOutcome(ContractModel):
    """completed carries nothing else; retained names the kept workspace; failed says why."""

    schema_: Literal["apss.workspace-finalization/v1"] | MISSING = Field(MISSING, alias="schema")
    state: Literal["completed", "retained", "failed"]
    stage: Literal["while_running", "before_delete"] | MISSING = MISSING
    error: OperationError | MISSING = MISSING
    retained: WorkspaceRef | MISSING = MISSING

    @model_validator(mode="after")
    def _fields_match_state(self) -> Self:
        present = {
            name for name in ("stage", "error", "retained") if getattr(self, name) is not MISSING
        }
        required, forbidden = {
            "completed": (set(), {"stage", "error", "retained"}),
            "retained": ({"stage", "error", "retained"}, set()),
            "failed": ({"stage", "error"}, {"retained"}),
        }[self.state]
        if missing := sorted(required - present):
            raise ValueError(f"a {self.state} finalization requires {missing}")
        if extra := sorted(forbidden & present):
            raise ValueError(f"a {self.state} finalization cannot carry {extra}")
        if self.state == "retained" and self.stage != "before_delete":
            raise ValueError("a retained finalization happens before_delete")
        return self
