"""Run a delegate with durable intent, native binding and its own exit status.

Parent identity comes from the harness hook or native shell environment. Shell
text is never scanned to decide whether delegation happened.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from agentic_session_store.child_hook import InvocationEnv, _identity, _parse
from agentic_session_store.child_journal import (
    ChildCall,
    ChildJournal,
    LaunchFailureReason,
)
from agentic_session_store.claude_permissions import (
    CLAUDE_PID_ENV,
    PERMISSIONS_ENV,
    ClaudePermissions,
    delegated_permissions,
)
from agentic_session_store.codex_sandbox import (
    SANDBOX_MODE_ENV,
    CodexSandboxMode,
    SandboxGrant,
    probe,
    resolve_sandbox_mode,
)
from agentic_session_store.contract import METADATA_NAMESPACE, SessionStoreContract
from agentic_session_store.hook_command import HookHarness
from agentic_session_store.hook_probe import (
    CaptureProbeError,
    probe_guard,
    without_untrusted_startup,
)

MAX_LINE_BYTES = 1024 * 1024
RECORD_ERRORS = (ValueError, TypeError, OSError, sqlite3.Error)

# Exit statuses, sysexits(3)-style where one fits.
EXIT_SANDBOX_UNAVAILABLE = 69  # EX_UNAVAILABLE: Codex sandbox cannot start here
EXIT_CONTEXT_UNAVAILABLE = 70  # durable parent context or storage unavailable
EXIT_START_FAILED = 127  # the delegate binary could not be executed
AUTH_PROBE_TIMEOUT_SECONDS = 30.0

# A nested syn-delegate that cannot write the journal prints this line, then
# the JSON object {"id": <uuid>, "target": <harness>}, on stderr. The enclosing
# syn-delegate, which can write the journal, reads it from its own child's
# machine stream and records a launch_failed intent under that child. It can
# only ever add a failed launch (a gap), never a binding or a success, so a
# forged line can make coverage less complete, never more.
DENIAL_MARKER = "agentic-delegate-launch-denied/v1"
MAX_DENIALS = 64
# Each delegate hands its child a fresh nonce; a nested syn-delegate repeats
# the one it inherited in its denial line, and the enclosing delegate accepts
# only its own. So output copied from elsewhere (a log, another delegate's
# run) is not mistaken for this child's denial. It is not a secret: the agent
# can read it, and a forged line can still only add a failed launch.
DENIAL_NONCE_ENV = "AGENTIC_DELEGATE_DENIAL_NONCE"


def launch_context(
    target: str, environment: Mapping[str, str]
) -> tuple[ChildJournal, ChildCall, SessionStoreContract]:
    contract = SessionStoreContract.from_env(environment)
    if contract is None:
        raise ValueError("Durable delegation requires session capture")
    harness = environment.get("AGENTIC_PARENT_HARNESS")
    parent = environment.get("AGENTIC_PARENT_NATIVE_ID")
    if harness is None and parent is None:
        # Codex supplies this for shell execution. Never infer parentage from a process list or command text.
        harness, parent = "codex", environment.get("CODEX_THREAD_ID")
    call = ChildCall(
        invocation_id=_identity(environment.get(InvocationEnv.INVOCATION_ID)),
        attempt_id=_identity(environment.get(InvocationEnv.ATTEMPT_ID)),
        harness=_identity(harness),
        parent_native_id=_identity(parent),
        tool_call_id=str(uuid4()),
        target_harness=target,
    )
    journal = ChildJournal(
        Path(contract.spool)
        / METADATA_NAMESPACE
        / contract.partition
        / "children.sqlite"
    )
    journal.register(call)
    return journal, call, contract


def denial_line(target: str, nonce: str | None) -> str:
    return (
        DENIAL_MARKER
        + " "
        + json.dumps({"id": str(uuid4()), "target": target, "nonce": nonce})
    )


def _tool_outputs(event: dict[str, object], harness: str) -> list[str]:
    """Shell tool output in a delegate's machine stream, per harness format."""
    if harness == "codex":
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "command_execution"
            and isinstance(item.get("aggregated_output"), str)
        ):
            return [item["aggregated_output"]]
        return []
    message = event.get("message")
    if event.get("type") != "user" or not isinstance(message, dict):
        return []
    content = message.get("content")
    outputs: list[str] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            outputs.append(inner)
        elif isinstance(inner, list):
            outputs.extend(
                part["text"]
                for part in inner
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return outputs


def denials(line: bytes, harness: str, nonce: str) -> list[tuple[str, str]]:
    """(id, target) of every nested denial with this nonce in one frame."""
    if DENIAL_MARKER.encode() not in line:
        return []
    try:
        event = _parse(line)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return []
    found: list[tuple[str, str]] = []
    for output in _tool_outputs(event, harness):
        for text in output.splitlines():
            text = text.strip()
            if not text.startswith(DENIAL_MARKER + " "):
                continue
            try:
                record = json.loads(text[len(DENIAL_MARKER) + 1 :])
                denial = (_identity(record["id"]), str(record["target"]))
                if record["nonce"] != nonce:
                    continue
            except (ValueError, TypeError, KeyError, RecursionError):
                continue
            if denial[1] in {"claude", "codex"} and len(denial[0]) <= 64:
                found.append(denial)
    return found


def native_identity(line: bytes, harness: str) -> str | None:
    try:
        event = _parse(line)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if harness == "codex" and event.get("type") == "thread.started":
        return _identity(event.get("thread_id"))
    if (
        harness == "claude"
        and event.get("type") == "system"
        and event.get("subtype") == "init"
        and event.get("parent_tool_use_id") is None
    ):
        return _identity(event.get("session_id"))
    return None


def _record(action, *args) -> None:
    try:
        action(*args)
    except RECORD_ERRORS:
        print(
            "Delegate capture record failed; retained intent requires recovery.",
            file=sys.stderr,
        )


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


class _Observed:
    """What the enclosing delegate has seen of its own child so far."""

    def __init__(self, nonce: str) -> None:
        self.native: str | None = None
        self.denials: set[str] = set()
        self.nonce = nonce


def _record_denials(
    line: bytes, journal: ChildJournal, call: ChildCall, observed: _Observed
) -> None:
    harness = call.target_harness or call.harness
    for denial_id, target in denials(line, harness, observed.nonce):
        if denial_id in observed.denials:
            continue
        if observed.native is None or len(observed.denials) >= MAX_DENIALS:
            print(
                "Delegate child reported a denied nested launch that could not "
                "be recorded.",
                file=sys.stderr,
            )
            return
        observed.denials.add(denial_id)
        nested = ChildCall(
            invocation_id=call.invocation_id,
            attempt_id=call.attempt_id,
            harness=harness,
            parent_native_id=observed.native,
            tool_call_id="denied-" + denial_id,
            target_harness=target,
        )
        _record(journal.register, nested)
        _record(
            journal.launch_failed,
            nested,
            LaunchFailureReason.NESTED_JOURNAL_UNAVAILABLE,
        )


def _bind_line(
    line: bytes, journal: ChildJournal, call: ChildCall, observed: _Observed
) -> None:
    if len(line) > MAX_LINE_BYTES:
        print("Delegate capture frame exceeded its byte limit.", file=sys.stderr)
        return
    try:
        native = native_identity(line, call.target_harness or call.harness)
        if native is not None:
            _record(journal.bind, call, native)
            if observed.native is None:
                observed.native = native
        _record_denials(line, journal, call, observed)
    except RECORD_ERRORS:
        print("Delegate emitted an invalid native identity.", file=sys.stderr)


def _stream(
    process: subprocess.Popen,
    journal: ChildJournal,
    call: ChildCall,
    timeout: float,
    nonce: str,
) -> int:
    deadline = time.monotonic() + timeout
    observed = _Observed(nonce)
    pending = b""
    discard = False
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                return process.returncode
            if not selector.select(min(remaining, 1)):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                if pending and not discard:
                    _bind_line(pending, journal, call, observed)
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            frames = (pending + chunk).split(b"\n")
            pending = frames.pop()
            for line in frames:
                if not discard:
                    _bind_line(line, journal, call, observed)
                discard = False
            if len(pending) > MAX_LINE_BYTES:
                pending = b""
                discard = True
                print(
                    "Delegate capture frame exceeded its byte limit.", file=sys.stderr
                )
    remaining = deadline - time.monotonic()
    try:
        return process.wait(timeout=max(0, remaining))
    except subprocess.TimeoutExpired:
        _terminate(process)
        return process.returncode


def child_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The delegate's environment: the parent's, minus parent-identity markers
    and any shell startup file the agent could edit (its hooks inherit it)."""
    child = without_untrusted_startup(environment)
    for key in (
        "AGENTIC_PARENT_HARNESS",
        "AGENTIC_PARENT_NATIVE_ID",
        "CODEX_THREAD_ID",
        "CLAUDECODE",
        CLAUDE_PID_ENV,
        PERMISSIONS_ENV,
        DENIAL_NONCE_ENV,
    ):
        child.pop(key, None)
    return child


def run(
    command: list[str], journal: ChildJournal, call: ChildCall, timeout: float
) -> int:
    environment = child_environment(os.environ)
    nonce = str(uuid4())
    environment[DENIAL_NONCE_ENV] = nonce
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            start_new_session=True,
            env=environment,
        )
    except OSError:
        _record(journal.launch_failed, call, LaunchFailureReason.PROCESS_START_FAILED)
        print("Delegate process could not start.", file=sys.stderr)
        return EXIT_START_FAILED
    _record(journal.launched, call)
    previous = {}

    def cancel(signum, _frame):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
        _terminate(process)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, cancel)
    try:
        code = _stream(process, journal, call, timeout, nonce)
    finally:
        _terminate(process)
        process.stdout.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _record(journal.finished, call, process.returncode)
    return code if code >= 0 else 128 - code


def codex_command(sandbox: CodexSandboxMode, grant: SandboxGrant) -> list[str]:
    """Codex always gets an explicit sandbox; its default is not relied on."""
    return [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox.value,
        *grant.config(sandbox),
    ]


def claude_transcript_root(contract: SessionStoreContract) -> str:
    """``$SPOOL/$PARTITION/claude``: where session-store init links
    ``~/.claude/projects``, so where a Claude grandchild writes its transcript."""
    # abspath, not resolve: no filesystem access, so nothing can fail here
    # after the intent is registered.
    return os.path.abspath(os.path.join(contract.spool, contract.partition, "claude"))


def nested_grant(journal: ChildJournal, contract: SessionStoreContract) -> SandboxGrant:
    """What a nested syn-delegate needs, nothing else: the journal's own
    directory (SQLite also writes its rollback journal there), this
    partition's Claude transcript root (so a Claude grandchild's transcript
    is captured, not silently dropped), and the network.

    Exactly this partition's ``claude`` directory: never the spool root, the
    partition root, the Codex transcript root or the metadata namespace.
    """
    return SandboxGrant(
        writable_roots=(
            os.path.abspath(journal.path.parent),
            claude_transcript_root(contract),
        ),
        network_access=True,
    )


def claude_command(permissions: ClaudePermissions) -> list[str]:
    return [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        *permissions.arguments(),
    ]


def refuse_without_sandbox(
    journal: ChildJournal,
    call: ChildCall,
    contract: SessionStoreContract,
    sandbox: CodexSandboxMode,
    environment: Mapping[str, str],
) -> int | None:
    """Record launch_failed and return an exit status if Codex cannot sandbox.

    Probes live with the delegate's own mode, directory and environment.
    """
    status = probe(
        sandbox,
        environment=child_environment(environment),
        grant=nested_grant(journal, contract),
    )
    if status.available:
        return None
    _record(journal.launch_failed, call, LaunchFailureReason.CODEX_SANDBOX_UNAVAILABLE)
    print(
        "Delegate launch refused: the Codex sandbox is unavailable in this "
        f"workspace ({status.detail or 'no detail'}). Codex was not started; "
        "the workspace needs the Codex sandbox seccomp profile.",
        file=sys.stderr,
    )
    return EXIT_SANDBOX_UNAVAILABLE


def refuse_without_parent_permissions(
    journal: ChildJournal, call: ChildCall, environment: Mapping[str, str]
) -> tuple[ClaudePermissions | None, int | None]:
    """The delegated Claude's grant, or launch_failed when it is unknown."""
    try:
        return delegated_permissions(environment), None
    except (ValueError, TypeError, RecursionError):
        _record(
            journal.launch_failed,
            call,
            LaunchFailureReason.PARENT_PERMISSIONS_UNAVAILABLE,
        )
        print(
            "Delegate launch refused: the parent Claude session's permission "
            "mode and tools are unavailable, so the delegated Claude cannot be "
            "limited to them. Claude was not started.",
            file=sys.stderr,
        )
        return None, EXIT_CONTEXT_UNAVAILABLE


