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

Nested Docker (AgentParadise/agentic-workspace#91). `DockerEngine` layers
Docker CE, the compose and buildx plugins and containerd onto the pinned image
in the E2B build itself, so no second image variant has to pass the release
channel. Provenance stays explicit rather than collapsed into one digest: the
base is still the verified digest, every Docker package is an exact apt
version from Docker's repository, whose key file must hold exactly one primary
key with the pinned fingerprint before any package is trusted, and every
pre-pulled image is pulled by digest and only then tagged. A base image on
another Debian release fails the build (`/etc/os-release` is checked). The
four bootstrap packages (ca-certificates, curl, gnupg, iptables) come from the
base's own Debian sources and are NOT pinned; the build records every
resolved version, theirs included, at `VERSIONS_PATH`, so a template says
what it holds even where the spec could not fix it.

E2B boots every sandbox, including a build step, under its own systemd as
PID 1 even when the image has none (measured live 2026-10-27 on the pinned
omni-agent image), so the package's own `docker.service` starts dockerd: no
start command is needed. The template's ready command is `docker info`, so the
snapshot is taken only once the daemon answers. E2B runs that command as the
template's user, its own `user` unless one is set (measured live 2026-10-27:
`permission denied` on the socket, then a 10 minute build timeout), so the
template sets the image's user (`agent`) first, which also makes it the
sandbox's default user, as the image declares.

The `docker` group is root in the sandbox (a `--privileged` container is
uid 0 with every capability), so a nested-docker workspace has no privilege
boundary between the agent and root inside the VM. The boundary is the
Firecracker VM itself.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PINNED_IMAGE = re.compile(r"(?P<repository>[^@\s]+)@(?P<digest>sha256:[0-9a-f]{64})")


class TemplateSpecError(ValueError):
    """A template spec that would not build a pinned, verified, sized template."""


#: The capability name a provider publishes in `ProviderDescriptor.capabilities`
#: when its template runs a Docker daemon the workspace user can reach.
NESTED_DOCKER = "nested_docker"

#: Docker's Debian apt signing key, "Docker Release (CE deb)", created
#: 2017-02-22: the primary key fingerprint of the key served at
#: `_DOCKER_APT_KEY_URL`, read with `gpg --show-keys` on 2026-10-27.
DOCKER_APT_KEY_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
#: The template's ready command; E2B takes the snapshot once it exits 0.
DOCKER_READY_CMD = "docker info >/dev/null 2>&1"
_DOCKER_APT_KEY_URL = "https://download.docker.com/linux/debian/gpg"
_DOCKER_APT_REPO = "https://download.docker.com/linux/debian"
_DOCKER_KEYRING = "/etc/apt/keyrings/docker.asc"
#: Written at build time: `package=version` for every package the layer installs.
VERSIONS_PATH = "/opt/agentic/nested-docker.versions"
_RECORDED_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-compose-plugin",
    "docker-buildx-plugin",
    "ca-certificates",
    "curl",
    "gnupg",
    "iptables",
)
_APT_VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~-]*")
_SUITE = re.compile(r"[a-z]+")
_USER = re.compile(r"[a-z_][a-z0-9_-]*")
_FINGERPRINT = re.compile(r"[0-9A-F]{40}")
_IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._/:-]*")


@dataclass(frozen=True)
class PrePull:
    """An image pulled by `digest` at build time, then tagged `reference`.

    Compose files name tags (`postgres:16`), so the tag is what makes the
    cached image usable; the digest is what makes it the image that was meant.
    """

    reference: str
    digest: str

    def __post_init__(self) -> None:
        if _IMAGE_REF.fullmatch(self.reference) is None or "@" in self.reference:
            raise TemplateSpecError(f"pre-pull reference {self.reference!r} is not repo[:tag]")
        if _DIGEST.fullmatch(self.digest) is None:
            raise TemplateSpecError(f"pre-pull {self.reference} digest must be sha256:<64 hex>")

    @property
    def repository(self) -> str:
        last = self.reference.rsplit("/", 1)[-1]
        return self.reference.rsplit(":", 1)[0] if ":" in last else self.reference


