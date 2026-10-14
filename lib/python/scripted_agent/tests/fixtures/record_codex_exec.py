"""Record ``codex exec --json`` from the REAL pinned codex binary, offline.

Usage: uv run python tests/fixtures/record_codex_exec.py <codex binary> <out.jsonl>
(v0.160.1_codex_exec_two-commands.jsonl: npm @openai/codex@0.160.1.)

Same local Responses fixture server as agentic_isolation's
generate_codex_rollout.py, so no credential and no model provider: two shell
calls, then a final message. Stdout is written as a replay recording: a
``_recording`` header carrying ``codex --version``, then each event with the
``_offset_ms`` it was printed at.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BINARY = sys.argv[1]
OUT = sys.argv[2]
COMMANDS = ["printf one", "printf two"]
reqs = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        reqs.append(1)
        n = len(reqs)
        if n <= len(COMMANDS):
            items = [
                {
                    "type": "function_call",
                    "call_id": f"c{n}",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": COMMANDS[n - 1]}),
                }
            ]
        else:
            items = [
                {
                    "type": "message",
                    "id": "m1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "FINAL_ANSWER"}],
                }
            ]
        ev = [{"type": "response.created", "response": {"id": f"r{n}"}}]
        ev += [{"type": "response.output_item.done", "item": i} for i in items]
        ev += [
            {
                "type": "response.completed",
                "response": {
                    "id": f"r{n}",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
            }
        ]
        body = "".join(
            "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in ev
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


version = subprocess.run(
    [BINARY, "--version"], capture_output=True, text=True, check=True
).stdout.split()[-1]
s = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=s.serve_forever, daemon=True).start()
with tempfile.TemporaryDirectory() as t:
    home = pathlib.Path(t) / "home"
    home.mkdir()
    work = pathlib.Path(t) / "w"
    work.mkdir()
    started = time.monotonic()
    proc = subprocess.Popen(
        [
            BINARY,
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-c",
            'model_provider="fixture"',
            "-c",
            "model_providers.fixture={"
            'name="fixture",'
            f'base_url="http://127.0.0.1:{s.server_port}/v1",'
            'wire_api="responses",requires_openai_auth=false,supports_websockets=false}',
            "run two commands",
        ],
        env={
            **os.environ,
            "CODEX_HOME": str(home),
            "CODEX_SQLITE_HOME": str(home),
            "CODEX_API_KEY": "fixture",
        },
        cwd=work,
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    events = []
    assert proc.stdout is not None
    for line in proc.stdout:
        offset = int((time.monotonic() - started) * 1000)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # codex prints non-JSON diagnostics; a recording holds events
        events.append({**event, "_offset_ms": offset})
    assert proc.wait(timeout=60) == 0
s.shutdown()
header = {
    "_recording": {
        "version": 1,
        "cli_version": version,
        "provider": "codex",
        "task": "two-commands",
        "recorded_at": datetime.now(UTC).isoformat(),
        "duration_ms": events[-1]["_offset_ms"],
        "event_count": len(events),
        "capture_method": "codex exec --json against a local Responses fixture server",
    }
}
pathlib.Path(OUT).write_text("".join(json.dumps(r) + "\n" for r in [header, *events]))
