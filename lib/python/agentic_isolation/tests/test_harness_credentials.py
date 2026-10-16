"""Presenter shapes (ADR-042). The wire round trip through the pinned CLIs is
`tests/integration/test_harness_credential_presentation.py`."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
import tomllib
from pydantic import SecretStr, ValidationError

from agentic_isolation.harnesses.claude.credentials import ClaudeApiKeyPresenter
from agentic_isolation.harnesses.codex.credentials import CodexApiKeyPresenter
from agentic_isolation.harnesses.credentials import PresentedFile

WIRE = SecretStr("cap-unit-7f3a")


def test_claude_presents_the_wire_as_its_api_key_under_the_anthropic_prefix() -> None:
    presented = ClaudeApiKeyPresenter().present(WIRE, "https://broker.test/")

    assert {k: v.get_secret_value() for k, v in presented.env.items()} == {
        "ANTHROPIC_BASE_URL": "https://broker.test/anthropic",
        "ANTHROPIC_API_KEY": "cap-unit-7f3a",
    }
    assert presented.files == ()


def test_codex_config_routes_one_https_provider_and_carries_no_secret() -> None:
    presented = CodexApiKeyPresenter().present(WIRE, 'https://broker.test/"x')

    assert {k: v.get_secret_value() for k, v in presented.env.items()} == {
        "OPENAI_API_KEY": "cap-unit-7f3a"
    }
    (config_file,) = presented.files
    assert config_file.path == PurePosixPath(".codex/config.toml")
    text = config_file.content.get_secret_value()
    assert "cap-unit-7f3a" not in text
    config = tomllib.loads(text)
    provider = config["model_providers"][config["model_provider"]]
    assert provider == {
        "name": config["model_provider"],
        "base_url": 'https://broker.test/"x/openai/v1',
        "wire_api": "responses",
        "env_key": "OPENAI_API_KEY",
        "supports_websockets": False,
    }


@pytest.mark.parametrize("presenter", [ClaudeApiKeyPresenter(), CodexApiKeyPresenter()])
def test_wire_form_is_the_capability_the_broker_hashes(
    presenter: ClaudeApiKeyPresenter | CodexApiKeyPresenter,
) -> None:
    assert presenter.wire_credential(WIRE).get_secret_value() == "cap-unit-7f3a"


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", ".codex/../../x", ""])
def test_presented_files_stay_under_home(path: str) -> None:
    with pytest.raises(ValidationError):
        PresentedFile(path=PurePosixPath(path), content=SecretStr("x"))


def test_presented_secrets_do_not_render() -> None:
    presented = CodexApiKeyPresenter().present(WIRE, "https://broker.test")
    assert "cap-unit-7f3a" not in repr(presented)
