#!/usr/bin/env python3
"""The workspace image version manifest, /opt/agentic/version.json (#93).

One file, three jobs, so the schema lives in exactly one place:

- ``build-args PROVIDER`` runs on the HOST (release workflow, integration gate,
  ``scripts/build-provider.py``). It prints the build args every workspace
  image's Dockerfile requires: the provider name and version and the sha256 of
  its ``manifest.yaml``, the source commit, and that commit's committer time.
- ``write`` runs INSIDE the image build (bind-mounted, never shipped). It
  checks every pinned component against the binary that is actually on PATH,
  optionally carries the base image's components forward, and writes the file.
- ``check PROVIDER`` runs on the HOST against a built image's file (stdin). It
  is the gate the integration job and the release smokes run.

The content is deterministic per source commit: ``built_at`` is the commit's
committer time, never the wall clock, every component is an exact pin, and the
JSON is serialised with sorted keys. So two builds of one commit write the same
bytes, and ``manifest_hash`` in agentic_isolation.providers.e2b_template (the
sha256 of the file) is reproducible from the commit alone.

The field set is exactly what syntropic137 parses (``ImageManifestContent``,
``extra="forbid"``): adding a field here breaks that reader. Change both sides
together.

Standard library only: it runs under the image's /opt/venv python and under
``uv run --no-project`` on the host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: Where every workspace image carries the manifest.
VERSION_JSON_PATH = "/opt/agentic/version.json"

ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = ROOT / "implementations" / "docker" / "images"

#: The images that ship a manifest, in release order. Each must name these
#: components, checked by ``check``; anything else an image lists is extra.
REQUIRED_COMPONENTS: dict[str, frozenset[str]] = {
    "claude-cli": frozenset({"claude_cli", "codex_cli"}),
    "omni-agent": frozenset({"claude_cli", "codex_cli", "apss_session_exporter"}),
    "toolchain": frozenset(
        {"claude_cli", "codex_cli", "apss_session_exporter", "rustup", "bun"}
    ),
    "scripted-agent": frozenset({"scripted_agent"}),
}

#: Components an image must NOT list: scripted-agent uninstalls both real
#: harnesses, so inheriting claude-cli's entries for them would be a lie.
FORBIDDEN_COMPONENTS: dict[str, frozenset[str]] = {
    "scripted-agent": frozenset({"claude_cli", "codex_cli"}),
}

FIELDS = frozenset(
    {
        "provider",
        "provider_version",
        "components",
        "build_commit",
        "built_at",
        "manifest_digest",
    }
)

_PROVIDER = re.compile(r"^[a-z][a-z0-9-]*$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}(-dirty)?$")
_BUILT_AT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMPONENT = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")
_MANIFEST_VERSION = re.compile(r'^version: *"?([^" ]*)"?$', re.MULTILINE)


class ManifestError(ValueError):
    """A manifest, or an input to one, that is not what it must be."""


@dataclass(frozen=True)
class VersionManifest:
    provider: str
    provider_version: str
    components: dict[str, str]
    build_commit: str
    built_at: str
    manifest_digest: str

    def __post_init__(self) -> None:
        _require(_PROVIDER, self.provider, "provider")
        _require(_SEMVER, self.provider_version, "provider_version")
        _require(_COMMIT, self.build_commit, "build_commit")
        _require(_BUILT_AT, self.built_at, "built_at")
        _require(_DIGEST, self.manifest_digest, "manifest_digest")
        if not isinstance(self.components, dict) or not self.components:
            raise ManifestError("components must be a non-empty object")
        for name, version in self.components.items():
            _require(_COMPONENT, name, "component name")
            if not isinstance(version, str):
                raise ManifestError(f"component {name} version must be a string")
            _require(_VERSION, version, f"component {name} version")

    def render(self) -> str:
        """The exact bytes written to the image: sorted keys, one trailing newline."""
        body = {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "components": dict(sorted(self.components.items())),
            "build_commit": self.build_commit,
            "built_at": self.built_at,
            "manifest_digest": self.manifest_digest,
        }
        return json.dumps(body, indent=2, sort_keys=True) + "\n"

    @classmethod
    def parse(cls, text: str) -> VersionManifest:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise ManifestError(f"not JSON: {e}") from e
        if not isinstance(raw, dict):
            raise ManifestError("not a JSON object")
        keys = set(raw)
        if keys != FIELDS:
            missing = sorted(FIELDS - keys)
            extra = sorted(keys - FIELDS)
            raise ManifestError(f"field set differs: missing={missing} extra={extra}")
        for key in FIELDS - {"components"}:
            if not isinstance(raw[key], str):
                raise ManifestError(f"{key} must be a string")
        return cls(**raw)


def _require(pattern: re.Pattern[str], value: object, what: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ManifestError(f"{what} is not valid: {value!r}")


# --------------------------------------------------------------------------- host


def manifest_yaml(provider: str) -> Path:
    path = IMAGES_DIR / provider / "manifest.yaml"
    if provider not in REQUIRED_COMPONENTS or not path.is_file():
        raise ManifestError(f"unknown provider: {provider!r}")
    return path


def provider_version(provider: str) -> str:
    """The manifest.yaml ``version``, read the way the release workflow reads it."""
    match = _MANIFEST_VERSION.search(manifest_yaml(provider).read_text())
    if match is None or _SEMVER.fullmatch(match.group(1)) is None:
        raise ManifestError(f"{provider}/manifest.yaml has no X.Y.Z version")
    return match.group(1)


def manifest_digest(provider: str) -> str:
    """sha256 of the manifest.yaml bytes. Not an OCI digest."""
    return "sha256:" + hashlib.sha256(manifest_yaml(provider).read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def source_commit(commit: str | None) -> str:
    """The full commit sha. With none given, HEAD, marked ``-dirty`` when tracked
    files differ from it, so a local build of uncommitted work never claims a
    commit it was not built from."""
    if commit is not None:
        full = _git("rev-parse", "--verify", f"{commit}^{{commit}}")
        if full != commit:
            raise ManifestError(f"--commit must be a full 40-hex sha: {commit!r}")
        return full
    head = _git("rev-parse", "HEAD")
    dirty = (
        subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=ROOT).returncode
        != 0
    )
    return f"{head}-dirty" if dirty else head


def commit_time(commit: str) -> str:
    """The commit's committer time in UTC, the only clock ``built_at`` reads."""
    sha = commit.removesuffix("-dirty")
    epoch = int(_git("show", "-s", "--format=%ct", sha))
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_args(provider: str, commit: str | None) -> dict[str, str]:
    full = source_commit(commit)
    return {
        "AGENTIC_PROVIDER": provider,
        "AGENTIC_PROVIDER_VERSION": provider_version(provider),
        "AGENTIC_MANIFEST_DIGEST": manifest_digest(provider),
        "AGENTIC_BUILD_COMMIT": full,
        "AGENTIC_BUILT_AT": commit_time(full),
    }


