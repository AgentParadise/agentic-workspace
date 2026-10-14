"""The scripted-agent image's runtime environment, as a fixture's gates see it.

No docker daemon here, so the image's ENV instructions are read from its Dockerfile
and applied to a real ``uv``: a fixture's ``uv sync`` (the gates workload) must use
the fixture's own ``.venv`` and leave the replayer's environment importable.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
DOCKERFILE = PACKAGE.parents[2] / "implementations/docker/images/scripted-agent/Dockerfile"
INSTALL_ROOT = "/opt/scripted-agent"


def _image_env(root: Path) -> dict[str, str]:
    """Every ENV the Dockerfile exports, with the install root moved under ``root``."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text())
    exported: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("ENV "):
            for pair in shlex.split(line[4:]):
                key, _, value = pair.partition("=")
                exported[key] = value.replace(INSTALL_ROOT, str(root))
    return exported


def _uv(args: list[str], env: dict[str, str]) -> None:
    done = subprocess.run(["uv", *args], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_a_fixture_uv_sync_uses_its_own_environment(tmp_path: Path) -> None:
    root = tmp_path / "opt-scripted-agent"
    base = {k: v for k, v in os.environ.items() if not k.startswith(("UV_PROJECT", "VIRTUAL"))}
    replayer = root / "venv"
    _uv(
        ["sync", "--project", str(PACKAGE), "--no-dev", "--frozen"],
        {**base, "UV_PROJECT_ENVIRONMENT": str(replayer)},
    )
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
        "dependencies = []\n"
    )

    _uv(["sync", "--project", str(fixture)], {**base, **_image_env(root)})

    assert (fixture / ".venv").is_dir()
    imported = subprocess.run(
        [str(replayer / "bin" / "python"), "-c", "import scripted_agent.cli"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr


def _install_flags() -> list[str]:
    """The ``uv sync`` arguments of the Dockerfile's replayer install RUN."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text())
    match = re.search(r"uv sync ([^&\n]+)", text)
    assert match is not None, "Dockerfile has no uv sync install"
    return shlex.split(match.group(1))


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_the_image_install_needs_only_the_copied_package(tmp_path: Path) -> None:
    # Same layout as the image: the package alone under <root>/src, no siblings.
    root = tmp_path / "opt-scripted-agent"
    source = root / "src"
    shutil.copytree(PACKAGE, source, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    venv = root / "venv"
    base = {k: v for k, v in os.environ.items() if not k.startswith(("UV_PROJECT", "VIRTUAL"))}

    done = subprocess.run(
        ["uv", "sync", *_install_flags()],
        cwd=source,
        env={**base, "UV_PROJECT_ENVIRONMENT": str(venv)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    imported = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "import scripted_agent.cli"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_the_lock_matches_the_project() -> None:
    # The image installs --frozen, so the lock is checked here, where the
    # test-only sibling source exists.
    _uv(["lock", "--check", "--project", str(PACKAGE)], dict(os.environ))
