#!/usr/bin/env python3
"""Install and validate dcg, the destructive command guard, in a workspace image.

Agents in a workspace run shell commands with no approval prompt. dcg is a
pre-execution hook that denies destructive ones (``git reset --hard``,
``rm -rf ./src``, ``docker volume rm``, ``DROP TABLE``). This script bakes it in
where the agent user cannot switch it off. It runs at image build time only,
bind-mounted for one RUN, and is never shipped.

``install`` (root, build time)
    Download the release pinned in ``dcg-version.toml`` for this dpkg arch,
    verify its SHA256 and THEN its minisign signature, extract the single
    ``dcg`` binary and assert its version. Only after every check passes does
    it write anything: the binary root-owned 0755 at ``/usr/local/bin/dcg``,
    the policy 0644 at ``/etc/dcg/config.toml``, and the hook in each
    harness's ROOT-OWNED settings layer:

    - Claude Code  ``/etc/claude-code/managed-settings.json``  PreToolUse/Bash
    - Codex        ``/etc/codex/requirements.toml``  managed PreToolUse/^Bash$
      (managed hooks are trusted; user hooks are skipped until trusted)

    ``~/.claude/settings.json`` is agent-writable and written by the
    entrypoint, so it is never the guard and is not touched.

``validate`` (any user, last build step)
    Exit 1 unless the files are root-owned and not group/world writable, the
    binary is the pinned version, both harnesses are registered, the policy
    the agent sees is fail-closed, and every known-bad payload is denied (and
    ``ls`` allowed) through both harnesses' hook protocols.

dcg is a guardrail, not a sandbox: an agent can still write a script and run
it, and Codex's ``unified_exec`` path can skip PreToolUse.

Standard library only. Test overrides: AGENT_GUARD_ROOT (path prefix for every
image path), AGENT_GUARD_OWNER_UID (expected owner, default 0),
AGENT_GUARD_RELEASE_URL, AGENT_GUARD_ARCH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

# Python 3.11 syntax (no `type` statement): the images run 3.12, a bare
# bookworm host has 3.11.
JsonValue: TypeAlias = (
    "str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]"
)
JsonObject: TypeAlias = "dict[str, JsonValue]"

HERE = Path(__file__).resolve().parent
VERSION_FILE = HERE / "dcg-version.toml"
POLICY_FILE = HERE / "dcg-config.toml"

BIN = "/usr/local/bin/dcg"
POLICY = "/etc/dcg/config.toml"
HOOK_COMMAND = f"/usr/bin/env DCG_NO_UPDATE_CHECK=1 {BIN}"
MARKER = "# Managed by agentic-workspace workspace/agent-guard."

CLAUDE_SETTINGS = "/etc/claude-code/managed-settings.json"
CODEX_REQUIREMENTS = "/etc/codex/requirements.toml"

REQUIRED_PACKS = (
    "core.git",
    "core.filesystem",
    "database.postgresql",
    "database.redis",
    "containers.docker",
    "containers.compose",
)
BAD_COMMANDS = (
    "rm -rf ./src",
    "git reset --hard",
    "docker volume rm x",
    "psql -c 'DROP TABLE x'",
)
HARNESSES = ("claude", "codex")

CODEX_REQUIREMENTS_BODY = f"""{MARKER} Do not edit.
# dcg as a managed Codex PreToolUse hook: always enabled, needs no per-user trust.
[hooks]

