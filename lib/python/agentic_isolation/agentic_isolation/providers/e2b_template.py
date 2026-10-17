"""An E2B template built from one pinned, verified workspace image.

syntropic137/syntropic137#1915, step E of the remote tier plan in
docs/research/2026-10-08-remote-executor-e2b.md (syntropic137). A template is
the E2B equivalent of the image a Docker workspace runs. It is built from a
digest-pinned image reference only, and the build records two hashes that
must never be compared with each other:

- `verified_oci_digest`: the image digest the caller verified (cosign) before
  asking for the build. The build refuses a reference whose digest differs.
- `expected_manifest_hash`: the sha256 of the image's version manifest,
  `/opt/agentic/version.json` (the file the platform reads; research doc
  issue C), which a launched workspace's manifest is checked
  against. `manifest_hash` computes it from the file.

CPU and RAM are pinned on the spec because E2B's default RAM is documented
inconsistently (research doc section 2). `from_image` with registry
credentials and `cpu_count`/`memory_mb` on build are documented at
https://e2b.dev/docs/template/base-image,
https://e2b.dev/docs/template/private-registries and
https://e2b.dev/docs/template/build (accessed 2026-10-17). Building from a
private GHCR digest has NOT been run (Q5); tests/integration/test_e2b_live.py
holds that proof.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PINNED_IMAGE = re.compile(r"(?P<repository>[^@\s]+)@(?P<digest>sha256:[0-9a-f]{64})")


class TemplateSpecError(ValueError):
    """A template spec that would not build a pinned, verified, sized template."""


@dataclass(frozen=True)
class E2BTemplateSpec:
    """Everything one template build needs, checked at construction."""

    name: str
    #: `repository@sha256:<64 hex>`. A tag alone is refused.
    image: str
    verified_oci_digest: str
    #: `sha256:<64 hex>` of the image's version.json, never an OCI digest.
    expected_manifest_hash: str
    cpu_count: int
    memory_mb: int

    def __post_init__(self) -> None:
        if not self.name:
            raise TemplateSpecError("a template needs a name")
        pinned = _PINNED_IMAGE.fullmatch(self.image)
        if pinned is None:
            raise TemplateSpecError(
                f"image {self.image!r} is not pinned as repository@sha256:<digest>"
            )
        if pinned["digest"] != self.verified_oci_digest:
            raise TemplateSpecError(
                f"image digest {pinned['digest']} is not the verified digest "
                f"{self.verified_oci_digest}"
            )
        if _DIGEST.fullmatch(self.expected_manifest_hash) is None:
            raise TemplateSpecError("expected_manifest_hash must be sha256:<64 hex>")
        if self.expected_manifest_hash == self.verified_oci_digest:
            raise TemplateSpecError(
                "expected_manifest_hash equals the OCI digest; it must hash version.json"
            )
        if self.cpu_count < 1 or self.memory_mb < 1:
            raise TemplateSpecError("cpu_count and memory_mb must be pinned to positive values")


@dataclass(frozen=True)
class E2BTemplateBuild:
    """The receipt of one build: what E2B named it, and what it was built from."""

    template_id: str
    build_id: str
    spec: E2BTemplateSpec


def manifest_hash(manifest_path: Path) -> str:
    """`sha256:<hex>` of a version manifest's exact bytes."""
    return "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()


async def build_template(
    spec: E2BTemplateSpec,
    *,
    registry_username: str | None = None,
    registry_password: str | None = None,
    api_key: str | None = None,
) -> E2BTemplateBuild:
    """Build `spec` on E2B and return its receipt. Needs the `e2b` extra and a key."""
    from e2b import AsyncTemplate

    template = AsyncTemplate().from_image(
        spec.image, username=registry_username, password=registry_password
    )
    info = await AsyncTemplate.build(
        template,
        spec.name,
        cpu_count=spec.cpu_count,
        memory_mb=spec.memory_mb,
        api_key=api_key,
    )
    return E2BTemplateBuild(template_id=info.template_id, build_id=info.build_id, spec=spec)
