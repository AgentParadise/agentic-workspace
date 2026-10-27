"""Live-test secrets cannot reach a failure report (AgentParadise/agentic-workspace#91).

Every secret below is invented for the test; none is, or resembles, a real key
beyond its shape.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib

from tests.integration import secret_guard

_PACKAGE = Path(__file__).resolve().parents[1]
_INTEGRATION = _PACKAGE / "tests" / "integration"
# Invented: e2b_ followed by 40 hex digits, the key's shape and nothing more.
_FAKE_KEY = "e2b_" + "0123456789abcdef" * 2 + "deadbeef"
_FAKE_TOKEN = "ghp_" + "Fake0nlyForTest" * 2


def test_no_integration_test_holds_or_passes_a_secret() -> None:
    files = sorted(_INTEGRATION.glob("*.py"))
    assert any(f.name == "test_e2b_live.py" for f in files)
    found = [v for f in files for v in secret_guard.violations(f.read_text(), f.name)]
    assert found == []


@pytest.mark.parametrize(
    "planted",
    [
        f'await build_template(spec, api_key="{_FAKE_KEY}")',
        f'build_template(spec, registry_password="{_FAKE_TOKEN}")',
        "build_template(spec, api_key=key)",
        'from_image(image, username="u", password=token)',
        'key = os.environ["E2B_API_KEY"]',
        'present = str(os.environ.get("E2B_API_KEY"))',
        'token = os.environ.get("E2B_GHCR_TOKEN")',
        'token = os.getenv("E2B_GHCR_TOKEN")',
        'api_key, template = _required("E2B_API_KEY", "E2B_TEMPLATE")',
        "_, image, user, token = _required("
        '"E2B_API_KEY", "IMG", "E2B_GHCR_USERNAME", "E2B_GHCR_TOKEN")',
        'values = _required("E2B_API_KEY")',
    ],
)
def test_the_static_guard_bites_a_planted_secret(planted: str) -> None:
    assert secret_guard.violations(planted) != []


@pytest.mark.parametrize(
    "allowed",
    [
        "await build_template(spec)",
        "build_template(spec, api_key=None)",
        'available = bool(os.environ.get("E2B_API_KEY"))',
        "build_template(spec, registry_env="
        'RegistryCredentialsEnv("E2B_GHCR_USERNAME", "E2B_GHCR_TOKEN"))',
        '_, template = _required("E2B_API_KEY", "E2B_TEMPLATE")',
        'template = _required("E2B_TEMPLATE")',
        'manifest = os.environ.get("E2B_MANIFEST_PATH", "/opt/agentic/version.json")',
    ],
)
def test_the_static_guard_allows_secrets_left_in_the_environment(allowed: str) -> None:
    assert secret_guard.violations(allowed) == []


def test_pytest_config_does_not_show_locals() -> None:
    config = tomllib.loads((_PACKAGE / "pyproject.toml").read_text())
    addopts = config["tool"]["pytest"]["ini_options"].get("addopts", "").split()
    assert not {"-l", "--showlocals"} & set(addopts)


_LEAKY_TEST = """
import os

def build(spec, *, api_key):
    raise RuntimeError("build failed")

def test_leaks_the_key_through_an_argument_and_a_message():
    print("captured:", os.environ["E2B_API_KEY"])
    assert os.environ["E2B_GHCR_TOKEN"] == "x"

def test_leaks_the_key_as_a_frame_argument():
    build("spec", api_key=os.environ["E2B_API_KEY"])
"""


def _run_leaky(tmp_path: Path, *plugin: str) -> str:
    (tmp_path / "test_leaky.py").write_text(_LEAKY_TEST)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--showlocals",
            "-rA",
            *plugin,
            "-c",
            str(tmp_path / "pytest.ini"),
            str(tmp_path / "test_leaky.py"),
        ],
        cwd=_PACKAGE,
        env={
            **{k: v for k, v in os.environ.items() if k not in secret_guard.SECRET_VARIABLES},
            "E2B_API_KEY": _FAKE_KEY,
            "E2B_GHCR_TOKEN": _FAKE_TOKEN,
            "PYTHONPATH": str(_PACKAGE),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert "2 failed" in result.stdout, result.stdout + result.stderr
    return result.stdout + result.stderr


def test_a_failing_report_shows_planted_secrets_without_the_hook(tmp_path: Path) -> None:
    """The negative control: the leaky test really does put both secrets in its report."""
    output = _run_leaky(tmp_path)
    assert _FAKE_KEY in output and _FAKE_TOKEN in output


def test_the_report_hook_redacts_planted_secrets_even_with_showlocals(tmp_path: Path) -> None:
    output = _run_leaky(tmp_path, "-p", "tests.integration.secret_guard")
    assert _FAKE_KEY not in output and _FAKE_TOKEN not in output
    assert output.count(secret_guard.REDACTED) >= 3


def test_the_integration_conftest_installs_the_hook() -> None:
    from tests.integration import conftest

    assert conftest.pytest_runtest_makereport is secret_guard.pytest_runtest_makereport


def test_short_values_are_not_treated_as_secrets() -> None:
    assert secret_guard.secret_values([("E2B_API_KEY", "abc")]) == []
    assert secret_guard.secret_values([("E2B_API_KEY", _FAKE_KEY)]) == [_FAKE_KEY]
