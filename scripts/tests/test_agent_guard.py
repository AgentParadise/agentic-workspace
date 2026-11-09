"""Tests for workspace/agent-guard/agent_guard.py, the dcg install baked into images.

What matters: nothing is installed unless the archive matches the pinned
SHA256 AND its minisign signature AND the binary reports the pinned version;
the hook lands in the root-owned harness layers without dropping hooks already
there; and validate fails when the guard does not actually deny.

The release is served from a file:// URL and minisign is a stub on PATH, so the
tests need curl and a POSIX shell but no network and no real dcg.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workspace" / "agent-guard"))

import agent_guard as ag  # noqa: E402

VERSION = "0.16.0"
ASSET = "dcg-x86_64-unknown-linux-musl.tar.xz"

#: A stand-in dcg: reports a version, prints a policy for `dcg config`, and
#: denies hook input that names a destructive command or is not JSON.
GOOD_DCG = """#!/bin/sh
case "$1" in
  --version) echo "@VERSION@"; exit 0 ;;
  config) cat <<'OUT'
General:
  Fail closed: true
  Unverified decision: deny
  Check updates: false

Enabled packs:
  - core.filesystem
  - core.git
  - database.postgresql
  - database.redis
  - containers.docker
  - containers.compose

Disabled packs:
OUT
  exit 0 ;;
esac
input="$(cat)"
case "$input" in
  '{'*'"ls"'*) exit 0 ;;
  '{'*) ;;
esac
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"}}'
"""

#: A dcg that allows everything: what validate must refuse.
OPEN_DCG = """#!/bin/sh
case "$1" in
  --version) echo "@VERSION@"; exit 0 ;;
  config) printf 'Fail closed: false\\nUnverified decision: ask\\nCheck updates: true\\n'; exit 0 ;;
esac
cat >/dev/null
"""


def _script(body: str, version: str = VERSION) -> bytes:
    return body.replace("@VERSION@", version).encode()


def _tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o700
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A file:// release, a pin file for it, a minisign stub and an image root.

    Returns ``publish(archive_bytes, sha=None, signature_ok=True)``.
    """
    rel = tmp_path / "release"
    (rel / f"v{VERSION}").mkdir(parents=True)
    image = tmp_path / "image"
    image.mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    pin_file = tmp_path / "dcg-version.toml"

    monkeypatch.setenv("AGENT_GUARD_ROOT", str(image))
    monkeypatch.setenv("AGENT_GUARD_ARCH", "amd64")
    monkeypatch.setenv("AGENT_GUARD_RELEASE_URL", f"file://{rel}")
    monkeypatch.setenv("AGENT_GUARD_OWNER_UID", str(os.getuid()))
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(ag, "VERSION_FILE", pin_file)

    def publish(
        archive: bytes, sha: str | None = None, signature_ok: bool = True
    ) -> Path:
        (rel / f"v{VERSION}" / ASSET).write_bytes(archive)
        (rel / f"v{VERSION}" / f"{ASSET}.minisig").write_text(
            "untrusted comment: stub\n"
        )
        pinned = sha or hashlib.sha256(archive).hexdigest()
        pin_file.write_text(
            f'version = "{VERSION}"\nrelease_url = "https://example.invalid"\n'
            'minisign_public_key = "RWStub"\n'
            f'[assets.amd64]\nname = "{ASSET}"\nsha256 = "{pinned}"\n'
        )
        minisign = stubs / "minisign"
        minisign.write_text(
            "#!/bin/sh\n"
            + (
                "exit 0\n"
                if signature_ok
                else "echo 'Signature verification failed' >&2; exit 1\n"
            )
        )
        minisign.chmod(0o755)
        return image

    return publish


def _installed(image: Path) -> list[str]:
    return sorted(str(p.relative_to(image)) for p in image.rglob("*") if p.is_file())


def test_install_writes_binary_policy_and_both_registrations(release) -> None:
    image = release(_tarball({"dcg": _script(GOOD_DCG)}))
    assert ag.main(["install", "--expect-version", VERSION]) == 0

    assert _installed(image) == [
        "etc/claude-code/managed-settings.json",
        "etc/codex/requirements.toml",
        "etc/dcg/config.toml",
        "usr/local/bin/dcg",
    ]
    assert stat.S_IMODE((image / "usr/local/bin/dcg").stat().st_mode) == 0o755
    for f in (
        "etc/dcg/config.toml",
        "etc/claude-code/managed-settings.json",
        "etc/codex/requirements.toml",
    ):
        assert stat.S_IMODE((image / f).stat().st_mode) == 0o644
    assert (image / "etc/dcg/config.toml").read_bytes() == ag.POLICY_FILE.read_bytes()

    claude = json.loads((image / "etc/claude-code/managed-settings.json").read_text())
    assert claude == {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": ag.HOOK_COMMAND}],
                }
            ]
        }
    }
    codex = tomllib.loads((image / "etc/codex/requirements.toml").read_text())
    assert codex == {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "^Bash$",
                    "hooks": [{"type": "command", "command": ag.HOOK_COMMAND}],
                }
            ]
        }
    }