def check(provider: str, text: str, commit: str | None) -> VersionManifest:
    """The manifest an image of ``provider`` built from ``commit`` must carry."""
    got = VersionManifest.parse(text)
    if text != got.render():
        raise ManifestError(
            "not in canonical form (sorted keys, indent 2, one trailing newline)"
        )
    want = build_args(provider, commit)
    expected = {
        "provider": want["AGENTIC_PROVIDER"],
        "provider_version": want["AGENTIC_PROVIDER_VERSION"],
        "manifest_digest": want["AGENTIC_MANIFEST_DIGEST"],
        "build_commit": want["AGENTIC_BUILD_COMMIT"],
        "built_at": want["AGENTIC_BUILT_AT"],
    }
    for key, value in expected.items():
        if getattr(got, key) != value:
            raise ManifestError(f"{key} is {getattr(got, key)!r}, expected {value!r}")
    missing = REQUIRED_COMPONENTS[provider] - set(got.components)
    if missing:
        raise ManifestError(f"components missing: {sorted(missing)}")
    forbidden = FORBIDDEN_COMPONENTS.get(provider, frozenset()) & set(got.components)
    if forbidden:
        raise ManifestError(
            f"components that are not in this image: {sorted(forbidden)}"
        )
    return got


# -------------------------------------------------------------------- in image


def verify_component(version: str, binary: str) -> None:
    """Run ``binary --version`` and require ``version`` as a whole token.

    Under a throwaway HOME: harnesses write state on --version (codex creates
    ~/.codex, gh a device id), and a root-owned leftover under /home/agent
    breaks the agent user at runtime.
    """
    with tempfile.TemporaryDirectory(prefix="agentic-version-") as home:
        env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
        env["HOME"] = home
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, env=env, timeout=120
        )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise ManifestError(
            f"{binary} --version exited {result.returncode}: {output.strip()}"
        )
    if (
        re.search(rf"(?<![0-9A-Za-z.]){re.escape(version)}(?![0-9A-Za-z.])", output)
        is None
    ):
        raise ManifestError(
            f"{binary} --version does not report {version}: {output.strip()}"
        )


