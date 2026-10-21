"""`ClaudeHeadless`: one `claude -p` answer on the CLI's OAuth login.

Ported from syntropic137 `scripts/tests/test_eval_suite.py` (syntropic137#2036). The
fake runner never starts `claude`; the real-CLI tests skip without it and talk only to
a local endpoint with synthetic credentials, so no test spends model quota.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentic_isolation.harnesses.claude import headless
from agentic_isolation.harnesses.claude.headless import (
    CLAUDE_CREDENTIAL_ENV,
    ClaudeHeadless,
    ClaudeHeadlessError,
    ClaudeHeadlessModelError,
    ClaudeHeadlessNotOAuthError,
)

MODEL = "claude-sonnet-5-5"


def _auth_status(
    method: str = "claude.ai",
    *,
    logged_in: bool = True,
    provider: str = "firstParty",
    key_source: str | None = None,
) -> str:
    """What `claude auth status --json` prints (shape read from CLI 2.1.293)."""
    status: dict[str, object] = {
        "loggedIn": logged_in,
        "authMethod": method,
        "apiProvider": provider,
        "analyticsDisabled": False,
        "configDirectory": "/home/u/.claude",
    }
    if key_source is not None:
        status["apiKeySource"] = key_source
    return json.dumps(status)


@dataclass
class _FakeClaude:
    """Stands in for `subprocess.run` under `ClaudeHeadless`: records calls, never runs `claude`.

    `claude auth status` is answered from `auth_stdout` and recorded in `auth_calls`;
    every other call is an answer, recorded in `calls`. `envs` holds each call's `env`.
    A status asked without CLAUDE_CODE_OAUTH_TOKEN is `auth_stdout_without_token` when set.
    """

    stdout: str = ""
    returncode: int = 0
    stderr: str = ""
    timeout: bool = False
    auth_stdout: str = field(default_factory=_auth_status)
    auth_returncode: int = 0
    auth_timeout: bool = False
    auth_stdout_without_token: str | None = None
    calls: list[tuple[list[str], int, float]] = field(default_factory=list)
    auth_calls: list[tuple[list[str], int, float]] = field(default_factory=list)
    envs: list[dict[str, str]] = field(default_factory=list)

    def __call__(
        self,
        args: list[str],
        *,
        stdin: int,
        capture_output: bool,
        text: bool,
        timeout: float,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        self.envs.append(dict(env))
        if args[1:3] == ["auth", "status"]:
            self.auth_calls.append((list(args), stdin, timeout))
            if self.auth_timeout:
                raise subprocess.TimeoutExpired(args, timeout)
            if self.auth_stdout_without_token is not None and "CLAUDE_CODE_OAUTH_TOKEN" not in env:
                return subprocess.CompletedProcess(args, 1, self.auth_stdout_without_token, "")
            return subprocess.CompletedProcess(args, self.auth_returncode, self.auth_stdout, "")
        self.calls.append((list(args), stdin, timeout))
        if self.timeout:
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def _envelope(result: str, *, is_error: bool = False) -> str:
    """What `claude -p --output-format json` prints, with fields the transport ignores."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "result": result,
            "session_id": "s",
            "total_cost_usd": 0.01,
        }
    )


@pytest.fixture(autouse=True)
def _oauth_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fake test starts on a synthetic OAuth token and no other credential."""
    for name in CLAUDE_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-oauth")


def _client(fake: _FakeClaude, timeout_s: float = 42.0) -> ClaudeHeadless:
    return ClaudeHeadless(effort="high", timeout_s=timeout_s, run=fake)


def test_runs_the_cli_headless_with_the_exact_model() -> None:
    fake = _FakeClaude(stdout=_envelope("ok"))
    assert _client(fake).complete(model=MODEL, system="the system", prompt="the prompt") == "ok"
    [(argv, _, _)] = fake.calls
    assert argv == [
        "claude",
        "-p",
        "--model",
        MODEL,
        "--system-prompt",
        "the system",
        "--effort",
        "high",
        "--output-format",
        "json",
        "--tools",
        "",
        "--no-session-persistence",
        "--setting-sources",
        "user",
        "--",
        "the prompt",
    ]
    assert "--bare" not in argv


@pytest.mark.parametrize("prompt", ["--help", "-h", "--version", "--model", "-", "--"])
def test_a_flag_shaped_prompt_is_passed_after_the_end_of_options(prompt: str) -> None:
    """Without the ``--`` the CLI parses a prompt like ``--help`` as its own option."""
    fake = _FakeClaude(stdout=_envelope("ok"))
    _client(fake).complete(model=MODEL, system="s", prompt=prompt)
    [(argv, _, _)] = fake.calls
    assert argv[-2:] == ["--", prompt]
    assert argv.index("--") == len(argv) - 2


@pytest.mark.parametrize("model", ["sonnet", "opus", "fable", "default", "sonnet[1m]"])
def test_a_model_alias_is_refused_before_any_call(model: str) -> None:
    """`--model` takes an alias for the latest model; the caller's exact id is the identity."""
    fake = _FakeClaude(stdout=_envelope("ok"))
    with pytest.raises(ClaudeHeadlessModelError, match="exact model id"):
        _client(fake).complete(model=model, system="s", prompt="p")
    assert fake.calls == [] and fake.auth_calls == []


