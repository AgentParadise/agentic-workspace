"""A degraded workspace removes the capture hooks it installed (#27).

The hooks are fail closed: a child launch whose intent cannot be recorded is
denied. That is right while capture is on. When the session-store capability
degrades, capture is OFF, so a hook left behind could deny children in a
workspace reported as running without capture. Removal must take exactly
what install added, keep everything else, and keep every other Codex hook's
trust valid even though trust is keyed by group index.
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import tomlkit

from agentic_session_store.claude_hook_config import (
    CAPTURE_GROUPS as CLAUDE_GROUPS,
)
from agentic_session_store.codex_hook_config import (
    CAPTURE_GROUPS as CODEX_GROUPS,
)
from agentic_session_store.codex_hook_config import (
    CAPTURE_HASHES,
)
from agentic_session_store.install_child_hooks import install, uninstall

USER_GROUP = {"matcher": "Bash", "hooks": [{"type": "command", "command": "user-hook"}]}


def _commands(groups: list[dict]) -> list[str]:
    return [h["command"] for g in groups for h in g["hooks"]]


def _capture_commands(document: dict) -> list[str]:
    return [
        command
        for groups in document.get("hooks", {}).values()
        if isinstance(groups, list)
        for command in _commands(groups)
        if "agentic_session_store" in command
    ]


def test_claude_uninstall_removes_only_what_install_added(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = {"enabledPlugins": {"x@y": True}, "hooks": {"PreToolUse": [USER_GROUP]}}
    path.write_text(json.dumps(original))
    install(path, harness="claude")
    assert _capture_commands(json.loads(path.read_text()))

    assert uninstall(path, harness="claude") is True
    document = json.loads(path.read_text())
    assert _capture_commands(document) == []
    assert document["enabledPlugins"] == {"x@y": True}
    assert document["hooks"]["PreToolUse"] == [USER_GROUP]
    # Event lists install created and uninstall emptied do not linger.
    for event in CLAUDE_GROUPS:
        if event != "PreToolUse":
            assert event not in document["hooks"]
    # Idempotent: nothing left to remove is not a change.
    before = path.stat()
    assert uninstall(path, harness="claude") is False
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_codex_uninstall_keeps_user_hooks_and_their_trust(tmp_path: Path) -> None:
    """Removing a group shifts later groups' indices, so their trust moves too."""
    path = tmp_path / "config.toml"
    path.write_text('# keep\nmodel="m"\n')
    install(path)
    document = tomllib.loads(path.read_text())
    resolved = path.resolve()
    # A user hook added AFTER capture, trusted at index 1.
    document["hooks"]["PreToolUse"].append(USER_GROUP)
    document["hooks"]["state"][f"{resolved}:pre_tool_use:1:0"] = {
        "trusted_hash": "sha256:user"
    }
    path.write_text("# keep\n" + tomlkit.dumps(document))

    assert uninstall(path) is True
    text = path.read_text()
    parsed = tomllib.loads(text)
    assert "# keep" in text and parsed["model"] == "m"
    assert _capture_commands(parsed) == []
    assert parsed["hooks"]["PreToolUse"] == [USER_GROUP]
    for event in CODEX_GROUPS:
        if event != "PreToolUse":
            assert event not in parsed["hooks"]
    state = parsed["hooks"]["state"]
    # The capture handlers' trust is gone; the user hook's moved to index 0.
    assert state == {f"{resolved}:pre_tool_use:0:0": {"trusted_hash": "sha256:user"}}
    for label, digest in CAPTURE_HASHES.values():
        assert digest not in text, label
    assert uninstall(path) is False


def test_partial_install_is_removed(tmp_path: Path) -> None:
    """init.sh installs Codex, then Claude. If the Claude step failed, only
    Codex is installed, and that half must still come out."""
    codex = tmp_path / "codex" / "config.toml"
    claude = tmp_path / "claude" / "settings.json"
    install(codex)
    assert uninstall(codex) is True
    assert uninstall(claude, harness="claude") is False
    assert not claude.exists(), "uninstall must not create a config"
    assert _capture_commands(tomllib.loads(codex.read_text())) == []


def test_a_capture_group_the_user_edited_is_left_alone(tmp_path: Path) -> None:
    """Only exact groups this package wrote are removed; anything else is the
    operator's, even if it mentions the recorder."""
    path = tmp_path / "settings.json"
    edited = {
        "matcher": "^(Agent|Task)$",
        "hooks": [
            {"type": "command", "command": "custom agentic_session_store wrapper"}
        ],
    }
    path.write_text(json.dumps({"hooks": {"PreToolUse": [edited]}}))
    assert uninstall(path, harness="claude") is False
    assert json.loads(path.read_text())["hooks"]["PreToolUse"] == [edited]


@pytest.mark.parametrize("kind", ["invalid", "symlink"])
def test_invalid_targets_remain_unchanged(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "config.toml"
    original = b"invalid={"
    if kind == "symlink":
        target = tmp_path / "original"
        target.write_bytes(original)
        path.symlink_to(target)
    else:
        path.write_bytes(original)
    with pytest.raises((OSError, ValueError, TypeError)):
        uninstall(path)
    assert (path.resolve() if kind == "symlink" else path).read_bytes() == original


def test_cli_uninstall_needs_no_active_contract(tmp_path: Path) -> None:
    """The degrade path runs uninstall exactly when capture is being turned
    off, so it must not demand the active contract install requires."""
    path = tmp_path / "settings.json"
    install(path, harness="claude")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTIC_SESSION")}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_session_store.install_child_hooks",
            str(path),
            "--harness",
            "claude",
            "--uninstall",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert _capture_commands(json.loads(path.read_text())) == []
