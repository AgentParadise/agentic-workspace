"""`apss.workspace-provider/v1`: what a provider can enforce, published before launch."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from agentic_isolation.contract._base import MISSING, ContractModel, NonEmptyStr, unique_items

UniqueNames = Annotated[tuple[NonEmptyStr, ...], AfterValidator(unique_items)]


class EnforceableLimits(ContractModel):
    """Which resource limits the provider can actually enforce."""

    cpu: bool
    memory: bool
    disk: bool
    network: bool
    timeout: bool


class ObservationDescriptor(ContractModel):
    """How much output the provider retains and how it reports loss."""

    retention_bytes: Annotated[int, Field(ge=1)]
    max_page_bytes: Annotated[int, Field(ge=1)]
    integrity: Literal["exact", "continuity_detected"]


class ProviderDescriptor(ContractModel):
    """A provider's published capabilities and security profile."""

    schema_: Literal["apss.workspace-provider/v1"] | MISSING = Field(MISSING, alias="schema")
    provider: NonEmptyStr
    security_profile: Literal["insecure-local", "isolated"]
    enforceable_limits: EnforceableLimits
    capabilities: UniqueNames
    observation: ObservationDescriptor
    interactive: bool
    environment_kinds: UniqueNames