def claude_authenticated(environment: Mapping[str, str]) -> bool | None:
    """Ask the pinned Claude itself, offline, whether this environment can
    authenticate. ``None`` when it cannot be asked (the launch then records
    its own start failure)."""
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=dict(environment),
            timeout=AUTH_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return False
    try:
        status = json.loads(result.stdout)
    except (ValueError, RecursionError):
        return False
    return (
        result.returncode == 0
        and isinstance(status, dict)
        and status.get("loggedIn") is True
    )


def refuse_without_claude_auth(
    journal: ChildJournal, call: ChildCall, environment: Mapping[str, str]
) -> int | None:
    """Claude Code removes CLAUDE_CODE_OAUTH_TOKEN from the environment of its
    Bash subprocesses (measured at 2.1.281), so a Claude started below a
    Claude Bash call has no OAuth credential. It is never copied back in: a
    child is started only when Claude itself reports a credential it reads
    (for example ANTHROPIC_API_KEY or a credentials file)."""
    if claude_authenticated(child_environment(environment)) is not False:
        return None
    _record(
        journal.launch_failed, call, LaunchFailureReason.CLAUDE_NESTED_AUTH_UNAVAILABLE
    )
    print(
        "Delegate launch refused: a delegated Claude would have no credentials "
        "here. Claude Code removes its OAuth token from Bash subprocesses, so "
        "Claude cannot be delegated to from below a Claude session unless the "
        "workspace supplies a credential Claude reads itself (ANTHROPIC_API_KEY "
        "or a credentials file). Claude was not started.",
        file=sys.stderr,
    )
    return EXIT_SANDBOX_UNAVAILABLE


