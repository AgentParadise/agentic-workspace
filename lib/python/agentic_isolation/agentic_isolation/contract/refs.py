"""Reference documents: credential, content, environment and workspace refs.

A CredentialRef names a credential the provider resolves from its own
configuration. No model in this package has a field that can hold a
credential value.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from agentic_isolation.contract._base import MISSING, ContractModel, JsonInt, NonEmptyStr


class CredentialRef(ContractModel):
    """`apss.credential-ref/v1`: a credential's name and scope, never its value."""

    schema_: Literal["apss.credential-ref/v1"] | MISSING = Field(MISSING, alias="schema")
    name: NonEmptyStr
    scope: NonEmptyStr


class CredentialBinding(ContractModel):
    """Where the provider delivers a resolved credential inside the workspace."""

    ref: CredentialRef
    delivery: Literal["env", "file"]
    target: NonEmptyStr


class ContentRef(ContractModel):
    """`apss.content-ref/v1`: immutable content identified by digest."""

    schema_: Literal["apss.content-ref/v1"] | MISSING = Field(MISSING, alias="schema")
    digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    size_bytes: Annotated[JsonInt, Field(ge=0)]
    media_type: NonEmptyStr | MISSING = MISSING
    locator: NonEmptyStr
    authority: CredentialRef | MISSING = MISSING


class EnvironmentRef(ContractModel):
    """`apss.environment-ref/v1`: opaque environment identity, compared by equality."""

    schema_: Literal["apss.environment-ref/v1"] | MISSING = Field(MISSING, alias="schema")
    kind: NonEmptyStr
    identifier: NonEmptyStr
    digest: NonEmptyStr | MISSING = MISSING


class WorkspaceRef(ContractModel):
    """`apss.workspace-ref/v1`: opaque provider-issued handle to one workspace."""

    schema_: Literal["apss.workspace-ref/v1"] | MISSING = Field(MISSING, alias="schema")
    provider: NonEmptyStr
    resource_id: NonEmptyStr
