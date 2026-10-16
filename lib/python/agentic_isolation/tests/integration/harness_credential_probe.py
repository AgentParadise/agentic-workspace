"""In-sandbox half of the harness credential round trip (ADR-042).

Standard library only: it runs inside a workspace image, which carries the
pinned CLIs but not this test tree. The host computes the presentation and
hands it over as a JSON spec on stdin, so the capability never sits in a
sandbox file; this script plays the sandbox and the fake
upstream at once:

1. writes the presented files under a fresh $HOME,
2. serves a fake Anthropic Messages / OpenAI Responses upstream on loopback,
3. runs the pinned CLI with the image env plus the presented env,
4. prints one JSON report: every request the upstream saw (method, path,
   headers, body), the CLI's exit and the full child env.

It neither scans files nor asserts. The host reads the stopped container's
filesystem and samples its command lines through the Docker daemon
(`harness_credential_scan.py`), so files the agent user cannot read are
still scanned, and the canaries it matches never enter the sandbox.

Usage: python3 harness_credential_probe.py < SPEC_JSON
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The upstream holds each reply this long so the host's command-line
# sampler sees the CLI process while it is waiting.
HOLD_SECONDS = 2.0


def _sse(events: list[dict[str, object]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _anthropic_reply(request: dict[str, object]) -> tuple[str, bytes]:
    message = {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "model": request.get("model", "probe"),
        "content": [{"type": "text", "text": "PROBE_OK"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    if not request.get("stream"):
        return "application/json", json.dumps(message).encode()
    start = dict(message, content=[], stop_reason=None)
    return "text/event-stream", _sse(
        [
            {"type": "message_start", "message": start},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "PROBE_OK"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
    )


def _openai_reply() -> tuple[str, bytes]:
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    return "text/event-stream", _sse(
        [
            {"type": "response.created", "response": {"id": "resp_probe"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "id": "msg_probe",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "PROBE_OK"}],
                },
            },
            {
                "type": "response.completed",
                "response": {"id": "resp_probe", "usage": usage},
            },
        ]
    )


def _serve(port: int, seen: list[dict[str, object]]) -> ThreadingHTTPServer:
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def _record(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": [[k, v] for k, v in self.headers.items()],
                    "body": body.decode("utf-8", "replace"),
                }
            )
            return body

        def _reply(self, status: int, kind: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._record()
            self._reply(404, "application/json", b"{}")

        def do_POST(self) -> None:
            body = self._record()
            time.sleep(HOLD_SECONDS)
            path = self.path.split("?", 1)[0]
            if path.endswith("/v1/messages"):
                self._reply(200, *_anthropic_reply(json.loads(body or b"{}")))
            elif path.endswith("/v1/responses"):
                self._reply(200, *_openai_reply())
            else:
                self._reply(404, "application/json", b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", port), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    spec = json.load(sys.stdin)
    work = Path(tempfile.mkdtemp(prefix="credential-probe-"))
    home = work / "home"
    home.mkdir()
    child_tmp = work / "tmp"
    child_tmp.mkdir()
    for presented in spec["files"]:
        target = home / presented["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(presented["content"])
        target.chmod(int(presented["mode"], 8))
    env = {
        **os.environ,
        **{k: v.replace("{HOME}", str(home)) for k, v in spec["workspace_env"].items()},
        "HOME": str(home),
        "TMPDIR": str(child_tmp),
        **spec["env"],
    }
    seen: list[dict[str, object]] = []
    server = _serve(spec["port"], seen)
    try:
        run = subprocess.run(
            spec["argv"],
            env=env,
            cwd=work,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=spec.get("timeout", 60),
            check=False,
        )
    finally:
        server.shutdown()
    json.dump(
        {
            "returncode": run.returncode,
            "stdout": run.stdout[-4000:],
            "stderr": run.stderr[-4000:],
            "requests": seen,
            "env": env,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