def test_closes_stdin_and_passes_its_timeout() -> None:
    fake = _FakeClaude(stdout=_envelope("ok"))
    _client(fake).complete(model=MODEL, system="s", prompt="p")
    [(_, stdin, timeout)] = fake.calls
    assert stdin == subprocess.DEVNULL
    assert timeout == 42.0


def test_no_credential_in_the_parent_environment_reaches_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claude -p` prefers an exported key to the OAuth login, so the child never sees one."""
    for name in CLAUDE_CREDENTIAL_ENV:
        monkeypatch.setenv(name, "synthetic-not-a-credential")
    monkeypatch.setenv("HOME", "/home/u")
    fake = _FakeClaude(stdout=_envelope("ok"))
    _client(fake).complete(model=MODEL, system="s", prompt="p")
    assert len(fake.envs) == 2 and len(fake.auth_calls) == 1 and len(fake.calls) == 1
    for env in fake.envs:
        assert not set(env) & {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        }
        # The rest of the environment, the OAuth login's included, is kept.
        assert env["HOME"] == "/home/u" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-oauth"


def test_check_auth_asks_the_cli_with_the_answers_environment_and_answers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-not-a-credential")
    fake = _FakeClaude()
    _client(fake).check_auth()
    assert fake.calls == []
    assert fake.auth_calls[0] == (["claude", "auth", "status", "--json"], subprocess.DEVNULL, 42.0)
    assert "ANTHROPIC_API_KEY" not in fake.envs[0]


def test_check_auth_refuses_api_key_auth() -> None:
    fake = _FakeClaude(auth_stdout=_auth_status("api_key", key_source="ANTHROPIC_API_KEY"))
    with pytest.raises(ClaudeHeadlessNotOAuthError, match="authMethod=api_key"):
        _client(fake).check_auth()


def test_the_login_is_checked_with_the_answers_own_environment_before_it() -> None:
    fake = _FakeClaude(stdout=_envelope("ok"), auth_stdout=_auth_status("claude.ai"))
    _client(fake).complete(model=MODEL, system="s", prompt="p")
    assert fake.auth_calls == [(["claude", "auth", "status", "--json"], subprocess.DEVNULL, 42.0)]
    assert fake.envs[0] == fake.envs[1]


def test_the_stored_oauth_login_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude.ai` is the stored login, which every other credential outranks."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    fake = _FakeClaude(stdout=_envelope("ok"), auth_stdout=_auth_status("claude.ai"))
    assert _client(fake).complete(model=MODEL, system="s", prompt="p") == "ok"
    assert len(fake.auth_calls) == 1