def refuse_without_capture_hooks(
    journal: ChildJournal,
    call: ChildCall,
    harness: str,
    environment: Mapping[str, str],
) -> int | None:
    """Record launch_failed if the delegate's own native hooks would fail open.

    The delegate inherits this environment, so a shell startup file set by the
    caller (for example BASH_ENV) would stop its capture hooks from running.
    """
    try:
        probe_guard(HookHarness(harness), child_environment(environment))
    except CaptureProbeError:
        _record(
            journal.launch_failed, call, LaunchFailureReason.CAPTURE_HOOK_UNREACHABLE
        )
        print(
            "Delegate launch refused: its native child capture hooks cannot "
            "run in this environment. The delegate was not started.",
            file=sys.stderr,
        )
        return EXIT_CONTEXT_UNAVAILABLE
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=("claude", "codex"))
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument(
        "--sandbox",
        help=(
            "Codex sandbox mode: read-only or workspace-write (default; "
            f"also {SANDBOX_MODE_ENV}). Modes that disable the sandbox are refused."
        ),
    )
    args = parser.parse_args()
    if not 0 < args.timeout <= 86400:
        parser.error("timeout must be between zero and one day")
    sandbox: CodexSandboxMode | None = None
    if args.harness == "codex":
        try:
            sandbox = resolve_sandbox_mode(args.sandbox, os.environ)
        except ValueError as error:
            parser.error(str(error))
    elif args.sandbox is not None:
        parser.error("--sandbox applies to codex only")
    try:
        journal, call, contract = launch_context(args.harness, os.environ)
    except RECORD_ERRORS:
        print(
            "Delegate launch denied: durable parent context or storage unavailable.",
            file=sys.stderr,
        )
        # Nothing can be written here; an enclosing delegate records it.
        print(
            denial_line(args.harness, os.environ.get(DENIAL_NONCE_ENV)),
            file=sys.stderr,
            flush=True,
        )
        return EXIT_CONTEXT_UNAVAILABLE
    if sandbox is not None:
        refused = refuse_without_sandbox(journal, call, contract, sandbox, os.environ)
        if refused is not None:
            return refused
        command = codex_command(sandbox, nested_grant(journal, contract))
    else:
        permissions, refused = refuse_without_parent_permissions(
            journal, call, os.environ
        )
        if refused is not None or permissions is None:
            return refused or EXIT_CONTEXT_UNAVAILABLE
        refused = refuse_without_claude_auth(journal, call, os.environ)
        if refused is not None:
            return refused
        command = claude_command(permissions)
    refused = refuse_without_capture_hooks(journal, call, args.harness, os.environ)
    if refused is not None:
        return refused
    if args.model:
        command.extend(["--model", args.model])
    # `--` ends option parsing, so a prompt that starts with `-` (for example
    # "--sandbox danger-full-access" or "-c sandbox_mode=...") stays a prompt.
    command.extend(["--", args.prompt])
    return run(command, journal, call, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
