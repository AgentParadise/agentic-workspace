"""A DEGRADED workspace launches native children even when nothing can record
them (#27). Pinned Claude 2.1.281 and Codex 0.156.1, offline.

test_pinned_fail_closed proves the installed guard DENIES a launch it cannot
record. That is right while capture is on. When the session-store capability
degrades, the entrypoint runs the adapter's degrade.sh, which uninstalls the
hooks; these cases install, uninstall, then make the recorder unavailable (no
python3 on PATH) and check the child RUNS and no denial reaches the parent.
The fail-closed cases are the control: same fixture, hooks left in, denied.

Set CLAUDE_NATIVE_TEST_BINARY and/or CODEX_NATIVE_TEST_BINARY to opt in. Run
inside the pinned image with networking disabled.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentic_session_store.hook_command import FAILURE_MESSAGE
from agentic_session_store.install_child_hooks import install, uninstall
from tests.test_pinned_fail_closed import (
    CLAUDE,
    CODEX,
    _capture_env,
    _claude_reply,
    _codex_reply,
    _path,
    _serve,
)

MODE = "missing_interpreter"


class PinnedDegradedLaunch(unittest.TestCase):
    @unittest.skipUnless(CLAUDE, "Set CLAUDE_NATIVE_TEST_BINARY")
    def test_claude_child_launches_after_degrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            settings = home / ".claude/settings.json"
            install(settings, harness="claude")
            self.assertTrue(uninstall(settings, harness="claude"))
            server, requests = _serve(_claude_reply)
            env = {
                **os.environ,
                **_capture_env(root, MODE),
                "PATH": _path(root, MODE),
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "ANTHROPIC_API_KEY": "fixture",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            try:
                result = subprocess.run(
                    [
                        CLAUDE,
                        "-p",
                        "FIXTURE_PARENT",
                        "--model",
                        "claude-sonnet-4-5",
                        "--tools",
                        "Agent",
                        "--allowedTools",
                        "Agent",
                        "--settings",
                        str(settings),
                        "--setting-sources",
                        "user",
                        "--output-format",
                        "json",
                        "--max-turns",
                        "3",
                    ],
                    env=env,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()
            self.assertEqual(result.returncode, 0, result.stderr)
            prompts = json.dumps([r["messages"][0] for r in requests])
            self.assertIn("FIXTURE_CHILD", prompts, "the child must run")
            self.assertNotIn(FAILURE_MESSAGE, json.dumps(requests))

    @unittest.skipUnless(CODEX, "Set CODEX_NATIVE_TEST_BINARY")
    def test_codex_child_launches_after_degrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "codex"
            home.mkdir()
            install(home / "config.toml")
            self.assertTrue(uninstall(home / "config.toml"))
            server, requests = _serve(_codex_reply)
            try:
                result = subprocess.run(
                    [
                        CODEX,
                        "exec",
                        "--skip-git-repo-check",
                        "--json",
                        "-c",
                        'model_provider="fixture"',
                        "-c",
                        'model_providers.fixture={name="fixture",base_url="http://127.0.0.1:'
                        + str(server.server_port)
                        + '/v1",wire_api="responses",requires_openai_auth=false,supports_websockets=false}',
                        "-c",
                        "features.multi_agent=true",
                        "-c",
                        "features.multi_agent_v2=true",
                        "Spawn a child.",
                    ],
                    env={
                        **os.environ,
                        **_capture_env(root, MODE),
                        "PATH": _path(root, MODE),
                        "CODEX_HOME": str(home),
                        "CODEX_SQLITE_HOME": str(home),
                        "CODEX_API_KEY": "fixture",
                    },
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(FAILURE_MESSAGE, json.dumps(requests))
            # The parent, its follow-up AND the child each made a request.
            self.assertGreater(len(requests), 2, json.dumps(requests)[:2000])
            self.assertIn("FIXTURE_CHILD", json.dumps(requests))


if __name__ == "__main__":
    unittest.main()