@pytest.mark.parametrize(
    "without_token",
    [_auth_status("none", logged_in=False), _auth_status("claude.ai")],
    ids=["token-only", "token-over-stored-login"],
)
def test_an_oauth_token_shown_to_come_from_its_variable_is_accepted(without_token: str) -> None:
    """Asked again without CLAUDE_CODE_OAUTH_TOKEN, the status stops saying `oauth_token`."""
    fake = _FakeClaude(
        stdout=_envelope("ok"),
        auth_stdout=_auth_status("oauth_token"),
        auth_stdout_without_token=without_token,
    )
    assert _client(fake).complete(model=MODEL, system="s", prompt="p") == "ok"
    status = (["claude", "auth", "status", "--json"], subprocess.DEVNULL, 42.0)
    assert fake.auth_calls == [status, status]
    with_token, without, answer = fake.envs
    assert with_token == answer and with_token["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-oauth"
    assert without == {k: v for k, v in with_token.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}


@pytest.mark.parametrize(
    ("oauth_token_env", "auth_calls", "match"),
    [(None, 1, "with no CLAUDE_CODE_OAUTH_TOKEN"), ("synthetic-oauth", 2, "still says")],
    ids=["settings-bearer-alone", "settings-bearer-over-oauth-token"],
)
def test_a_settings_bearer_token_cannot_pass_for_oauth(
    monkeypatch: pytest.MonkeyPatch, oauth_token_env: str | None, auth_calls: int, match: str
) -> None:
    """A settings file's `env` ANTHROPIC_AUTH_TOKEN outranks every OAuth credential, and
    CLI 2.1.293 reports it exactly as it reports an OAuth token (status shape below)."""
    if oauth_token_env is None:
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    bearer = json.dumps(
        {
            "loggedIn": True,
            "authMethod": "oauth_token",
            "apiProvider": "firstParty",
            "analyticsDisabled": False,
        }
    )
    # With the variable set, taking it away leaves the settings token in place.
    fake = _FakeClaude(
        stdout=_envelope("ok"),
        auth_stdout=bearer,
        auth_stdout_without_token=None if oauth_token_env is None else bearer,
    )
    with pytest.raises(ClaudeHeadlessNotOAuthError, match=match):
        _client(fake).complete(model=MODEL, system="s", prompt="p")
    assert len(fake.auth_calls) == auth_calls
    assert fake.calls == []


@pytest.mark.parametrize(
    ("fake", "error", "match"),
    [
        (
            _FakeClaude(auth_stdout=_auth_status("api_key", key_source="ANTHROPIC_API_KEY")),
            "ClaudeHeadlessNotOAuthError",
            "authMethod=api_key",
        ),
        (
            _FakeClaude(auth_stdout=_auth_status("api_key")),
            "ClaudeHeadlessNotOAuthError",
            "authMethod=api_key apiProvider",
        ),
        (
            _FakeClaude(auth_stdout=_auth_status("api_key_helper", key_source="apiKeyHelper")),
            "ClaudeHeadlessNotOAuthError",
            "apiKeySource=apiKeyHelper",
        ),
        (
            _FakeClaude(auth_stdout=_auth_status("claude.ai", key_source="apiKeyHelper")),
            "ClaudeHeadlessNotOAuthError",
            "apiKeySource=apiKeyHelper",
        ),
        (
            _FakeClaude(auth_stdout=_auth_status("third_party", provider="bedrock")),
            "ClaudeHeadlessNotOAuthError",
            "apiProvider=bedrock",
        ),
        (
            _FakeClaude(auth_stdout=_auth_status("none", logged_in=False), auth_returncode=1),
            "ClaudeHeadlessNotOAuthError",
            "loggedIn=False",
        ),
        (
            _FakeClaude(auth_stdout=_auth_status(), auth_returncode=1),
            "ClaudeHeadlessNotOAuthError",
            "claude auth login",
        ),
        (
            _FakeClaude(auth_stdout="not json"),
            "ClaudeHeadlessUnreadableOutputError",
            "auth status",
        ),
        (_FakeClaude(auth_timeout=True), "ClaudeHeadlessTimeoutError", "auth status"),
    ],
    ids=[
        "api-key",
        "non-oauth-method-without-key-source",
        "api-key-helper",
        "oauth-but-key-source",
        "bedrock",
        "logged-out",
        "status-exit",
        "status-unreadable",
        "status-timeout",
    ],
)
def test_a_call_that_would_not_run_on_oauth_is_refused_before_any_answer(
    fake: _FakeClaude, error: str, match: str
) -> None:
    """Settings can bring a key back (apiKeyHelper, `env`), so the CLI is asked, not assumed."""
    with pytest.raises(getattr(headless, error), match=match) as raised:
        _client(fake).complete(model=MODEL, system="s", prompt="p")
    assert isinstance(raised.value, ClaudeHeadlessError)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("fake", "error", "match"),
    [
        (
            _FakeClaude(returncode=1, stderr="not logged in"),
            "ClaudeHeadlessExitError",
            "exited 1: not logged in",
        ),
        (_FakeClaude(timeout=True), "ClaudeHeadlessTimeoutError", "within 42s"),
        (_FakeClaude(stdout=""), "ClaudeHeadlessEmptyOutputError", "printed nothing"),
        (_FakeClaude(stdout=" \n"), "ClaudeHeadlessEmptyOutputError", "printed nothing"),
        (_FakeClaude(stdout=_envelope("")), "ClaudeHeadlessEmptyOutputError", "empty result"),
        (
            _FakeClaude(stdout="not json"),
            "ClaudeHeadlessUnreadableOutputError",
            "no JSON result envelope",
        ),
        (
            _FakeClaude(stdout='{"is_error": false}'),
            "ClaudeHeadlessUnreadableOutputError",
            "no JSON",
        ),
        (
            _FakeClaude(stdout=_envelope("usage limit reached", is_error=True)),
            "ClaudeHeadlessReportedError",
            "usage limit reached",
        ),
    ],
    ids=["exit", "timeout", "empty", "blank", "empty-result", "not-json", "no-result", "is-error"],
)
def test_every_failure_raises_its_own_error(fake: _FakeClaude, error: str, match: str) -> None:
    with pytest.raises(getattr(headless, error), match=match) as raised:
        _client(fake).complete(model=MODEL, system="s", prompt="p")
    assert isinstance(raised.value, ClaudeHeadlessError)


