"""Harness credential presentation, round-tripped through the pinned CLIs.

S1 of the syntropic137 #1735 credential-broker plan; the result is ADR-042
(`docs/adrs/042-harness-credential-presentation.md`).

For each class the host does what production does: generate entropy, call
the presenter's `wire_credential`, take `sha256(wire)` (what the broker
stores), then `present(wire, base_url)`. Only the presentation crosses into
a `--network none` container running the pinned image. There
`harness_credential_probe.py` serves a fake upstream on loopback, runs the
real CLI, and reports every request and the child env. The host side
(`harness_credential_scan.py`) samples every process command line while it
runs and, once it exits, reads the container's whole filesystem and every
writable mount through the daemon, as root, so files the agent user cannot
read are scanned too. This side then asserts:

- exactly one request reached the upstream, at the class's model path,
- it carries exactly one credential header, the class's standard one, once,
  and that value hashes to `sha256(wire_credential(entropy))`,
- the CLI's command line was sampled and the scan left nothing unread,
- neither the wire nor a host canary is in any file of the sandbox
  filesystem, image files included,
- no other credential header, env var or credential-shaped string is in the
  requests, the sandbox env, the command lines or the files the run wrote,
  and the host's synthetic upstream-key canaries appear in none of them.

Requirements:
    - Docker
    - the claude-cli image; the release pin is
      ghcr.io/agentparadise/agentic-workspace-claude@sha256:b3f39a3577a46dee75347ce0db7803014ab4569083c7aa353be63643df810231
      (set AGENTIC_CLAUDE_CLI_IMAGE), or one built from this tree with
      `uv run scripts/build-provider.py claude-cli`

Run with:
    uv run pytest -m integration tests/integration/test_harness_credential_presentation.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import SecretStr

from agentic_isolation.harnesses.claude.credentials import ClaudeApiKeyPresenter
from agentic_isolation.harnesses.codex.credentials import CodexApiKeyPresenter
from agentic_isolation.harnesses.credentials import PresentedCredentials
from tests.integration.harness_credential_scan import CommandLines, scan_container

IMAGE = os.environ.get("AGENTIC_CLAUDE_CLI_IMAGE", "agentic-workspace-claude-cli:latest")
PROBE = Path(__file__).with_name("harness_credential_probe.py")
PORT = 18080
# The broker authority as the sandbox sees it; loopback because the
# container has no network and the fake upstream runs beside the CLI.
BASE_URL = f"http://127.0.0.1:{PORT}"
CREDENTIAL_HEADERS = {
    "authorization",
    "x-api-key",
    "api-key",
    "proxy-authorization",
    "cookie",
    "chatgpt-account-id",
}
CREDENTIAL_ENV_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|AUTH|CREDENTIAL", re.I)
CREDENTIAL_SHAPED = re.compile(
    r"sk-ant-[\w-]{8,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_\w{20,}|github_pat_\w+"
)
# Synthetic stand-ins for the upstream keys the host holds. They are set in
# this process's env for the run; finding one in the sandbox means something
# carried host env across.
HOST_CANARIES = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-S1CANARY" + "a" * 40,
    "OPENAI_API_KEY": "sk-proj-S1CANARY" + "b" * 40,
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-S1CANARY" + "c" * 40,
    "GH_TOKEN": "ghs_S1CANARY" + "d" * 36,
}


def _image_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, check=False
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _image_available(),
        reason=f"Docker or {IMAGE} not available; "
        "run `uv run scripts/build-provider.py claude-cli`",
    ),
]


@dataclass(frozen=True)
class HarnessClass:
    presenter: ClaudeApiKeyPresenter | CodexApiKeyPresenter
    version_argv: tuple[str, ...]
    version: str
    argv: tuple[str, ...]
    # Non-credential env the workspace sets anyway; {HOME} is the probe's home.
    workspace_env: dict[str, str]
    model_path: str
    # The standard auth header and scheme prefix the broker hashes behind.
    auth_header: str
    auth_prefix: str


CLASSES = {
    "claude-api-key": HarnessClass(
        presenter=ClaudeApiKeyPresenter(),
        version_argv=("claude", "--version"),
        version="2.1.293 (Claude Code)",
        argv=(
            "claude",
            "-p",
            "PROBE",
            "--model",
            "claude-sonnet-4-5",
            "--output-format",
            "json",
            "--max-turns",
            "1",
        ),
        workspace_env={
            "CLAUDE_CONFIG_DIR": "{HOME}/.claude",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        model_path="/anthropic/v1/messages",
        auth_header="x-api-key",
        auth_prefix="",
    ),
    "codex-api-key": HarnessClass(
        presenter=CodexApiKeyPresenter(),
        version_argv=("codex", "--version"),
        version="codex-cli 0.160.1",
        argv=("codex", "exec", "--skip-git-repo-check", "PROBE"),
        workspace_env={"CODEX_HOME": "{HOME}/.codex"},
        model_path="/openai/v1/responses",
        auth_header="authorization",
        auth_prefix="Bearer ",
    ),
}


def _docker(mount: Path, *argv: str, name: str | None = None) -> subprocess.CompletedProcess[str]:
    # Named runs are kept after exit for the host scan; the caller removes them.
    keep = ["--name", name] if name else ["--rm"]
    return subprocess.run(
        [
            "docker",
            "run",
            *keep,
            "--network",
            "none",
            "--entrypoint",
            argv[0],
            "-v",
            f"{mount}:/probe:ro",
            IMAGE,
            *argv[1:],
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _spec(harness: HarnessClass, presented: PresentedCredentials) -> dict[str, object]:
    return {
        "argv": list(harness.argv),
        "env": {k: v.get_secret_value() for k, v in presented.env.items()},
        "files": [
            {
                "path": str(f.path),
                "content": f.content.get_secret_value(),
                "mode": f"{f.mode:o}",
            }
            for f in presented.files
        ],
        "workspace_env": harness.workspace_env,
        "port": PORT,
        "timeout": 90,
    }


def _assert_round_trip(
    harness: HarnessClass,
    presented: PresentedCredentials,
    stored_hash: str,
    wire: str,
    report: dict[str, object],
) -> None:
    assert report["returncode"] == 0, report["stderr"]
    requests = report["requests"]
    assert isinstance(requests, list)
    paths = [(r["method"], r["path"].split("?", 1)[0]) for r in requests]
    assert paths == [("POST", harness.model_path)], paths

    # The raw header list, not a dict: a repeated header must not hide an
    # earlier value behind a later one.
    credential = [
        (k.lower(), v) for k, v in requests[0]["headers"] if k.lower() in CREDENTIAL_HEADERS
    ]
    names = [k for k, _ in credential]
    assert names == [harness.auth_header], names
    value = credential[0][1]
    assert value.startswith(harness.auth_prefix), "unexpected auth scheme"
    auth = value.removeprefix(harness.auth_prefix)
    assert hashlib.sha256(auth.encode()).hexdigest() == stored_hash
    assert wire not in requests[0]["body"]

    env = report["env"]
    assert isinstance(env, dict)
    # Every credential-named variable, presented or inherited, holds the wire.
    stray = sorted(k for k, v in env.items() if CREDENTIAL_ENV_NAME.search(k) and v != wire)
    assert not stray, f"credential env other than the capability: {stray}"
    cmdlines = report["cmdlines"]
    assert isinstance(cmdlines, list)
    assert [c for c in cmdlines if "PROBE" in c], f"CLI command line not sampled: {cmdlines}"
    unscanned = report["unscanned"]
    assert isinstance(unscanned, dict)
    assert not unscanned, f"sandbox file scan incomplete: {sorted(unscanned)}"
    hits = report["hits"]
    assert isinstance(hits, dict)
    assert not hits, f"wire or host canary on sandbox disk: {hits}"
    files = report["files"]
    assert isinstance(files, dict)

    haystacks = {
        "env": "\n".join(f"{k}={v}" for k, v in env.items()),
        "requests": json.dumps(requests),
        "command lines": "\n".join(cmdlines),
        **{f"file {p}": text for p, text in files.items()},
    }
    for where, text in haystacks.items():
        for canary in HOST_CANARIES.values():
            assert canary not in text, f"host canary in sandbox {where}"
        assert not CREDENTIAL_SHAPED.findall(text.replace(wire, "")), where


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_pinned_cli_sends_only_the_wire_capability(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, canary in HOST_CANARIES.items():
        monkeypatch.setenv(key, canary)
    harness = CLASSES[name]
    version = _docker(tmp_path, *harness.version_argv)
    assert version.stdout.strip() == harness.version, version.stderr

    entropy = SecretStr(secrets.token_urlsafe(32))
    wire = harness.presenter.wire_credential(entropy)
    stored_hash = hashlib.sha256(wire.get_secret_value().encode()).hexdigest()
    presented = harness.presenter.present(wire, BASE_URL)

    # The image runs as its own non-root user; the mount must be readable.
    tmp_path.chmod(0o755)
    for target, text in (
        ("spec.json", json.dumps(_spec(harness, presented))),
        ("probe.py", PROBE.read_text()),
    ):
        (tmp_path / target).write_text(text)
        (tmp_path / target).chmod(0o644)
    container = f"s1-credential-probe-{secrets.token_hex(6)}"
    needles = {"wire": wire.get_secret_value(), **HOST_CANARIES}
    try:
        with CommandLines(container) as cmdlines:
            run = _docker(
                tmp_path, "python3", "/probe/probe.py", "/probe/spec.json", name=container
            )
        assert run.returncode == 0, run.stderr
        report = {
            **json.loads(run.stdout),
            "cmdlines": sorted(cmdlines.lines),
            **scan_container(container, needles),
        }
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    _assert_round_trip(harness, presented, stored_hash, wire.get_secret_value(), report)