[[hooks.PreToolUse]]
matcher = "^Bash$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "{HOOK_COMMAND}"
"""


class GuardError(Exception):
    """A step that must stop the install or fail validation."""


@dataclass(frozen=True)
class Pin:
    version: str
    release_url: str
    minisign_public_key: str
    asset_name: str
    asset_sha256: str


def image_path(path: str) -> Path:
    """An image path, under AGENT_GUARD_ROOT in tests."""
    return Path(os.environ.get("AGENT_GUARD_ROOT", "/")) / path.lstrip("/")


def dpkg_arch() -> str:
    override = os.environ.get("AGENT_GUARD_ARCH")
    if override:
        return override
    return subprocess.run(
        ["dpkg", "--print-architecture"], capture_output=True, text=True, check=True
    ).stdout.strip()


def load_pin() -> Pin:
    data = tomllib.loads(VERSION_FILE.read_text())
    arch = dpkg_arch()
    assets = data["assets"]
    if arch not in assets:
        raise GuardError(
            f"no pinned dcg asset for architecture {arch!r} in {VERSION_FILE}"
        )
    return Pin(
        version=str(data["version"]),
        release_url=os.environ.get("AGENT_GUARD_RELEASE_URL", str(data["release_url"])),
        minisign_public_key=str(data["minisign_public_key"]),
        asset_name=str(assets[arch]["name"]),
        asset_sha256=str(assets[arch]["sha256"]),
    )


# --- install -----------------------------------------------------------------


def fetch(url: str, dest: Path) -> None:
    # curl, not urllib: the uv-managed interpreter does not read Debian's CA
    # store, and curl also serves file:// for the tests.
    subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "-o", str(dest), url],
        check=True,
        capture_output=True,
        text=True,
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_binary(pin: Pin, work: Path) -> Path:
    """Download the pinned archive, verify SHA256 then minisign, return the dcg binary."""
    archive = work / pin.asset_name
    signature = work / f"{pin.asset_name}.minisig"
    base = f"{pin.release_url}/v{pin.version}"
    fetch(f"{base}/{pin.asset_name}", archive)
    fetch(f"{base}/{pin.asset_name}.minisig", signature)
    actual = sha256_of(archive)
    if actual != pin.asset_sha256:
        raise GuardError(
            f"{pin.asset_name}: sha256 {actual} is not the pinned {pin.asset_sha256}"
        )
    minisign = shutil.which("minisign")
    if minisign is None:
        raise GuardError("minisign is required to verify the dcg release signature")
    verify = subprocess.run(
        [
            minisign,
            "-V",
            "-q",
            "-m",
            str(archive),
            "-x",
            str(signature),
            "-P",
            pin.minisign_public_key,
        ],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        raise GuardError(
            f"{pin.asset_name}: minisign signature did not verify: {verify.stderr.strip()}"
        )
    with tarfile.open(archive) as tar:
        members = [
            m
            for m in tar.getmembers()
            if m.isfile() and m.name.removeprefix("./") == "dcg"
        ]
        if len(members) != 1:
            raise GuardError(
                f"{pin.asset_name}: expected one dcg binary at the root, found {len(members)}"
            )
        source = tar.extractfile(members[0])
        if source is None:
            raise GuardError(f"{pin.asset_name}: dcg member is unreadable")
        binary = work / "dcg"
        with source, binary.open("wb") as out:
            shutil.copyfileobj(source, out)
    binary.chmod(0o755)
    return binary


def own_by_root(path: Path) -> None:
    if os.geteuid() == 0:
        os.chown(path, 0, 0)


def write_root_file(path: Path, data: bytes, mode: int) -> None:
    """Atomically replace PATH with DATA, root-owned, new parents 0755."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_bytes(data)
    tmp.chmod(mode)
    own_by_root(tmp)
    tmp.replace(path)


def is_dcg_command(command: JsonValue) -> bool:
    return isinstance(command, str) and bool(
        re.search(r"(^|[\s/])dcg$", command.strip())
    )


def merged_hook(
    settings: JsonObject, event: str, matcher: str, hook: JsonObject
) -> JsonObject:
    """SETTINGS with HOOK registered once under EVENT/MATCHER; other hooks kept."""
    hooks = settings.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    groups = hooks.get(event)
    kept: list[JsonValue] = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        handlers = group.get("hooks")
        handlers = handlers if isinstance(handlers, list) else []
        others = [
            h
            for h in handlers
            if not (isinstance(h, dict) and is_dcg_command(h.get("command")))
        ]
        if others or not handlers:
            kept.append({**group, "hooks": others})
    kept.append({"matcher": matcher, "hooks": [hook]})
    hooks[event] = kept
    return {**settings, "hooks": hooks}