_needs_claude = pytest.mark.skipif(
    shutil.which("claude") is None, reason="needs the real claude CLI"
)


@_needs_claude
def test_the_real_cli_still_takes_every_flag_the_argv_uses() -> None:
    """`claude --help` only: no login, no model call. Flags a new CLI drops fail here."""
    done = subprocess.run(
        ["claude", "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    flags = ClaudeHeadless(effort="medium").argv(model=MODEL, system="s", prompt="p")
    for flag in (a for a in flags if a.startswith("-") and a != "--"):
        assert re.search(rf"(^|[\s,]){re.escape(flag)}\b", done.stdout), flag
    assert "low, medium, high, xhigh, max" in done.stdout


class _MessagesEndpoint(http.server.ThreadingHTTPServer):
    """A local Messages API that answers every request with one streamed "ok" and records
    the credential each request carried, so the real CLI can be run with no real login."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _MessagesHandler)
        self.authorizations: list[str | None] = []
        self.requests: list[dict[str, Any]] = []


class _MessagesHandler(http.server.BaseHTTPRequestHandler):
    server: _MessagesEndpoint

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.authorizations.append(self.headers.get("authorization"))
        self.server.requests.append(body)
        events: list[dict[str, Any]] = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(
            "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        )

    def log_message(self, format: str, *args: object) -> None:
        pass


@_needs_claude
@pytest.mark.parametrize(
    ("scope", "settings_file"),
    [
        ("none", None),
        ("project", "project/.claude/settings.json"),
        ("local", "project/.claude/settings.local.json"),
        ("user", "home/.claude/settings.json"),
    ],
    ids=["none", "project", "local", "user"],
)
def test_no_settings_file_routes_the_real_cli_off_the_oauth_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: str, settings_file: str | None
) -> None:
    """The real CLI, against a local endpoint with synthetic credentials: a settings
    ``env`` bearer token either never reaches the answer or stops the call before it.

    `claude auth status` ignores project and local settings while `claude -p` loads
    them by default, so only what the answer is run with closes those two; user
    settings are seen by the status and refused there.
    """
    for name in list(os.environ):
        if name.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_DIR")):
            monkeypatch.delenv(name)
    home, project = tmp_path / "home", tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    if settings_file is not None:
        (tmp_path / settings_file).write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "synthetic-settings-bearer"}})
        )
    endpoint = _MessagesEndpoint()
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-oauth")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{endpoint.server_port}")
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        monkeypatch.chdir(project)
        client = ClaudeHeadless(effort="medium", timeout_s=60.0)
        if scope == "user":
            with pytest.raises(ClaudeHeadlessNotOAuthError, match="still says"):
                client.complete(model=MODEL, system="s", prompt="p")
            assert endpoint.authorizations == []
        else:
            assert client.complete(model=MODEL, system="s", prompt="p") == "ok"
            assert endpoint.authorizations
            assert set(endpoint.authorizations) == {"Bearer synthetic-oauth"}
    finally:
        endpoint.shutdown()
        endpoint.server_close()


def _texts(content: str | list[dict[str, Any]]) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [block["text"] for block in content if block["type"] == "text"]


@_needs_claude
@pytest.mark.parametrize(
    "prompt", ["an ordinary prompt", "--help", "-h", "--version", "--unknown-option"]
)
def test_the_real_cli_sends_a_flag_shaped_prompt_as_the_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prompt: str
) -> None:
    """The real CLI, against a local endpoint with synthetic credentials: every request
    carries the caller's exact model and prompt, which is never read as a CLI option."""
    for name in list(os.environ):
        if name.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_DIR")):
            monkeypatch.delenv(name)
    home, project = tmp_path / "home", tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    project.mkdir()
    endpoint = _MessagesEndpoint()
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-oauth")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{endpoint.server_port}")
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        monkeypatch.chdir(project)
        client = ClaudeHeadless(effort="medium", timeout_s=60.0)
        assert client.complete(model=MODEL, system="--help", prompt=prompt) == "ok"
        assert endpoint.requests
        for request in endpoint.requests:
            assert request["model"] == MODEL
            assert "--help" in "".join(_texts(request.get("system", [])))
            user = [m["content"] for m in request["messages"] if m["role"] == "user"]
            assert prompt in [text for content in user for text in _texts(content)]
    finally:
        endpoint.shutdown()
        endpoint.server_close()
