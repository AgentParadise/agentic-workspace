"""Shared building blocks for the EXP-V1-0006 0.2.0 contract models.

Every model is frozen and rejects unknown fields, matching the schemas'
`additionalProperties: false`. Optional schema properties use the `MISSING`
sentinel rather than `None`, so an absent property and an explicit JSON `null`
stay distinct, exactly as the schemas distinguish them.
"""

from __future__ import annotations

import re
from typing import Annotated, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.experimental.missing_sentinel import MISSING

__all__ = [
    "MISSING",
    "ContractModel",
    "CredentialFreeStringMap",
    "JsonInt",
    "NonEmptyStr",
    "RelativePath",
    "unique_items",
]


class ContractModel(BaseModel):
    """Base for every contract model: immutable, closed, strict.

    `pattern` uses Python's `re`, the engine the schemas are validated with, so
    an anchored pattern means the same thing to the model and the schema.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        serialize_by_alias=True,
        regex_engine="python-re",
    )


def _integral_number(value: object) -> object:
    # JSON Schema's "integer" is any number with a zero fractional part, so
    # `1.0` is an integer. Booleans and fractional numbers are left for the
    # strict int check to reject.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# A JSON Schema "integer": strict int, plus integral JSON numbers such as `1.0`.
JsonInt = Annotated[int, BeforeValidator(_integral_number)]


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
