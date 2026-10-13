"""APSS EXP-V1-0006 0.2.0 workspace operation contract, as Pydantic models.

The vendored schemas in `schemas/` are normative; these models must accept and
reject exactly what they do. `PROVENANCE.md` records the APSS revision both
were taken from, and the tests hold it equal to the Cargo.toml pin.

`parse_document` is the boundary: it takes the JSON text of any top-level
contract document, dispatches on its `schema` discriminator, and returns the
typed model or raises `pydantic.ValidationError`. Models serialize back to
schema-valid JSON with `model_dump_json()`; absent optional fields stay absent.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Discriminator, Tag, TypeAdapter

from agentic_isolation.contract._base import MISSING, ContractModel
from agentic_isolation.contract.finalization import FinalizationOutcome
from agentic_isolation.contract.observation import ObservationGap, ObservationPage
from agentic_isolation.contract.operation import LaunchMarker, OperationRef, OperationSpec
from agentic_isolation.contract.provider import (
    EnforceableLimits,
    ObservationDescriptor,
    ProviderDescriptor,
)
from agentic_isolation.contract.refs import (
    ContentRef,
    CredentialBinding,
    CredentialRef,
    EnvironmentRef,
    WorkspaceRef,
)
from agentic_isolation.contract.result import CaptureOutcome, ResolvedCommit, WorkspaceResult
from agentic_isolation.contract.status import (
    TERMINAL_STATES,
    ErrorKind,
    OperationError,
    OperationState,
    OperationStatus,
    TerminalState,
)

__all__ = [
    "MISSING",
    "TERMINAL_STATES",
    "CaptureOutcome",
    "ContentRef",
    "ContractDocument",
    "ContractModel",
    "CredentialBinding",
    "CredentialRef",
    "EnforceableLimits",
    "EnvironmentRef",
    "ErrorKind",
    "FinalizationOutcome",
    "LaunchMarker",
    "ObservationDescriptor",
    "ObservationGap",
    "ObservationPage",
    "OperationError",
    "OperationRef",
    "OperationSpec",
    "OperationState",
    "OperationStatus",
    "ProviderDescriptor",
    "ResolvedCommit",
    "TerminalState",
    "WorkspaceRef",
    "WorkspaceResult",
    "parse_document",
]

_STATUS = "apss.workspace-status/v1"
_STATUS_ERROR = "apss.workspace-status/v1#error"


def _document_tag(document: object) -> str | None:
    """The `schema` discriminator; workspace-status splits on the error's `kind`."""
    if not isinstance(document, dict):
        return None
    tag = document.get("schema")
    if not isinstance(tag, str):
        return None
    if tag == _STATUS and "kind" in document:
        return _STATUS_ERROR
    return tag


ContractDocument = Annotated[
    Annotated[ContentRef, Tag("apss.content-ref/v1")]
    | Annotated[CredentialRef, Tag("apss.credential-ref/v1")]
    | Annotated[EnvironmentRef, Tag("apss.environment-ref/v1")]
    | Annotated[FinalizationOutcome, Tag("apss.workspace-finalization/v1")]
    | Annotated[ObservationPage, Tag("apss.workspace-observation/v1")]
    | Annotated[OperationSpec, Tag("apss.workspace-operation/v1")]
    | Annotated[ProviderDescriptor, Tag("apss.workspace-provider/v1")]
    | Annotated[WorkspaceRef, Tag("apss.workspace-ref/v1")]
    | Annotated[WorkspaceResult, Tag("apss.workspace-result/v1")]
    | Annotated[OperationStatus, Tag(_STATUS)]
    | Annotated[OperationError, Tag(_STATUS_ERROR)],
    Discriminator(_document_tag),
]
"""Any top-level contract document. A top-level document must name its schema."""

_DOCUMENT: TypeAdapter[ContractDocument] = TypeAdapter(ContractDocument)


def parse_document(raw: str | bytes) -> ContractDocument:
    """Parse the JSON text of one contract document into its model."""
    return _DOCUMENT.validate_json(raw)
