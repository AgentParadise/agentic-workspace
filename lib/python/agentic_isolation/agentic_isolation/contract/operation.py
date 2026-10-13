"""`apss.workspace-operation/v1`: the request to start an operation, and its ref."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from agentic_isolation.contract._base import (
    MISSING,
    ContractModel,
    CredentialFreeStringMap,
    JsonInt,
    NonEmptyStr,
    RelativePath,
)
from agentic_isolation.contract.refs import CredentialBinding, WorkspaceRef


class LaunchMarker(ContractModel):
    """The line a launcher prints so a provider can find the operation's start."""

    argv0: NonEmptyStr
    line: NonEmptyStr


class OperationSpec(ContractModel):
    """A request to start one operation inside a workspace."""

    schema_: Literal["apss.workspace-operation/v1"] | MISSING = Field(MISSING, alias="schema")
    execution_id: NonEmptyStr
    operation_key: NonEmptyStr
    argv: Annotated[tuple[str, ...], Field(min_length=1)]
    working_directory: RelativePath
    environment: CredentialFreeStringMap
    credential_bindings: tuple[CredentialBinding, ...]
    launch_marker: LaunchMarker | MISSING = MISSING
    timeout_seconds: Annotated[JsonInt, Field(ge=1)]
    metadata: CredentialFreeStringMap


class OperationRef(ContractModel):
    """The reference a provider returns for a started operation."""

    workspace: WorkspaceRef
    operation_id: NonEmptyStr
    operation_key: NonEmptyStr