def parse_component(spec: str) -> tuple[str, str, str | None]:
    """``name=version`` or ``name=version@binary`` (verified by running it)."""
    name, sep, rest = spec.partition("=")
    if not sep:
        raise ManifestError(f"component must be name=version[@binary]: {spec!r}")
    version, _, binary = rest.partition("@")
    _require(_COMPONENT, name, "component name")
    _require(_VERSION, version, f"component {name} version")
    return name, version, binary or None


def write(args: argparse.Namespace) -> None:
    out = Path(args.out)
    components: dict[str, str] = {}
    if args.inherit_from is not None:
        if not out.is_file():
            raise ManifestError(
                f"no base manifest at {out} to inherit from {args.inherit_from}"
            )
        base = VersionManifest.parse(out.read_text())
        if base.provider != args.inherit_from:
            raise ManifestError(
                f"base manifest is {base.provider!r}, expected {args.inherit_from!r}"
            )
        # The inherited components are only this commit's if the base is: a
        # stale local base would otherwise stamp old CLI versions with this
        # commit, and two builds of one commit would differ.
        commit = os.environ.get("AGENTIC_BUILD_COMMIT", "")
        if base.build_commit != commit:
            raise ManifestError(
                f"base {base.provider} was built from {base.build_commit}, not "
                f"{commit!r}; rebuild the base from this commit"
            )
        components.update(base.components)
    for name in args.drop:
        if components.pop(name, None) is None:
            raise ManifestError(f"--drop {name}: the base does not list it")
    for spec in args.component:
        name, version, binary = parse_component(spec)
        if binary is not None:
            verify_component(version, binary)
        components[name] = version
    manifest = VersionManifest(
        provider=os.environ.get("AGENTIC_PROVIDER", ""),
        provider_version=os.environ.get("AGENTIC_PROVIDER_VERSION", ""),
        components=components,
        build_commit=os.environ.get("AGENTIC_BUILD_COMMIT", ""),
        built_at=os.environ.get("AGENTIC_BUILT_AT", ""),
        manifest_digest=os.environ.get("AGENTIC_MANIFEST_DIGEST", ""),
    )
    if manifest.provider not in REQUIRED_COMPONENTS:
        raise ManifestError(
            f"AGENTIC_PROVIDER is not a workspace image: {manifest.provider!r}"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(manifest.render())
    tmp.chmod(0o444)
    tmp.replace(out)
    print(manifest.render(), end="")


# ------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_args = sub.add_parser("build-args", help="print the build args for PROVIDER")
    p_args.add_argument("provider")
    p_args.add_argument("--commit", help="full sha (default: HEAD, -dirty if modified)")
    p_args.add_argument(
        "--format",
        choices=["lines", "docker"],
        default="lines",
        help="lines: KEY=VALUE per line; docker: --build-arg and KEY=VALUE, one token per line",
    )

    p_write = sub.add_parser("write", help="write the manifest inside an image build")
    p_write.add_argument("--out", default=VERSION_JSON_PATH)
    p_write.add_argument(
        "--inherit-from", help="carry the base image's components; its provider"
    )
    p_write.add_argument(
        "--drop", action="append", default=[], help="a base component to remove"
    )
    p_write.add_argument(
        "--component", action="append", default=[], help="name=version[@binary]"
    )

    p_check = sub.add_parser("check", help="check a manifest read from stdin")
    p_check.add_argument("provider")
    p_check.add_argument(
        "--commit", help="full sha (default: HEAD, -dirty if modified)"
    )

    args = parser.parse_args(argv)
    try:
        if args.cmd == "build-args":
            for key, value in build_args(args.provider, args.commit).items():
                if args.format == "docker":
                    print("--build-arg")
                print(f"{key}={value}")
        elif args.cmd == "write":
            write(args)
        else:
            got = check(args.provider, sys.stdin.read(), args.commit)
            print(
                f"version.json OK: {got.provider} {got.provider_version} "
                f"{got.build_commit[:12]} {json.dumps(got.components, sort_keys=True)}"
            )
    except (ManifestError, subprocess.CalledProcessError, OSError) as e:
        print(f"version manifest: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
