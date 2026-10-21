"""Claude Code run headless for one answer (`claude -p`), on the CLI's own OAuth login.

Everything here changes when Anthropic ships a new Claude Code CLI: the argv, the
`--output-format json` envelope, the `claude auth status --json` fields and which
environment variables outrank the login. Callers see `ClaudeHeadless.complete` and
`ClaudeHeadless.check_auth` and the `ClaudeHeadlessError` family, nothing else.
Moved from syntropic137 `scripts/eval_suite.py` (`ClaudeCode`, syntropic137#2036).

Measured against `CLAUDE_CLI_VERSION`. Re-check the flags in `claude --help` and the
fields of `claude auth status --json` before raising it.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CLAUDE_CLI_VERSION = "2.1.293"
"""The `claude --version` every flag, field and precedence rule here was checked against."""

ClaudeEffort = Literal["low", "medium", "high", "xhigh", "max"]
"""`claude --effort <level>` values (`claude --help`, CLI 2.1.293)."""

CLAUDE_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_CUSTOM_HEADERS",
)
"""Variables that would put a call on a key, a bearer token or a cloud provider, ahead
of the OAuth login (`claude -p` prefers an API key whenever one is set), or add an
``Authorization`` or ``x-api-key`` header beside it. Never passed to the CLI."""

CLAUDE_OAUTH_METHODS = frozenset({"claude.ai", "oauth_token"})
"""`authMethod` values of `claude auth status` that can be the OAuth login. `claude.ai`
is the stored login; `oauth_token` is also what any bearer token reports, so it is
accepted only once `CLAUDE_OAUTH_TOKEN_ENV` is shown to be its source."""

CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
"""The variable that carries an OAuth token from `claude setup-token`."""

CLAUDE_SETTING_SOURCES = "user"
"""The only settings files `claude -p` loads for an answer. `claude auth status` reads
user settings but not project or local ones, which `claude -p` loads by default from
its working directory: a project's ``env`` ANTHROPIC_AUTH_TOKEN would reach the answer
unseen by the OAuth check. Managed settings apply to both regardless."""

CLAUDE_FLAG_SETTINGS = json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": ""}})
"""`claude -p --settings` for an answer, which outrank user settings. `claude auth status`
does not report custom headers, so a user settings ``env`` ANTHROPIC_CUSTOM_HEADERS could
replace the OAuth bearer or add an API key unseen by the check: the answer sends none."""

CLAUDE_HEADLESS_TIMEOUT_S = 300.0
"""Default per-call ceiling on one CLI run, after which the call is an error."""


class ProcessRunner(Protocol):
    """How `ClaudeHeadless` starts the CLI: `subprocess.run`'s shape, so a fake stands in."""

    def __call__(
        self,
        args: list[str],
        *,
        stdin: int,
        capture_output: Literal[True],
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]: ...


class ClaudeHeadlessError(Exception):
    """A headless Claude Code call gave no answer. The subclass names why."""


class ClaudeHeadlessModelError(ClaudeHeadlessError):
    """The model is an alias, not an exact id, so the CLI would pick the model. Nothing ran."""


class ClaudeHeadlessExitError(ClaudeHeadlessError):
    """The CLI exited non-zero."""


class ClaudeHeadlessTimeoutError(ClaudeHeadlessError):
    """The CLI did not finish within `ClaudeHeadless.timeout_s`."""


class ClaudeHeadlessEmptyOutputError(ClaudeHeadlessError):
    """The CLI printed nothing, or a result with no text."""


class ClaudeHeadlessUnreadableOutputError(ClaudeHeadlessError):
    """The CLI printed something that is not its JSON result envelope or auth status."""


class ClaudeHeadlessReportedError(ClaudeHeadlessError):
    """The envelope parsed and says `is_error`: the session failed (auth, a refusal, a limit)."""


class ClaudeHeadlessNotOAuthError(ClaudeHeadlessError):
    """`claude auth status` says the CLI would not run on its OAuth login, so nothing is asked."""