@pytest.mark.parametrize(
    ("case", "kwargs", "argv_extra"),
    [
        ("sha256 mismatch", {"sha": "0" * 64}, []),
        ("bad signature", {"signature_ok": False}, []),
        ("ARG and pin disagree", {}, ["--expect-version", "0.15.0"]),
    ],
)
def test_a_failed_check_installs_nothing(
    release, case: str, kwargs: dict, argv_extra: list[str]
) -> None:
    image = release(_tarball({"dcg": _script(GOOD_DCG)}), **kwargs)
    assert ag.main(["install", *argv_extra]) == 1, case
    assert _installed(image) == [], case


def test_a_binary_reporting_another_version_installs_nothing(release) -> None:
    image = release(_tarball({"dcg": _script(GOOD_DCG, version="0.15.9")}))
    assert ag.main(["install"]) == 1
    assert _installed(image) == []


def test_missing_minisign_installs_nothing(
    release, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = release(_tarball({"dcg": _script(GOOD_DCG)}))
    (tmp_path / "stubs" / "minisign").unlink()
    monkeypatch.setattr(ag.shutil, "which", lambda name: None)
    assert ag.main(["install"]) == 1
    assert _installed(image) == []


@pytest.mark.parametrize(
    "members",
    [{"nested/dcg": b"x"}, {"dcg": b"x", "./dcg": b"y"}, {"README": b"x"}],
)
def test_an_archive_without_exactly_one_root_dcg_installs_nothing(
    release, members: dict[str, bytes]
) -> None:
    image = release(_tarball(members))
    assert ag.main(["install"]) == 1
    assert _installed(image) == []


def test_claude_registration_keeps_existing_managed_hooks(release) -> None:
    image = release(_tarball({"dcg": _script(GOOD_DCG)}))
    settings = image / "etc/claude-code/managed-settings.json"
    settings.parent.mkdir(parents=True)
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook"}]}
    settings.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [other]}}))

    assert ag.main(["install"]) == 0
    assert ag.main(["install"]) == 0  # idempotent: one dcg entry, not two

    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    groups = data["hooks"]["PreToolUse"]
    assert other in groups
    assert [h["command"] for g in groups for h in g["hooks"]].count(
        ag.HOOK_COMMAND
    ) == 1


def test_codex_registration_refuses_an_unmanaged_requirements_file(release) -> None:
    image = release(_tarball({"dcg": _script(GOOD_DCG)}))
    requirements = image / "etc/codex/requirements.toml"
    requirements.parent.mkdir(parents=True)
    requirements.write_text('allowed_sandbox_modes = ["read-only"]\n')
    assert ag.main(["install"]) == 1
    assert requirements.read_text() == 'allowed_sandbox_modes = ["read-only"]\n'


def test_validate_passes_a_guard_that_denies(release, tmp_path: Path) -> None:
    release(_tarball({"dcg": _script(GOOD_DCG)}))
    assert ag.main(["install"]) == 0
    assert ag.validate(tmp_path) == []


def test_validate_fails_a_guard_that_allows_everything(release, tmp_path: Path) -> None:
    image = release(_tarball({"dcg": _script(GOOD_DCG)}))
    assert ag.main(["install"]) == 0
    (image / "usr/local/bin/dcg").write_bytes(_script(OPEN_DCG))

    problems = ag.validate(tmp_path)
    for command in ag.BAD_COMMANDS:
        for harness in ag.HARNESSES:
            assert f"{harness}: known-bad command was not denied: {command}" in problems
    assert "unparseable hook input was allowed (not fail-closed)" in problems
    assert any("'Fail closed' is false" in p for p in problems)


def test_validate_fails_a_writable_or_unregistered_install(
    release, tmp_path: Path
) -> None:
    image = release(_tarball({"dcg": _script(GOOD_DCG)}))
    assert ag.main(["install"]) == 0
    (image / "etc/dcg/config.toml").chmod(0o666)
    (image / "etc/codex/requirements.toml").write_text(ag.MARKER + "\n")

    problems = ag.validate(tmp_path)
    assert any("config.toml is group/world writable" in p for p in problems)
    assert f"codex: dcg not registered in {ag.CODEX_REQUIREMENTS}" in problems


def test_payloads_match_each_harness_protocol(tmp_path: Path) -> None:
    claude = json.loads(ag.payload("claude", "ls", tmp_path))
    codex = json.loads(ag.payload("codex", "ls", tmp_path))
    assert claude["tool_name"] == codex["tool_name"] == "Bash"
    assert "turn_id" not in claude and "tool_use_id" not in claude
    assert {"turn_id", "tool_use_id"} <= set(codex)


def test_pin_covers_both_image_arches_and_matches_the_dockerfiles() -> None:
    pin = tomllib.loads((ROOT / "workspace/agent-guard/dcg-version.toml").read_text())
    assert set(pin["assets"]) == {"amd64", "arm64"}
    for image in ("omni-agent", "claude-cli"):
        dockerfile = (
            ROOT / "implementations/docker/images" / image / "Dockerfile"
        ).read_text()
        assert f"ARG DCG_VERSION={pin['version']}\n" in dockerfile, image
        manifest = (
            ROOT / "implementations/docker/images" / image / "manifest.yaml"
        ).read_text()
        assert f'- name: dcg\n      version: "{pin["version"]}"\n' in manifest, image