@dataclass(frozen=True)
class DockerEngine:
    """The Docker layer of a nested-docker template, every input pinned.

    Defaults are the versions proven live (2026-10-21 and 2026-10-27) on
    Debian 12. The spec floor is config, not a constant at the call site.
    """

    docker_ce_version: str = "5:29.9.2-1~debian.12~bookworm"
    containerd_version: str = "2.4.2-3~debian.12~bookworm"
    compose_version: str = "5.6.1-1~debian.12~bookworm"
    buildx_version: str = "0.38.1-1~debian.12~bookworm"
    #: The Debian suite of the base image; the versions above must name it.
    apt_suite: str = "bookworm"
    apt_key_fingerprint: str = DOCKER_APT_KEY_FINGERPRINT
    #: The workspace user added to the `docker` group (the image's USER).
    docker_user: str = "agent"
    pre_pull: tuple[PrePull, ...] = ()
    #: The smallest sandbox Docker is allowed in: 2 vCPU, 4 GiB (issue #91).
    min_cpu_count: int = 2
    min_memory_mb: int = 4096

    def __post_init__(self) -> None:
        versions = (
            self.docker_ce_version,
            self.containerd_version,
            self.compose_version,
            self.buildx_version,
        )
        for version in versions:
            if _APT_VERSION.fullmatch(version) is None:
                raise TemplateSpecError(f"apt version {version!r} is not an exact version")
        if _SUITE.fullmatch(self.apt_suite) is None:
            raise TemplateSpecError(f"apt suite {self.apt_suite!r} is not a Debian codename")
        if any(not version.endswith(f"~{self.apt_suite}") for version in versions):
            raise TemplateSpecError(f"every Docker package version must target {self.apt_suite}")
        if _FINGERPRINT.fullmatch(self.apt_key_fingerprint) is None:
            raise TemplateSpecError("apt_key_fingerprint must be 40 uppercase hex digits")
        if _USER.fullmatch(self.docker_user) is None:
            raise TemplateSpecError(f"docker_user {self.docker_user!r} is not a user name")
        if len({p.reference for p in self.pre_pull}) != len(self.pre_pull):
            raise TemplateSpecError("pre_pull names one reference twice")
        if self.min_cpu_count < 1 or self.min_memory_mb < 1:
            raise TemplateSpecError("the Docker spec floor must be positive")

    def install_commands(self) -> list[str]:
        """Root build steps that install the pinned engine and grant `docker_user`."""
        packages = [
            f"docker-ce={self.docker_ce_version}",
            f"docker-ce-cli={self.docker_ce_version}",
            f"containerd.io={self.containerd_version}",
            f"docker-compose-plugin={self.compose_version}",
            f"docker-buildx-plugin={self.buildx_version}",
        ]
        apt = "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends"
        names = " ".join(_RECORDED_PACKAGES)
        return [
            "set -eu",
            # The pinned versions name a Debian release; refuse any other base.
            f"grep -qx VERSION_CODENAME={self.apt_suite} /etc/os-release",
            "apt-get update",
            f"{apt} ca-certificates curl gnupg iptables",
            "install -m 0755 -d /etc/apt/keyrings",
            f"curl -fsSL {_DOCKER_APT_KEY_URL} -o {_DOCKER_KEYRING}",
            # Trusted only if the file holds exactly one primary key, Docker's:
            # apt trusts every key in a signed-by file, so a second must fail.
            'test "$(gpg --show-keys --with-colons '
            + _DOCKER_KEYRING
            + ' | awk -F: \'$1 == "pub" { getline; print $10 }\')" = '
            + self.apt_key_fingerprint,
            # The suite is checked to be a bare codename, so it needs no quoting.
            "printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] "
            f'{_DOCKER_APT_REPO} {self.apt_suite} stable\\n\' "$(dpkg --print-architecture)"'
            " > /etc/apt/sources.list.d/docker.list",
            "apt-get update",
            f"{apt} {shlex.join(packages)}",
            "rm -rf /var/lib/apt/lists/*",
            f"usermod -aG docker {shlex.quote(self.docker_user)}",
            # What was resolved, the unpinned bootstrap packages included.
            f"dpkg-query -W -f '${{Package}}=${{Version}}\\n' {names} > {VERSIONS_PATH}",
            f"cat {VERSIONS_PATH}",
        ]

    def expected_versions(self) -> dict[str, str]:
        """The pinned package versions `VERSIONS_PATH` must show, by package."""
        return {
            "docker-ce": self.docker_ce_version,
            "docker-ce-cli": self.docker_ce_version,
            "containerd.io": self.containerd_version,
            "docker-compose-plugin": self.compose_version,
            "docker-buildx-plugin": self.buildx_version,
        }

    def pre_pull_commands(self) -> list[str]:
        """Root build steps that pull each image by digest and tag it."""
        commands = ["set -eu", "systemctl start docker", "docker info >/dev/null"]
        for image in self.pre_pull:
            pinned = f"{image.repository}@{image.digest}"
            commands.append(f"docker pull {shlex.quote(pinned)}")
            commands.append(f"docker tag {shlex.quote(pinned)} {shlex.quote(image.reference)}")
        return commands


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
    #: Present for a nested-docker template; None builds the image as it is.
    docker: DockerEngine | None = None
    #: Free root disk to request after the build steps (MiB); None is E2B's default.
    min_free_disk_mb: int | None = None

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
        if self.min_free_disk_mb is not None and self.min_free_disk_mb < 0:
            raise TemplateSpecError("min_free_disk_mb must not be negative")
        if self.docker is not None and (
            self.cpu_count < self.docker.min_cpu_count or self.memory_mb < self.docker.min_memory_mb
        ):
            raise TemplateSpecError(
                f"nested Docker needs at least {self.docker.min_cpu_count} vCPU and "
                f"{self.docker.min_memory_mb} MB; the spec has {self.cpu_count} and "
                f"{self.memory_mb}"
            )


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
    from e2b.template.main import TemplateBuilder, TemplateFinal

    builder = AsyncTemplate().from_image(
        spec.image, username=registry_username, password=registry_password
    )
    template: TemplateBuilder | TemplateFinal = builder
    if spec.docker is not None:
        builder = builder.run_cmd(spec.docker.install_commands(), user="root")
        if spec.docker.pre_pull:
            builder = builder.run_cmd(spec.docker.pre_pull_commands(), user="root")
        # The ready command runs as the template's user: the docker one (#91).
        builder = builder.set_user(spec.docker.docker_user)
        template = builder.set_ready_cmd(DOCKER_READY_CMD)
    info = await AsyncTemplate.build(
        template,
        spec.name,
        cpu_count=spec.cpu_count,
        memory_mb=spec.memory_mb,
        min_free_disk_mb=spec.min_free_disk_mb,
        api_key=api_key,
    )
    return E2BTemplateBuild(template_id=info.template_id, build_id=info.build_id, spec=spec)