class _ClaudeResult(BaseModel):
    """The fields of `claude -p --output-format json` that carry the answer."""

    model_config = ConfigDict(extra="ignore")
    is_error: bool
    result: str


class _ClaudeAuthStatus(BaseModel):
    """The fields of `claude auth status --json` that say which credential a call would use."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    logged_in: bool = Field(alias="loggedIn")
    auth_method: str = Field(alias="authMethod")
    api_provider: str = Field(alias="apiProvider")
    api_key_source: str | None = Field(default=None, alias="apiKeySource")


@dataclass(frozen=True)
class ClaudeHeadless:
    """One answer from Claude Code run headless (`claude -p`), on the CLI's own login.

    No API key is read or passed: the call runs on the CLI's OAuth login, and only on
    it. The child gets the parent environment minus `CLAUDE_CREDENTIAL_ENV`, and before
    each answer `check_auth`, run with that same environment, must see a first-party
    OAuth method (`CLAUDE_OAUTH_METHODS`) and no API key source. That catches what the
    environment alone cannot, such as an ``apiKeyHelper`` or ``env`` key in a settings
    file. The answer loads only the settings that status reads (`CLAUDE_SETTING_SOURCES`),
    and sends no custom headers (`CLAUDE_FLAG_SETTINGS`), so no settings file can change
    the credential after the check. Never add ``--bare``,
    which skips that login for an API key. Each call is one non-interactive session with
    stdin closed, tools disabled, nothing persisted, the exact model id given and
    `effort`. The reply is the ``result`` of the JSON envelope. Every way that yields no
    answer raises a `ClaudeHeadlessError` subclass naming it.
    """

    effort: ClaudeEffort
    timeout_s: float = CLAUDE_HEADLESS_TIMEOUT_S
    run: ProcessRunner = subprocess.run
    executable: str = "claude"

    def argv(self, *, model: str, system: str, prompt: str) -> list[str]:
        if not model.startswith("claude-"):
            raise ClaudeHeadlessModelError(
                f"claude -p needs an exact model id such as 'claude-sonnet-5-5', not "
                f"{model!r}: the CLI resolves an alias to whatever model is latest"
            )
        return [
            self.executable,
            "-p",
            "--model",
            model,
            "--system-prompt",
            system,
            "--effort",
            self.effort,
            "--output-format",
            "json",
            "--settings",
            CLAUDE_FLAG_SETTINGS,
            "--tools",
            "",
            "--no-session-persistence",
            "--setting-sources",
            CLAUDE_SETTING_SOURCES,
            "--",
            prompt,
        ]

    def check_auth(self) -> None:
        """Raise `ClaudeHeadlessNotOAuthError` unless a call now would run on the OAuth login."""
        self._require_oauth(_child_env())

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        """The model's answer to `prompt` under `system`, or a `ClaudeHeadlessError`."""
        argv = self.argv(model=model, system=system, prompt=prompt)
        env = _child_env()
        self._require_oauth(env)
        try:
            done = self.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeHeadlessTimeoutError(
                f"claude -p --model {model} gave no answer within {self.timeout_s:g}s"
            ) from exc
        if done.returncode != 0:
            raise ClaudeHeadlessExitError(
                f"claude -p --model {model} exited {done.returncode}: {done.stderr.strip()[-500:]}"
            )
        if not done.stdout.strip():
            raise ClaudeHeadlessEmptyOutputError(f"claude -p --model {model} printed nothing")
        try:
            reply = _ClaudeResult.model_validate_json(done.stdout)
        except ValidationError as exc:
            raise ClaudeHeadlessUnreadableOutputError(
                f"claude -p --model {model} printed no JSON result envelope: {done.stdout[:500]!r}"
            ) from exc
        if reply.is_error:
            raise ClaudeHeadlessReportedError(
                f"claude -p --model {model} reported an error: {reply.result[:500]!r}"
            )
        if not reply.result.strip():
            raise ClaudeHeadlessEmptyOutputError(
                f"claude -p --model {model} returned an empty result"
            )
        return reply.result

    def _auth_status(self, env: Mapping[str, str]) -> tuple[int, _ClaudeAuthStatus]:
        """`claude auth status` run with `env`: its exit code and the status it printed."""
        try:
            done = self.run(
                [self.executable, "auth", "status", "--json"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeHeadlessTimeoutError(
                f"claude auth status gave no answer within {self.timeout_s:g}s"
            ) from exc
        try:
            return done.returncode, _ClaudeAuthStatus.model_validate_json(done.stdout)
        except ValidationError as exc:
            raise ClaudeHeadlessUnreadableOutputError(
                f"claude auth status exited {done.returncode} with no readable status: "
                f"{done.stdout[:500]!r} {done.stderr.strip()[-500:]}"
            ) from exc

    def _require_oauth(self, env: Mapping[str, str]) -> None:
        """Raise unless a call with `env` would run on the OAuth login."""
        returncode, status = self._auth_status(env)
        if (
            returncode != 0
            or not status.logged_in
            or status.api_provider != "firstParty"
            or status.auth_method not in CLAUDE_OAUTH_METHODS
            or status.api_key_source is not None
        ):
            raise ClaudeHeadlessNotOAuthError(
                f"headless Claude Code runs only on its OAuth login, but claude auth status "
                f"says loggedIn={status.logged_in} authMethod={status.auth_method} "
                f"apiProvider={status.api_provider} apiKeySource={status.api_key_source}: "
                "run `claude auth login`, and remove any apiKeyHelper or key from its settings"
            )
        if status.auth_method == "oauth_token":
            self._require_oauth_token_source(env)

    def _require_oauth_token_source(self, env: Mapping[str, str]) -> None:
        """Raise unless an `oauth_token` status comes from `CLAUDE_OAUTH_TOKEN_ENV`.

        The CLI reports every bearer token as `oauth_token`, including an
        ``ANTHROPIC_AUTH_TOKEN`` from a settings file's ``env``, which outranks both the
        OAuth token and the stored login. Asked again without the variable, a status
        that still says `oauth_token` has its token from somewhere else.
        """
        fix = "remove ANTHROPIC_AUTH_TOKEN from the env of every Claude Code settings file"
        if CLAUDE_OAUTH_TOKEN_ENV not in env:
            raise ClaudeHeadlessNotOAuthError(
                f"claude auth status says authMethod=oauth_token with no "
                f"{CLAUDE_OAUTH_TOKEN_ENV} set, so its token is a bearer token from a "
                f"settings file: {fix}"
            )
        _, without = self._auth_status(
            {k: v for k, v in env.items() if k != CLAUDE_OAUTH_TOKEN_ENV}
        )
        if without.auth_method == "oauth_token":
            raise ClaudeHeadlessNotOAuthError(
                f"claude auth status still says authMethod=oauth_token without "
                f"{CLAUDE_OAUTH_TOKEN_ENV}, so a bearer token from a settings file "
                f"outranks the OAuth token: {fix}"
            )


def _child_env() -> dict[str, str]:
    """The parent environment minus every variable that would outrank the OAuth login."""
    return {k: v for k, v in os.environ.items() if k not in CLAUDE_CREDENTIAL_ENV}


__all__ = [
    "CLAUDE_CLI_VERSION",
    "CLAUDE_CREDENTIAL_ENV",
    "CLAUDE_FLAG_SETTINGS",
    "CLAUDE_HEADLESS_TIMEOUT_S",
    "CLAUDE_OAUTH_METHODS",
    "CLAUDE_OAUTH_TOKEN_ENV",
    "CLAUDE_SETTING_SOURCES",
    "ClaudeEffort",
    "ClaudeHeadless",
    "ClaudeHeadlessEmptyOutputError",
    "ClaudeHeadlessError",
    "ClaudeHeadlessExitError",
    "ClaudeHeadlessModelError",
    "ClaudeHeadlessNotOAuthError",
    "ClaudeHeadlessReportedError",
    "ClaudeHeadlessTimeoutError",
    "ClaudeHeadlessUnreadableOutputError",
    "ProcessRunner",
]
