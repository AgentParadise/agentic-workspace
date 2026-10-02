"""Pinned Claude and Codex, depth three, in the workspace's real layout.

agentic-workspace#19, #20 and #21. Offline Messages and Responses fixtures,
no credentials, network-disabled container. Unlike
``test_pinned_cross_harness.py`` nothing a delegate needs is under its
working directory: home, spool and journal are siblings of the workspace and
the transcript roots are linked into the spool partition as session-store
init links them. So a Codex delegate reaches the journal and the network only
through what ``syn-delegate`` grants it.

Run inside the pinned image with the Codex sandbox seccomp profile, from a
directory outside ``/tmp`` (``/tmp`` is a Codex writable root):

    AGENTIC_PINNED_ROOT=/home/agent/pinned \\
    AGENTIC_PINNED_WORKSPACE=/workspace AGENTIC_PINNED_SPOOL=/spool \\
    CLAUDE_NATIVE_TEST_BINARY=$(command -v claude) \\
    CODEX_NATIVE_TEST_BINARY=$(command -v codex) \\
    python3 tests/test_pinned_depth_three.py
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

if __package__:
    from .native_fixture_protocol import messages, responses
else:
    from native_fixture_protocol import messages, responses

from agentic_session_store.child_journal import ChildIntent, ChildJournal
from agentic_session_store.install_child_hooks import install

CLAUDE = os.environ.get("CLAUDE_NATIVE_TEST_BINARY")
CODEX = os.environ.get("CODEX_NATIVE_TEST_BINARY")
ENABLED = bool(CLAUDE and CODEX)
TOKEN = re.compile(r"FIXTURE_[A-Z_]+")


@dataclass
class Layout:
    """Home, workspace and spool, none inside another.

    By default all three are siblings under ``AGENTIC_PINNED_ROOT`` (or the
    test's temporary directory). ``AGENTIC_PINNED_WORKSPACE`` and
    ``AGENTIC_PINNED_SPOOL`` place the workspace and spool at the image's
    real ``/workspace`` and ``/spool``, which an AppArmor host requires: the
    Codex sandbox profile admits writable roots only there.
    """

    home: Path
    workspace: Path
    spool: Path
    partition: str

    @classmethod
    def create(cls, tmp_path: Path, name: str, partition: str | None = None) -> Layout:
        root = os.environ.get("AGENTIC_PINNED_ROOT")
        base = (Path(root) if root else tmp_path) / name
        workspace_root = os.environ.get("AGENTIC_PINNED_WORKSPACE")
        spool_root = os.environ.get("AGENTIC_PINNED_SPOOL")
        layout = cls(
            home=base / "home",
            workspace=Path(workspace_root) / name
            if workspace_root
            else base / "workspace",
            spool=Path(spool_root) if spool_root else base / "spool",
            partition=partition or name,
        )
        for directory in (
            base,
            layout.workspace,
            layout.journal.parent,
            layout.spool / layout.partition,
        ):
            # Under /tmp (or $TMPDIR) every directory would be a Codex
            # writable root, and the test would prove nothing about the grant.
            for writable in {"/tmp", os.environ.get("TMPDIR") or "/tmp"}:
                assert not directory.resolve().is_relative_to(
                    Path(writable).resolve()
                ), "Set AGENTIC_PINNED_ROOT outside Codex writable roots"
            if directory.exists():
                shutil.rmtree(directory)
        return layout

    @property
    def journal(self) -> Path:
        return (
            self.spool / ".agentic-session-store" / self.partition / "children.sqlite"
        )

    @property
    def claude_transcripts(self) -> Path:
        return self.spool / self.partition / "claude"

    @property
    def codex_transcripts(self) -> Path:
        return self.spool / self.partition / "codex"


class Fixture:
    """One offline model for both harnesses, scripted per prompt token.

    Each agent's first request gets its scripted shell command (if any); every
    later request gets a final answer. Requests are counted per prompt.
    """

    def __init__(self, script: dict[str, str | None]) -> None:
        self.script = script
        self.requests: dict[str, int] = {}
        self.tools: dict[str, list[str]] = {}
        self.results: dict[str, list[str]] = {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                codex = self.path.startswith("/v1/responses")
                token = fixture.token(request, codex)
                count = fixture.requests.get(token, 0)
                fixture.requests[token] = count + 1
                if not codex:
                    fixture.tools.setdefault(
                        token, [t.get("name", "") for t in request.get("tools", [])]
                    )
                fixture.results.setdefault(token, []).extend(
                    fixture.outputs(request, codex)
                )
                command = fixture.script.get(token) if count == 0 else None
                (responses if codex else messages)(self, command)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def token(request: dict, codex: bool) -> str:
        texts: list[str] = []
        if codex:
            for item in request.get("input", []):
                if item.get("type") == "message" and item.get("role") == "user":
                    texts += [c.get("text", "") for c in item.get("content", [])]
        else:
            first = request.get("messages", [{}])[0].get("content", [])
            texts = (
                [first]
                if isinstance(first, str)
                else [b.get("text", "") for b in first if isinstance(b, dict)]
            )
        for text in texts:
            if (match := TOKEN.search(text)) is not None:
                return match.group(0)
        return "UNKNOWN"

    @staticmethod
    def outputs(request: dict, codex: bool) -> list[str]:
        if codex:
            return [
                str(item.get("output", ""))
                for item in request.get("input", [])
                if item.get("type") == "function_call_output"
            ]
        return [
            str(block.get("content", ""))
            for message in request.get("messages", [])
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self.server.server_port


def _prepare(layout: Layout, port: int) -> dict[str, str]:
    for directory in (
        layout.workspace,
        layout.home / ".claude",
        layout.home / ".codex",
        layout.journal.parent,
        layout.claude_transcripts,
        layout.codex_transcripts,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    # As session-store init links them: transcripts live on the spool.
    (layout.home / ".claude" / "projects").symlink_to(layout.claude_transcripts)
    (layout.home / ".codex" / "sessions").symlink_to(layout.codex_transcripts)
    install(layout.home / ".claude" / "settings.json", harness="claude")
    install(layout.home / ".codex" / "config.toml")
    ChildJournal(layout.journal)
    import tomlkit

    path = layout.home / ".codex" / "config.toml"
    document = tomlkit.parse(path.read_text())
    # Fixture-local policies, never installed by the shim. No network and no
    # writable roots here: those must come from syn-delegate.
    document["model_provider"] = "fixture"
    document["approval_policy"] = "never"
    document["model_providers"] = {
        "fixture": {
            "name": "fixture",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": False,
        }
    }
    path.write_text(tomlkit.dumps(document))
    environment = {
        **os.environ,
        "HOME": str(layout.home),
        "CLAUDE_CONFIG_DIR": str(layout.home / ".claude"),
        "CODEX_HOME": str(layout.home / ".codex"),
        "CODEX_SQLITE_HOME": str(layout.home / ".codex"),
        "ANTHROPIC_API_KEY": "fixture",
        "CODEX_API_KEY": "fixture",
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "AGENTIC_SESSION_STORE_PROVIDER": "local",
        "AGENTIC_SESSION_STORE_SPOOL": str(layout.spool),
        "AGENTIC_SESSION_STORE_PARTITION": layout.partition,
        "AGENTIC_INVOCATION_ID": "invocation",
        "AGENTIC_ATTEMPT_ID": "attempt",
    }
    # $TMPDIR is a Codex writable root; the layout must not depend on it.
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "CODEX_THREAD_ID", "TMPDIR"):
        environment.pop(key, None)
    return environment


def _runner() -> str:
    runner = os.environ.get("DELEGATE_NATIVE_TEST_BINARY")
    return (
        shlex.quote(runner)
        if runner
        else shlex.quote(sys.executable) + " -m agentic_session_store.delegate"
    )


def _root_claude(layout: Layout, environment: dict[str, str]) -> str:
    # As Syntropic137 launches a Claude phase: -p before the variadic --tools.
    result = subprocess.run(
        [
            CLAUDE or "claude",
            "--model",
            "claude-sonnet-4-5",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "-p",
            "FIXTURE_ROOT",
            "--max-turns",
            "4",
            "--tools",
            "Bash",
        ],
        env=environment,
        cwd=layout.workspace,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-2000:])
    return json.loads(result.stdout)["session_id"]


def _root_codex(layout: Layout, environment: dict[str, str]) -> str:
    # Syntropic137's default codex phase level (full-access) is the root
    # here; syn-delegate itself never uses it.
    result = subprocess.run(
        [
            CODEX or "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
            "--",
            "FIXTURE_ROOT",
        ],
        env=environment,
        cwd=layout.workspace,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-2000:])
    first = json.loads(result.stdout.splitlines()[0])
    assert first["type"] == "thread.started"
    return first["thread_id"]


def _latest(layout: Layout) -> list[ChildIntent]:
    latest = {
        change.intent.child_invocation_id: change.intent
        for change in ChildJournal(layout.journal).page().changes
    }
    return sorted(latest.values(), key=lambda intent: intent.sequence)


def _claude_sessions(layout: Layout) -> set[str]:
    return {
        row["sessionId"]
        for path in layout.claude_transcripts.rglob("*.jsonl")
        for line in path.read_text().splitlines()
        if (row := json.loads(line)).get("sessionId")
    }


def _codex_sessions(layout: Layout) -> set[str]:
    return {
        json.loads(path.read_text().splitlines()[0])["payload"]["id"]
        for path in layout.codex_transcripts.rglob("*.jsonl")
    }


def _check_versions() -> None:
    for binary, version in (
        (CLAUDE, "2.1.281 (Claude Code)"),
        (CODEX, "codex-cli 0.156.1"),
    ):
        assert (
            subprocess.check_output([binary or "", "--version"], text=True).strip()
            == version
        )


def _outside_grant(layout: Layout) -> dict[str, Path]:
    """Spool locations a workspace-write Codex delegate must not write."""
    name = "outside-grant-probe"
    return {
        "spool-root": layout.spool / name,
        "partition-root": layout.spool / layout.partition / name,
        "codex-root": layout.codex_transcripts / name,
        "metadata-namespace": layout.spool / ".agentic-session-store" / name,
    }


def _completed(intent: ChildIntent) -> bool:
    return intent.status == "completed" and intent.exit_code == 0


@unittest.skipUnless(ENABLED, "Set both pinned native test binaries")
def test_claude_codex_claude(tmp_path: Path) -> None:
    """Claude -> Codex (workspace-write) -> Claude: the Codex delegate writes
    the journal and reaches the model only through syn-delegate's grant, the
    Claude grandchild runs a writing Bash command (#20), and the grandchild's
    own transcript is captured on the spool."""
    _check_versions()
    # Nested, as Syntropic137 builds partitions (<execution>/<workspace>).
    layout = Layout.create(
        tmp_path, "claude-codex-claude", partition="exec-1/claude-codex-claude"
    )
    runner = _runner()
    script = {
        "FIXTURE_ROOT": runner + " codex --prompt FIXTURE_CODEX_CHILD --timeout 90",
        # Inside the Codex sandbox: every spool location outside the grant
        # must refuse a write, then the nested launch runs.
        "FIXTURE_CODEX_CHILD": " ".join(
            f"(: > {shlex.quote(str(path))} && echo WROTE:{label} || echo DENIED:{label});"
            for label, path in _outside_grant(layout).items()
        )
        + " "
        + runner
        + " claude --prompt FIXTURE_CLAUDE_GRANDCHILD --model claude-sonnet-4-5"
        " --timeout 60",
        "FIXTURE_CLAUDE_GRANDCHILD": "touch grandchild-wrote && echo GRANDCHILD_RAN",
    }
    with Fixture(script) as fixture:
        environment = _prepare(layout, fixture.port)
        root = _root_claude(layout, environment)
    sandboxed = "".join(fixture.results["FIXTURE_CODEX_CHILD"])
    for label, path in _outside_grant(layout).items():
        assert f"DENIED:{label}" in sandboxed and f"WROTE:{label}" not in sandboxed, (
            label,
            sandboxed,
        )
        assert not path.exists(), path
    intents = _latest(layout)
    assert len(intents) == 2, (intents, fixture.results)
    codex_child, claude_grandchild = intents
    assert (codex_child.call.harness, codex_child.call.target_harness) == (
        "claude",
        "codex",
    )
    assert codex_child.call.parent_native_id == root
    assert (claude_grandchild.call.harness, claude_grandchild.call.target_harness) == (
        "codex",
        "claude",
    )
    assert claude_grandchild.call.parent_native_id == codex_child.child_native_id
    assert None not in {codex_child.child_native_id, claude_grandchild.child_native_id}
    assert (
        len({root, codex_child.child_native_id, claude_grandchild.child_native_id}) == 3
    )
    assert all(_completed(intent) for intent in intents), intents
    # The grandchild's Bash write ran (dontAsk with the Codex parent's tools),
    # inside the Codex sandbox's writable workspace.
    assert (layout.workspace / "grandchild-wrote").exists()
    assert any(
        "GRANDCHILD_RAN" in r for r in fixture.results["FIXTURE_CLAUDE_GRANDCHILD"]
    )
    assert codex_child.child_native_id in _codex_sessions(layout)
    # Every node's own transcript is captured, the grandchild's included: the
    # Codex delegate's grant has this partition's Claude transcript root
    # (`$SPOOL/$PARTITION/claude`), where `~/.claude/projects` is linked.
    # Before that grant Claude ran without it (exit 0, no file).
    assert {root, claude_grandchild.child_native_id} <= _claude_sessions(layout)


@unittest.skipUnless(ENABLED, "Set both pinned native test binaries")
def test_codex_claude_codex(tmp_path: Path) -> None:
    """Codex -> Claude -> Codex: the delegated Claude can run Bash (#20), so it
    can delegate again."""
    _check_versions()
    layout = Layout.create(tmp_path, "codex-claude-codex")
    runner = _runner()
    script = {
        "FIXTURE_ROOT": runner
        + " claude --prompt FIXTURE_CLAUDE_CHILD --model claude-sonnet-4-5 --timeout 90",
        "FIXTURE_CLAUDE_CHILD": runner
        + " codex --prompt FIXTURE_CODEX_GRANDCHILD --timeout 60",
        "FIXTURE_CODEX_GRANDCHILD": "touch grandchild-wrote",
    }
    with Fixture(script) as fixture:
        environment = _prepare(layout, fixture.port)
        root = _root_codex(layout, environment)
    intents = _latest(layout)
    assert len(intents) == 2, (intents, fixture.results)
    claude_child, codex_grandchild = intents
    assert (claude_child.call.harness, claude_child.call.target_harness) == (
        "codex",
        "claude",
    )
    assert claude_child.call.parent_native_id == root
    assert (codex_grandchild.call.harness, codex_grandchild.call.target_harness) == (
        "claude",
        "codex",
    )
    assert codex_grandchild.call.parent_native_id == claude_child.child_native_id
    assert None not in {claude_child.child_native_id, codex_grandchild.child_native_id}
    assert (
        len({root, claude_child.child_native_id, codex_grandchild.child_native_id}) == 3
    )
    assert all(_completed(intent) for intent in intents), intents
    assert (layout.workspace / "grandchild-wrote").exists()
    assert {root, codex_grandchild.child_native_id} <= _codex_sessions(layout)
    assert claude_child.child_native_id in _claude_sessions(layout)


@unittest.skipUnless(ENABLED, "Set both pinned native test binaries")
def test_claude_claude_inherits_the_parent_grant(tmp_path: Path) -> None:
    """Claude -> Claude with a credential Claude reads itself (an API key):
    the child gets the parent's mode and --tools, and its Bash write runs."""
    _check_versions()
    layout = Layout.create(tmp_path, "claude-claude-api-key")
    runner = _runner()
    script = {
        "FIXTURE_ROOT": runner
        + " claude --prompt FIXTURE_CLAUDE_CHILD --model claude-sonnet-4-5 --timeout 60",
        "FIXTURE_CLAUDE_CHILD": "touch child-wrote && echo CHILD_RAN",
    }
    with Fixture(script) as fixture:
        environment = _prepare(layout, fixture.port)
        root = _root_claude(layout, environment)
    (child,) = _latest(layout)
    assert child.call.parent_native_id == root
    assert _completed(child), child
    assert (layout.workspace / "child-wrote").exists()
    # The parent ran with --tools Bash; so does the child, and nothing more.
    assert fixture.tools["FIXTURE_ROOT"] == ["Bash"]
    assert fixture.tools["FIXTURE_CLAUDE_CHILD"] == ["Bash"]
    assert {root, child.child_native_id} <= _claude_sessions(layout)


@unittest.skipUnless(ENABLED, "Set both pinned native test binaries")
def test_claude_claude_without_a_readable_credential_is_refused(
    tmp_path: Path,
) -> None:
    """Claude Code removes CLAUDE_CODE_OAUTH_TOKEN from its Bash subprocesses,
    so with OAuth only the delegated Claude is refused with a reason (#21)."""
    _check_versions()
    layout = Layout.create(tmp_path, "claude-claude-oauth")
    runner = _runner()
    script = {
        "FIXTURE_ROOT": runner + " claude --prompt FIXTURE_CLAUDE_CHILD --timeout 60",
    }
    with Fixture(script) as fixture:
        environment = _prepare(layout, fixture.port)
        environment.pop("ANTHROPIC_API_KEY")
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat01-fixture-not-a-secret"
        root = _root_claude(layout, environment)
    (child,) = _latest(layout)
    assert child.call.parent_native_id == root
    assert (child.status, child.reason) == (
        "launch_failed",
        "claude_nested_auth_unavailable",
    )
    assert child.child_native_id is None
    assert "FIXTURE_CLAUDE_CHILD" not in fixture.requests
    assert any(
        "removes its OAuth token from Bash subprocesses" in result
        for result in fixture.results["FIXTURE_ROOT"]
    ), fixture.results


@unittest.skipUnless(ENABLED, "Set both pinned native test binaries")
def test_journal_denied_nested_launch_is_recorded(tmp_path: Path) -> None:
    """A read-only Codex delegate cannot write the journal. Its nested
    syn-delegate starts nothing and exits 70; the enclosing syn-delegate
    records that launch as launch_failed under the Codex child (#19)."""
    _check_versions()
    layout = Layout.create(tmp_path, "journal-denied")
    runner = _runner()
    script = {
        "FIXTURE_ROOT": runner
        + " codex --sandbox read-only --prompt FIXTURE_CODEX_CHILD --timeout 90",
        "FIXTURE_CODEX_CHILD": runner + " claude --prompt FIXTURE_NEVER --timeout 30",
    }
    with Fixture(script) as fixture:
        environment = _prepare(layout, fixture.port)
        root = _root_claude(layout, environment)
    intents = _latest(layout)
    assert len(intents) == 2, (intents, fixture.results)
    codex_child, denied = intents
    assert codex_child.call.parent_native_id == root
    assert _completed(codex_child), codex_child
    assert (denied.call.harness, denied.call.target_harness) == ("codex", "claude")
    assert denied.call.parent_native_id == codex_child.child_native_id
    assert (denied.status, denied.reason) == (
        "launch_failed",
        "nested_journal_unavailable",
    )
    assert denied.child_native_id is None
    assert "FIXTURE_NEVER" not in fixture.requests


if __name__ == "__main__":
    tests = [
        test_claude_codex_claude,
        test_codex_claude_codex,
        test_claude_claude_inherits_the_parent_grant,
        test_claude_claude_without_a_readable_credential_is_refused,
        test_journal_denied_nested_launch_is_recorded,
    ]
    selected = sys.argv[1:]
    for test in tests:
        if selected and test.__name__ not in selected:
            continue
        with tempfile.TemporaryDirectory() as directory:
            test(Path(directory))
        print("PASSED", test.__name__, flush=True)
