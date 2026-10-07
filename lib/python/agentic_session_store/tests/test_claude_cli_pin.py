"""The workspace images and the pinned-binary tests agree on one Claude Code.

The ``test_pinned_*`` suites only run with ``CLAUDE_NATIVE_TEST_BINARY`` set,
so a pin bump that forgets them, or an image left on an older CLI, is invisible
offline. This reads the pins where they are declared and checks them together.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
IMAGES = REPO_ROOT / "implementations/docker/images"
# Images a phase's agent runs `claude -p` in. interactive-tmux drives the TUI
# against screen fixtures recorded at its own pin, so it moves separately.
WORKSPACE_IMAGES = ("omni-agent", "claude-cli")
# Claude Sonnet 5.5 (`claude-sonnet-5-5`) first shipped in 2.1.284.
MINIMUM = (2, 1, 284)


def _pin(image: str) -> str:
    text = (IMAGES / image / "Dockerfile").read_text()
    return re.search(r"^ARG CLAUDE_CLI_VERSION=(\S+)$", text, re.MULTILINE).group(1)


def test_workspace_images_share_one_claude_pin():
    pins = {image: _pin(image) for image in WORKSPACE_IMAGES}
    assert len(set(pins.values())) == 1, pins


def test_claude_pin_runs_sonnet_5_5():
    version = tuple(int(part) for part in _pin("omni-agent").split("."))
    assert version >= MINIMUM


def test_pinned_suites_expect_the_image_pin():
    expected = f"{_pin('omni-agent')} (Claude Code)"
    found = [
        (path.name, version)
        for path in Path(__file__).parent.glob("test_pinned_*.py")
        for version in re.findall(r'"(\S+ \(Claude Code\))"', path.read_text())
    ]
    assert found, "no pinned suite asserts a Claude Code version"
    assert {version for _, version in found} == {expected}, found
