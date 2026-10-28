"""Tests for scripts/version_manifest.py, the /opt/agentic/version.json writer (#93).

What matters: the field set is exactly the one syntropic137 parses (it forbids
extras), the bytes are a pure function of the commit (so the E2B manifest hash
is reproducible), every recorded component was checked against a real binary,
and the release gate refuses a manifest that does not describe the image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import version_manifest as vm
from version_manifest import ManifestError, VersionManifest

#: syntropic137's ImageManifestContent fields (extra="forbid"), read from
#: packages/syn-domain/.../aggregate_execution/ImageManifestValueObjects.py.
SYN137_FIELDS = {
    "provider",
    "provider_version",
    "components",
    "build_commit",
    "built_at",
    "manifest_digest",
}

SHA = "0123456789abcdef0123456789abcdef01234567"


def _manifest(**overrides: object) -> VersionManifest:
    fields: dict[str, object] = {
        "provider": "claude-cli",
        "provider_version": "2.4.0",
        "components": {"codex_cli": "0.160.1", "claude_cli": "2.1.293"},
        "build_commit": SHA,
        "built_at": "2026-10-27T12:00:00Z",
        "manifest_digest": "sha256:" + "a" * 64,
    }
    fields.update(overrides)
    return VersionManifest(**fields)  # type: ignore[arg-type]


def test_field_set_is_exactly_what_syntropic137_parses() -> None:
    assert vm.FIELDS == SYN137_FIELDS
    assert set(json.loads(_manifest().render())) == SYN137_FIELDS


def test_render_is_canonical_and_order_independent() -> None:
    a = _manifest(components={"a": "1", "b": "2"}).render()
    b = _manifest(components={"b": "2", "a": "1"}).render()
    assert a == b
    assert a.endswith("}\n") and not a.endswith("\n\n")
    assert list(json.loads(a)) == sorted(SYN137_FIELDS)
    assert VersionManifest.parse(a).render() == a


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=1),
        lambda d: d.pop("built_at"),
        lambda d: d.update(build_commit="abc123"),
        lambda d: d.update(built_at="2026-10-27T12:00:00+00:00"),
        lambda d: d.update(manifest_digest="a" * 64),
        lambda d: d.update(components={}),
        lambda d: d.update(components={"Claude": "1"}),
        lambda d: d.update(components={"claude_cli": 2}),
        lambda d: d.update(provider_version="2.4"),
    ],
)
def test_parse_refuses_anything_off_schema(mutate) -> None:  # type: ignore[no-untyped-def]
    raw = json.loads(_manifest().render())
    mutate(raw)
    with pytest.raises(ManifestError):
        VersionManifest.parse(json.dumps(raw))


def test_build_args_are_a_pure_function_of_the_commit() -> None:
    head = vm._git("rev-parse", "HEAD")
    first = vm.build_args("toolchain", head)
    assert first == vm.build_args("toolchain", head)
    assert first["AGENTIC_BUILD_COMMIT"] == head
    ct = int(vm._git("show", "-s", "--format=%ct", head))
    from datetime import datetime, timezone

    assert first["AGENTIC_BUILT_AT"] == datetime.fromtimestamp(
        ct, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    yaml_bytes = (vm.IMAGES_DIR / "toolchain" / "manifest.yaml").read_bytes()
    assert (
        first["AGENTIC_MANIFEST_DIGEST"]
        == "sha256:" + hashlib.sha256(yaml_bytes).hexdigest()
    )


def test_build_args_refuse_a_short_sha_and_an_unknown_provider() -> None:
    head = vm._git("rev-parse", "HEAD")
    with pytest.raises(ManifestError):
        vm.build_args("toolchain", head[:12])
    with pytest.raises(ManifestError):
        vm.build_args("interactive-tmux", head)


def test_every_release_image_has_a_semver_manifest() -> None:
    for provider in vm.REQUIRED_COMPONENTS:
        assert vm.provider_version(provider).count(".") == 2


def _rendered_for(provider: str, components: dict[str, str]) -> str:
    args = vm.build_args(provider, vm._git("rev-parse", "HEAD"))
    return VersionManifest(
        provider=args["AGENTIC_PROVIDER"],
        provider_version=args["AGENTIC_PROVIDER_VERSION"],
        components=components,
        build_commit=args["AGENTIC_BUILD_COMMIT"],
        built_at=args["AGENTIC_BUILT_AT"],
        manifest_digest=args["AGENTIC_MANIFEST_DIGEST"],
    ).render()


def test_check_accepts_what_the_build_would_write() -> None:
    head = vm._git("rev-parse", "HEAD")
    text = _rendered_for(
        "scripted-agent", {"scripted_agent": "0.1.0", "playwright": "1.63.0"}
    )
    assert vm.check("scripted-agent", text, head).provider == "scripted-agent"


def test_check_refuses_a_manifest_that_does_not_describe_the_image() -> None:
    head = vm._git("rev-parse", "HEAD")
    good = _rendered_for("claude-cli", {"claude_cli": "1.0.0", "codex_cli": "1.0.0"})
    with pytest.raises(ManifestError, match="build_commit"):
        vm.check("claude-cli", good.replace(head, SHA), head)
    with pytest.raises(ManifestError, match="provider"):
        vm.check("omni-agent", good, head)
    with pytest.raises(ManifestError, match="canonical"):
        vm.check("claude-cli", json.dumps(json.loads(good)), head)
    missing = _rendered_for("claude-cli", {"claude_cli": "1.0.0"})
    with pytest.raises(ManifestError, match="missing"):
        vm.check("claude-cli", missing, head)
    lying = _rendered_for(
        "scripted-agent", {"scripted_agent": "0.1.0", "claude_cli": "2.1.293"}
    )
    with pytest.raises(ManifestError, match="not in this image"):
        vm.check("scripted-agent", lying, head)


# ------------------------------------------------------------------- write


def _fake_binary(tmp_path: Path, name: str, output: str) -> Path:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    # Writes into $HOME, as codex and gh do on --version.
    path.write_text(f'#!/bin/sh\ntouch "$HOME/{name}-was-here"\necho "{output}"\n')
    path.chmod(0o755)
    return path


@pytest.fixture
def build_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key, value in {
        "AGENTIC_PROVIDER": "toolchain",
        "AGENTIC_PROVIDER_VERSION": "1.8.0",
        "AGENTIC_MANIFEST_DIGEST": "sha256:" + "b" * 64,
        "AGENTIC_BUILD_COMMIT": SHA,
        "AGENTIC_BUILT_AT": "2026-10-27T12:00:00Z",
    }.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return tmp_path / "opt" / "version.json"


def _write(out: Path, *, inherit: str | None = None, drop=(), component=()) -> None:  # type: ignore[no-untyped-def]
    vm.write(
        argparse.Namespace(
            out=str(out),
            inherit_from=inherit,
            drop=list(drop),
            component=list(component),
        )
    )


def test_write_inherits_the_base_and_verifies_each_binary(
    build_env: Path, tmp_path: Path
) -> None:
    build_env.parent.mkdir(parents=True)
    build_env.write_text(_manifest(provider="omni-agent").render())
    bun = _fake_binary(tmp_path, "bun", "1.3.14")
    _write(build_env, inherit="omni-agent", component=[f"bun=1.3.14@{bun}"])
    got = VersionManifest.parse(build_env.read_text())
    assert got.provider == "toolchain"
    assert got.components == {
        "claude_cli": "2.1.293",
        "codex_cli": "0.160.1",
        "bun": "1.3.14",
    }
    assert oct(build_env.stat().st_mode & 0o777) == "0o444"
    # The probe ran under a throwaway HOME, not the build's.
    assert list(Path(os.environ["HOME"]).iterdir()) == []


def test_write_drops_what_the_image_removed(build_env: Path) -> None:
    build_env.parent.mkdir(parents=True)
    build_env.write_text(_manifest().render())
    _write(
        build_env,
        inherit="claude-cli",
        drop=["claude_cli", "codex_cli"],
        component=["s=1"],
    )
    assert VersionManifest.parse(build_env.read_text()).components == {"s": "1"}


@pytest.mark.parametrize(
    ("base", "inherit", "drop", "match"),
    [
        (None, "omni-agent", (), "no base manifest"),
        ("claude-cli", "omni-agent", (), "expected 'omni-agent'"),
        ("claude-cli", "claude-cli", ("rtk",), "does not list it"),
    ],
)
def test_write_fails_closed_on_the_base(
    build_env: Path, base, inherit, drop, match
) -> None:  # type: ignore[no-untyped-def]
    if base is not None:
        build_env.parent.mkdir(parents=True)
        build_env.write_text(_manifest(provider=base).render())
    with pytest.raises(ManifestError, match=match):
        _write(build_env, inherit=inherit, drop=drop, component=["x=1"])


def test_write_refuses_a_missing_build_arg(
    build_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTIC_BUILD_COMMIT")
    with pytest.raises(ManifestError, match="build_commit"):
        _write(build_env, component=["x=1"])
    assert not build_env.exists()


@pytest.mark.parametrize(
    ("pin", "output"),
    [
        ("2.1.29", "2.1.293 (Claude Code)"),  # a prefix of the real version
        ("0.160.1", "codex-cli 10.160.1"),
        ("1.3.14", "1.3.14.1"),
    ],
)
def test_a_component_must_report_its_exact_version(
    tmp_path: Path, pin: str, output: str
) -> None:
    binary = _fake_binary(tmp_path, "tool", output)
    with pytest.raises(ManifestError, match="does not report"):
        vm.verify_component(pin, str(binary))


def test_a_component_whose_binary_fails_is_refused(tmp_path: Path) -> None:
    binary = tmp_path / "broken"
    binary.write_text("#!/bin/sh\necho 1.0.0\nexit 3\n")
    binary.chmod(0o755)
    with pytest.raises(ManifestError, match="exited 3"):
        vm.verify_component("1.0.0", str(binary))


def test_cli_build_args_docker_format_is_one_token_per_line() -> None:
    head = vm._git("rev-parse", "HEAD")
    out = subprocess.run(
        [
            sys.executable,
            str(Path(vm.__file__)),
            "build-args",
            "toolchain",
            "--commit",
            head,
            "--format",
            "docker",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert out[0::2] == ["--build-arg"] * 5
    assert [line.split("=", 1)[0] for line in out[1::2]] == [
        "AGENTIC_PROVIDER",
        "AGENTIC_PROVIDER_VERSION",
        "AGENTIC_MANIFEST_DIGEST",
        "AGENTIC_BUILD_COMMIT",
        "AGENTIC_BUILT_AT",
    ]


def test_write_refuses_a_base_built_from_another_commit(build_env: Path) -> None:
    """A stale local base would stamp its old CLI versions with this commit."""
    build_env.parent.mkdir(parents=True)
    stale = _manifest(provider="omni-agent", build_commit="f" * 40)
    build_env.write_text(stale.render())
    with pytest.raises(ManifestError, match="rebuild the base"):
        _write(build_env, inherit="omni-agent", component=["x=1"])
    assert build_env.read_text() == stale.render()
