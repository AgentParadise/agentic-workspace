"""What a harness credential presenter hands the sandbox. See ADR-042
(`docs/adrs/042-harness-credential-presentation.md`).

A presenter turns a broker-issued capability into the env and files one
pinned CLI needs to send that capability, and nothing else, to
`<base_url>/...` in the CLI's standard auth header. Syntropic137 owns the
`HarnessCredentialPresenter` Protocol; the presenters in
`agentic_isolation.harnesses.{claude,codex}.credentials` satisfy it
structurally, so neither side imports the other.

The capability is never the upstream credential. The host hashes
`wire_credential(entropy)` and the broker hashes the standard auth value, so
the two must be byte-identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class PresentedFile(BaseModel):
    """One file the sandbox must hold, at `path` relative to `$HOME`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: PurePosixPath
    content: SecretStr
    mode: Annotated[int, Field(ge=0, le=0o777)] = 0o600

    @field_validator("path")
    @classmethod
    def _relative_inside_home(cls, path: PurePosixPath) -> PurePosixPath:
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"presented file path must be relative to $HOME: {path}")
        return path


class PresentedCredentials(BaseModel):
    """The whole presentation: env entries plus files, nothing implicit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    env: Mapping[str, SecretStr] = Field(default_factory=lambda: MappingProxyType({}))
    files: tuple[PresentedFile, ...] = ()

    @field_validator("env")
    @classmethod
    def _read_only(cls, env: Mapping[str, SecretStr]) -> Mapping[str, SecretStr]:
        return MappingProxyType(dict(env))


__all__ = ["PresentedCredentials", "PresentedFile"]
