"""Codex API-key presentation. See ADR-042
(`docs/adrs/042-harness-credential-presentation.md`).

Measured against Codex 0.160.1:

- `OPENAI_BASE_URL` is ignored; the built-in provider still dials
  api.openai.com. A base URL has to come from config.
- `openai_base_url` in config reroutes the built-in provider, but it opens a
  WebSocket to `/responses` before falling back to HTTPS, so one turn is
  several requests.
- A config-defined provider with `env_key` and `supports_websockets = false`
  sends exactly one `POST <base_url>/openai/v1/responses` with
  `authorization: Bearer <wire>`. `env_key` reads only that variable, so a
  ChatGPT `auth.json` in `$CODEX_HOME` can never be sent instead.

The wire form is the capability itself. The config file carries no secret.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from pydantic import SecretStr

from agentic_isolation.harnesses.credentials import PresentedCredentials, PresentedFile

PROVIDER_ID = "syntropic_broker"
API_KEY_ENV = "OPENAI_API_KEY"
# `$CODEX_HOME` defaults to `~/.codex`; a workspace that moves it must move
# this file with it.
CONFIG_PATH = PurePosixPath(".codex/config.toml")


class CodexApiKeyPresenter:
    """Presents a broker capability to Codex as a provider API key."""

    token_type = "openai"

    def wire_credential(self, entropy: SecretStr) -> SecretStr:
        return entropy

    def present(self, wire: SecretStr, base_url: str) -> PresentedCredentials:
        # json.dumps yields a valid TOML basic string for any URL.
        provider_url = json.dumps(f"{base_url.rstrip('/')}/openai/v1")
        config = "\n".join(
            [
                f'model_provider = "{PROVIDER_ID}"',
                "",
                f"[model_providers.{PROVIDER_ID}]",
                f'name = "{PROVIDER_ID}"',
                f"base_url = {provider_url}",
                'wire_api = "responses"',
                f'env_key = "{API_KEY_ENV}"',
                "supports_websockets = false",
                "",
            ]
        )
        return PresentedCredentials(
            env={API_KEY_ENV: wire},
            files=(PresentedFile(path=CONFIG_PATH, content=SecretStr(config)),),
        )


__all__ = ["CodexApiKeyPresenter"]
