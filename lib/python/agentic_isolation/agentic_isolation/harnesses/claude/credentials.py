"""Claude Code API-key presentation. See ADR-042
(`docs/adrs/042-harness-credential-presentation.md`).

Measured against Claude Code 2.1.293: `ANTHROPIC_API_KEY` is not
format-checked in `-p` mode, so the wire form is the capability itself and
it reaches `<base_url>/v1/messages` as `x-api-key`. No file is needed.
"""

from __future__ import annotations

from pydantic import SecretStr

from agentic_isolation.harnesses.credentials import PresentedCredentials


class ClaudeApiKeyPresenter:
    """Presents a broker capability to Claude Code as its API key."""

    token_type = "anthropic"

    def wire_credential(self, entropy: SecretStr) -> SecretStr:
        return entropy

    def present(self, wire: SecretStr, base_url: str) -> PresentedCredentials:
        return PresentedCredentials(
            env={
                "ANTHROPIC_BASE_URL": SecretStr(f"{base_url.rstrip('/')}/anthropic"),
                "ANTHROPIC_API_KEY": wire,
            }
        )


__all__ = ["ClaudeApiKeyPresenter"]