def register_claude(path: Path) -> None:
    current: JsonObject = {}
    if path.exists():
        loaded = json.loads(path.read_text() or "{}")
        if not isinstance(loaded, dict):
            raise GuardError(f"{path} is not a JSON object")
        current = loaded
    updated = merged_hook(
        current, "PreToolUse", "Bash", {"type": "command", "command": HOOK_COMMAND}
    )
    write_root_file(path, (json.dumps(updated, indent=2) + "\n").encode(), 0o644)


def register_codex(path: Path) -> None:
    if path.exists() and not path.read_text().startswith(MARKER):
        raise GuardError(
            f"{path} exists and is not managed by agent_guard.py; merge the dcg [hooks] block"
        )
    write_root_file(path, CODEX_REQUIREMENTS_BODY.encode(), 0o644)


def install(expect_version: str | None) -> None:
    if os.geteuid() != 0 and "AGENT_GUARD_ROOT" not in os.environ:
        raise GuardError("install writes root-owned files: run it as root")
    pin = load_pin()
    if expect_version is not None and expect_version != pin.version:
        raise GuardError(
            f"ARG DCG_VERSION is {expect_version}, dcg-version.toml pins {pin.version}"
        )
    with tempfile.TemporaryDirectory(prefix="dcg-") as tmp:
        binary = verified_binary(pin, Path(tmp))
        version = run_version(binary)
        if version != pin.version:
            raise GuardError(
                f"downloaded dcg reports {version!r}, pinned {pin.version}"
            )
        write_root_file(image_path(BIN), binary.read_bytes(), 0o755)
    write_root_file(image_path(POLICY), POLICY_FILE.read_bytes(), 0o644)
    register_claude(image_path(CLAUDE_SETTINGS))
    register_codex(image_path(CODEX_REQUIREMENTS))
    print(
        f"dcg {pin.version} installed at {BIN}; policy {POLICY}; hooks: claude, codex"
    )


# --- validate ----------------------------------------------------------------


