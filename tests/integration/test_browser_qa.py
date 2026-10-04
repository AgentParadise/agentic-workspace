"""Headless browser QA works in the BUILT image, as the agent, offline.

syntropic137/syntropic137#1028: an agent validating a UI change needs a
screenshot, and cannot install Chromium's system libraries itself (uid 1000,
no root, no apt lists). The image bakes them, the pinned Playwright CLI and
the headless shell. A Dockerfile diff proves none of that; this does.

Each case runs the image the way production does (shared entrypoint, tmpfs
$HOME, default USER) with --network none, so anything the image failed to
bake cannot be fetched at run time. It serves two pages on localhost, the
shape of `npx playwright screenshot ... http://localhost:5173 out.png`, and
checks the screenshots are real PNGs at the default viewport whose bytes
depend on what was rendered.

The omni case carries "omni" in its id so the release workflow's
`-k omni` rerun against the toolchain image covers that image too.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

PLAYWRIGHT_VERSION = "1.63.0"

IMAGES = [
    pytest.param(
        os.getenv("AGENTIC_WORKSPACE_IMAGE", "agentic-workspace-claude-cli:latest"),
        id="claude-cli",
    ),
    pytest.param(
        os.getenv("AGENTIC_OMNI_IMAGE", "omni-agent-workspace:latest"),
        id="omni",
    ),
]

SMOKE = r"""
set -euo pipefail
fail() { echo "BROWSER_QA_FAIL: $*" >&2; exit 1; }

[ "$(id -u)" = 1000 ] || fail "running as uid $(id -u), not the agent"
[ "$(npx --no-install playwright --version)" = "Version ${EXPECTED_PLAYWRIGHT}" ] \
  || fail "npx playwright is not the pinned ${EXPECTED_PLAYWRIGHT}"

site="$(mktemp -d)"
printf '<body style="margin:0;background:#d01010"><h1>red</h1></body>' > "$site/red.html"
printf '<body style="margin:0;background:#1010d0"><h1>blue</h1></body>' > "$site/blue.html"
python3 -m http.server 5173 --bind 127.0.0.1 --directory "$site" >/dev/null 2>&1 &
for _ in $(seq 50); do
  python3 -c 'import urllib.request; urllib.request.urlopen("http://localhost:5173/red.html")' \
    2>/dev/null && break
  sleep 0.1
done

out="$(mktemp -d)"
for page in red blue; do
  npx --no-install playwright screenshot --browser chromium \
    "http://localhost:5173/${page}.html" "$out/${page}.png"
done

python3 - "$out/red.png" "$out/blue.png" <<'PY'
import struct, sys
shots = [open(p, "rb").read() for p in sys.argv[1:]]
for path, data in zip(sys.argv[1:], shots):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1280, 720), f"{path} is {width}x{height}"
assert shots[0] != shots[1], "red and blue pages produced identical screenshots"
print("BROWSER_QA_PASS", *(len(d) for d in shots))
PY
"""


def _available(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    return r.returncode == 0


@pytest.mark.integration
@pytest.mark.parametrize("image", IMAGES)
def test_agent_can_screenshot_a_local_page_offline(image: str):
    if not _available(image):
        pytest.skip(f"{image} not built")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--tmpfs=/home/agent:rw,exec,nosuid,size=128m,uid=1000,gid=1000",
            "-e",
            f"EXPECTED_PLAYWRIGHT={PLAYWRIGHT_VERSION}",
            image,
            "bash",
            "-c",
            SMOKE,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BROWSER_QA_PASS" in result.stdout, result.stdout + result.stderr
