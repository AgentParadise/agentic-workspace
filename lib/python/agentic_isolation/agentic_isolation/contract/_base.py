"""Shared building blocks for the EXP-V1-0006 0.2.0 contract models.

Every model is frozen and rejects unknown fields, matching the schemas'
`additionalProperties: false`. Optional schema properties use the `MISSING`
sentinel rather than `None`, so an absent property and an explicit JSON `null`
stay distinct, exactly as the schemas distinguish them.
"""

from __future__ import annotations

import re
from typing import Annotated, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic.experimental.missing_sentinel import MISSING

__all__ = [
    "MISSING",
    "ContractModel",
    "CredentialFreeStringMap",
    "NonEmptyStr",
    "RelativePath",
    "unique_items",
]


class ContractModel(BaseModel):
    """Base for every contract model: immutable, closed, strict."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, serialize_by_alias=True)


NonEmptyStr = Annotated[str, Field(min_length=1)]
RelativePath = Annotated[str, Field(min_length=1, pattern=r"^[^/]")]

# credential-ref.schema.json#/$defs/credentialFreeStringMap, verbatim.
_CREDENTIAL_KEY = re.compile(
    r"^[Vv][Aa][Ll][Uu][Ee]$|[Ss][Ee][Cc][Rr][Ee][Tt]|[Tt][Oo][Kk][Ee][Nn]"
    r"|[Pp][Aa][Ss][Ss]([Ww][Oo][Rr][Dd]|[Ww][Dd])|[Aa][Pp][Ii]_?[Kk][Ee][Yy]"
    r"|[Pp][Rr][Ii][Vv][Aa][Tt][Ee]_?[Kk][Ee][Yy]|[Cc][Rr][Ee][Dd][Ee][Nn][Tt][Ii][Aa][Ll]"
)


def _reject_credential_keys(value: dict[str, str]) -> dict[str, str]:
    named = sorted(key for key in value if _CREDENTIAL_KEY.search(key))
    if named:
        raise ValueError(
            f"keys {named} name a credential value; pass a CredentialRef binding instead"
        )
    return value


# A free-form string map that cannot carry a key naming a credential value
# (spec 5.1.3). A secret under an innocuous key is forbidden normatively; no
# schema can recognise it.
CredentialFreeStringMap = Annotated[dict[str, str], AfterValidator(_reject_credential_keys)]


_T = TypeVar("_T")


def unique_items(items: tuple[_T, ...]) -> tuple[_T, ...]:
    """Enforce JSON Schema `uniqueItems` without silently deduplicating."""
    if len(set(items)) != len(items):
        raise ValueError("items must be unique")
    return items