def clean_env(home: Path | None) -> dict[str, str]:
    """No inherited DCG_* overrides: validate what the files say."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "NO_COLOR": "1",
        "DCG_NO_UPDATE_CHECK": "1",
    }
    if home is not None:
        env["HOME"] = str(home)
    return env


def run_version(binary: Path) -> str:
    out = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        env=clean_env(None),
        timeout=30,
    ).stdout
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", out)
    return match.group(1) if match else ""


def expected_owner() -> int:
    return int(os.environ.get("AGENT_GUARD_OWNER_UID", "0"))


def check_locked(path: Path) -> list[str]:
    """PATH and every ancestor: owned by root, not group/world writable."""
    if not path.exists():
        return [f"{path} is missing"]
    problems: list[str] = []
    owner = expected_owner()
    root = image_path("/").resolve()
    for p in [path, *path.parents]:
        if "AGENT_GUARD_ROOT" in os.environ and not p.resolve().is_relative_to(root):
            break
        st = p.stat()
        if st.st_uid != owner:
            problems.append(f"{p} is owned by uid {st.st_uid}, not {owner}")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            problems.append(f"{p} is group/world writable")
    return problems


def registered_claude(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    groups = (
        data.get("hooks", {}).get("PreToolUse", []) if isinstance(data, dict) else []
    )
    return any(
        isinstance(g, dict)
        and g.get("matcher") == "Bash"
        and any(
            isinstance(h, dict) and h.get("command") == HOOK_COMMAND
            for h in g.get("hooks") or []
        )
        for g in groups
    )


def registered_codex(path: Path) -> bool:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return False
    groups = data.get("hooks", {}).get("PreToolUse", [])
    return any(
        g.get("matcher") == "^Bash$"
        and any(h.get("command") == HOOK_COMMAND for h in g.get("hooks", []))
        for g in groups
    )


def payload(harness: str, command: str, cwd: Path) -> str:
    body: JsonObject = {
        "session_id": "agent-guard-validate",
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    if harness == "codex":
        body["turn_id"] = "agent-guard-validate"
        body["tool_use_id"] = "agent-guard-validate"
    return json.dumps(body)


def hook_denies(stdin: str, home: Path, cwd: Path) -> bool:
    """Run the registered hook command as the harness would; True when it denies."""
    argv = HOOK_COMMAND.split()
    argv[-1] = str(image_path(argv[-1]))
    result = subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        env=clean_env(home),
        cwd=cwd,
        timeout=30,
    )
    if result.returncode == 2:
        return True
    try:
        decision = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        return False
    if not isinstance(decision, dict):
        return False
    specific = decision.get("hookSpecificOutput")
    return isinstance(specific, dict) and specific.get("permissionDecision") == "deny"


def effective_policy(home: Path) -> list[str]:
    out = subprocess.run(
        [str(image_path(BIN)), "config"],
        capture_output=True,
        text=True,
        env=clean_env(home),
        cwd=home if home.is_dir() else "/",
        timeout=30,
    ).stdout
    problems: list[str] = []
    for label, want in (
        ("Fail closed", "true"),
        ("Unverified decision", "deny"),
        ("Check updates", "false"),
    ):
        match = re.search(rf"^\s*{label}:\s*(\S+)", out, re.MULTILINE)
        if not match or match.group(1) != want:
            got = match.group(1) if match else "missing"
            problems.append(f"{home}: effective '{label}' is {got}, want {want}")
    section = out.split("Enabled packs:", 1)[-1].split("Disabled packs:", 1)[0]
    enabled = set(re.findall(r"^\s+- (\S+)$", section, re.MULTILINE))
    missing = [p for p in REQUIRED_PACKS if p not in enabled]
    if missing:
        problems.append(f"{home}: required packs not enabled: {', '.join(missing)}")
    return problems


def validate(home: Path) -> list[str]:
    pin = load_pin()
    problems: list[str] = []
    for path in (BIN, POLICY, CLAUDE_SETTINGS, CODEX_REQUIREMENTS):
        problems += check_locked(image_path(path))
    if image_path(BIN).exists() and (v := run_version(image_path(BIN))) != pin.version:
        problems.append(f"{BIN} reports {v or 'no version'}, pinned {pin.version}")
    if (
        image_path(POLICY).exists()
        and image_path(POLICY).read_bytes() != POLICY_FILE.read_bytes()
    ):
        problems.append(f"{POLICY} differs from {POLICY_FILE.name}")
    if not registered_claude(image_path(CLAUDE_SETTINGS)):
        problems.append(f"claude: dcg not registered in {CLAUDE_SETTINGS}")
    if not registered_codex(image_path(CODEX_REQUIREMENTS)):
        problems.append(f"codex: dcg not registered in {CODEX_REQUIREMENTS}")
    if problems:
        return problems  # the probes below would only restate a missing install
    problems += effective_policy(home)
    with tempfile.TemporaryDirectory(prefix="dcg-validate-") as tmp:
        cwd = Path(tmp)
        for harness in HARNESSES:
            for command in BAD_COMMANDS:
                if not hook_denies(payload(harness, command, cwd), home, cwd):
                    problems.append(
                        f"{harness}: known-bad command was not denied: {command}"
                    )
            if hook_denies(payload(harness, "ls", cwd), home, cwd):
                problems.append(
                    f"{harness}: harmless 'ls' was denied (policy blocks everything)"
                )
        if not hook_denies("not json", home, cwd):
            problems.append("unparseable hook input was allowed (not fail-closed)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    p_install = sub.add_parser(
        "install", help="install the pinned dcg and register it (root)"
    )
    p_install.add_argument(
        "--expect-version",
        help="fail unless dcg-version.toml pins this version (ARG DCG_VERSION)",
    )
    p_validate = sub.add_parser(
        "validate", help="exit 1 unless every guard check passes"
    )
    p_validate.add_argument(
        "--home", type=Path, required=True, help="the agent user's HOME"
    )
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            install(args.expect_version)
            return 0
        problems = validate(args.home)
    except (
        GuardError,
        OSError,
        subprocess.SubprocessError,
        KeyError,
        tarfile.TarError,
    ) as exc:
        detail = (
            exc.stderr.strip()
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else ""
        )
        print(f"agent guard: {exc} {detail}".rstrip(), file=sys.stderr)
        return 1
    for problem in problems:
        print(f"agent guard: FAIL {problem}", file=sys.stderr)
    if not problems:
        print(
            "agent guard: dcg pinned, root-owned, registered for claude and codex, known-bad denied"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
