"""Claude Code PreToolUse hook: refuse kill commands aimed at the harness.

WHY (syntropic137/syntropic137#2355). An agent in a workspace ran
``ps aux | grep "[p]ytest -m unit" | awk '{print $2}' | xargs kill``. The
Claude CLI's own argv carried the task prompt, which mentioned
``pytest -m unit``, so the pipeline matched the harness itself and SIGTERMed
it; the phase died with exit 143. Claude Code's built-in shell wrapper only
guards ``pkill``. This hook covers the other spellings.

WHAT IT PROTECTS. Every ancestor of the hook process, walked through
``/proc/<pid>/stat`` up to and including pid 1: the Claude CLI that spawned
the hook, and the launcher shells and init above it. A process group that
contains one of them is protected too.

WHAT IT CHECKS. Each simple command anywhere in the Bash tool's command
string (after ``;``, ``&&``, ``||``, ``|``, ``&``, newlines, inside ``$(...)``,
backticks, ``bash -c`` / ``eval`` strings, ``for`` lists) whose command,
after stripping env assignments and wrappers (``sudo``, ``command``,
``builtin``, ``exec``, ``nohup``, ``timeout``, ``nice``, ``env``, ``setsid``,
``stdbuf``), is one of:

- ``kill``: any signal spelling (``-9``, ``-KILL``, ``-SIGTERM``, ``-s SIG``,
  ``-n N``, ``--``); ``-l`` / ``-L`` list signals and are allowed.
- ``xargs [opts] kill ...``: the pids are the output of the pipeline that
  feeds xargs.
- ``pkill``: resolved by running ``pgrep`` with the same selection args.
- ``killall``: names compared with the protected processes' ``comm`` and
  ``argv[0]`` basename.

Targets are RESOLVED AT HOOK TIME, not matched lexically. A ``$(...)``,
backtick, xargs feed or ``NAME=$(...)`` assignment is evaluated by actually
running it, but only when every simple command in it is on a read-only
allowlist (ps, pgrep, pidof, grep, awk, sed, cut, head, tail, sort, uniq, tr,
cat, echo, printf), with no output redirection other than to /dev/null, under
a 5 s timeout. Integer tokens of its output are the pids.

Refusal: exit 2, stderr names the pid and its cmdline. Otherwise exit 0.

KNOWN LIMITS (all fail toward ALLOW; this is defence in depth, not a sandbox):

- A target whose value cannot be known at hook time is allowed: an unknown
  ``$VAR``, a producer that is not on the allowlist (``docker ps``, ``jq``,
  scripts), awk/sed programs that could execute or write, ``read`` loops,
  ``xargs -a FILE``, and anything assembled from several expansions in one
  word.
- A kill hidden in a script file, a function, an alias, a here-document, a
  language runtime (``python -c 'os.kill(...)'``) or a background job started
  earlier is not seen. Only the literal Bash tool command is parsed.
- Resolution happens before the command runs. A process that starts or
  changes its cmdline in between is judged on what it looked like at hook
  time.
- ``killall`` name matching ignores ``-u``, so it may over-refuse for a
  protected name; ``killall -u USER`` with NO name (every process of that
  user) is not resolved and is allowed. ``killall -g`` is judged by name only.
- A producer word that itself contains an expansion
  (``echo $(pgrep x) | xargs kill``) is not evaluated.
- Two deliberately conservative refusals: ``kill 0`` (signals the caller's
  process group, which may be the harness's) and ``kill $PPID`` (the parent of
  the agent's shell is the harness).
- Any internal error fails OPEN with a one-line stderr note: the guard must
  never make Bash unusable.

The decision logic (``decide``) is pure: the protected set, the producer
runner and pgrep are injected. ``main`` binds the real ``/proc`` versions.
Stdlib only; must run on Python 3.11.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Where the image installs this file, and the exact command registered as
#: the hook. ``env -i`` because the agent controls its own environment
#: (PYTHONPATH, PYTHONSTARTUP, BASH_ENV ...) and the hook inherits it; ``-I
#: -S`` so no cwd, user-site or site-packages module can shadow the stdlib.
#: /opt/venv is root-owned; node:22-slim ships no /usr/bin/python3.
INSTALL_PATH = "/usr/local/lib/agentic-guards/kill_guard.py"
HOOK_COMMAND = (
    "/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin "
    f"/opt/venv/bin/python3 -I -S {INSTALL_PATH}"
)
MANAGED_SETTINGS = "/etc/claude-code/managed-settings.json"
HOOK_TIMEOUT_S = 30

SAFE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"}
PRODUCER_TIMEOUT_S = 5.0
TOTAL_BUDGET_S = 15.0

KILL_WORD = re.compile(r"(?<![\w.-])(?:kill|pkill|killall)(?![\w.-])")

# --------------------------------------------------------------------------
# Process table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    comm: str
    argv: tuple[str, ...]

    @property
    def cmdline(self) -> str:
        return " ".join(self.argv) if self.argv else f"[{self.comm}]"


ProcLookup = Callable[[int], "ProcInfo | None"]
ProducerRunner = Callable[[str], "str | None"]
PgrepRunner = Callable[[Sequence[str]], "list[int] | None"]


def protected_set(start_pid: int, lookup: ProcLookup) -> dict[int, ProcInfo]:
    """``start_pid`` and every ancestor of it, up to and including pid 1."""
    out: dict[int, ProcInfo] = {}
    pid = start_pid
    while pid > 0 and pid not in out:
        info = lookup(pid)
        if info is None:
            break
        out[pid] = info
        if pid == 1:
            break
        pid = info.ppid
    return out


def read_proc(pid: int) -> ProcInfo | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    lp, rp = stat.find("("), stat.rfind(")")
    fields = stat[rp + 2 :].split()
    argv = tuple(a.decode("utf-8", "replace") for a in raw.split(b"\0") if a)
    return ProcInfo(pid, int(fields[1]), int(fields[2]), stat[lp + 1 : rp], argv)


# --------------------------------------------------------------------------
# Shell lexer / parser (enough of bash to find commands and their words)
# --------------------------------------------------------------------------


class ParseError(Exception):
    pass


@dataclass
class Word:
    raw: str
    start: int
    end: int
    #: ("lit", text) | ("var", name) | ("sub", body) | ("dyn", body)
    parts: list[tuple[str, str]]

    @property
    def value(self) -> str | None:
        if all(k == "lit" for k, _ in self.parts):
            return "".join(t for _, t in self.parts)
        return None

    def single(self) -> tuple[str, str] | None:
        """The one expansion this word consists of, quotes aside."""
        rest = [(k, t) for k, t in self.parts if not (k == "lit" and t == "")]
        return rest[0] if len(rest) == 1 and rest[0][0] != "lit" else None

    @property
    def nested(self) -> list[str]:
        return [t for k, t in self.parts if k in ("sub", "dyn") and t]


@dataclass
class Op:
    op: str
    start: int


@dataclass
class Redir:
    op: str
    start: int
    target: str | None = None  # set for >&N style; else the next Word


Token = Word | Op | Redir


def _find_close(src: str, i: int) -> int:
    """Index of the ``)`` closing a ``(`` that ends just before ``i``."""
    depth, n = 1, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = src.find("'", i + 1)
            if j < 0:
                raise ParseError("unterminated '")
            i = j + 1
            continue
        if c == '"':
            i = _skip_dq(src, i + 1)
            continue
        if c == "`":
            i = _find_backtick(src, i + 1) + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ParseError("unterminated (")


def _skip_dq(src: str, i: int) -> int:
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and src.startswith("$(", i):
            i = _find_close(src, i + 2) + 1
            continue
        if c == "`":
            i = _find_backtick(src, i + 1) + 1
            continue
        i += 1
    raise ParseError('unterminated "')


def _find_backtick(src: str, i: int) -> int:
    n = len(src)
    while i < n:
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == "`":
            return i
        i += 1
    raise ParseError("unterminated `")


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _dollar(src: str, i: int, parts: list[tuple[str, str]]) -> int:
    """Lex an expansion starting at ``src[i] == '$'``; return the next index."""
    n = len(src)
    if src.startswith("$((", i):
        j = _find_close(src, i + 2)
        parts.append(("dyn", ""))
        return j + 1
    if src.startswith("$(", i):
        j = _find_close(src, i + 2)
        parts.append(("sub", src[i + 2 : j]))
        return j + 1
    if src.startswith("${", i):
        j = src.find("}", i + 2)
        if j < 0:
            raise ParseError("unterminated ${")
        body = src[i + 2 : j]
        m = _NAME.fullmatch(body) or re.fullmatch(r"[0-9$!?#@*-]", body)
        parts.append(("var", body) if m else ("dyn", ""))
        return j + 1
    if src.startswith("$'", i):
        j = i + 2
        while j < n and src[j] != "'":
            j += 2 if src[j] == "\\" else 1
        parts.append(("dyn", ""))
        return j + 1
    if i + 1 < n:
        m = _NAME.match(src, i + 1)
        if m:
            parts.append(("var", m.group()))
            return m.end()
        if src[i + 1] in "0123456789$!?#@*-":
            parts.append(("var", src[i + 1]))
            return i + 2
    parts.append(("lit", "$"))
    return i + 1


_OPS = ("&&", "||", ";;", "|&", "|", "&", ";", "(", ")")
_REDIRS = ("<<<", "<<-", "<<", "<>", ">>", ">|", ">&", "<&", "&>>", "&>", ">", "<")


def lex(src: str) -> list[Token]:
    tokens: list[Token] = []
    parts: list[tuple[str, str]] = []
    start = -1
    heredocs: list[tuple[str, bool]] = []
    want_delim: bool | None = None  # strip-tabs flag while awaiting a delimiter
    i, n = 0, len(src)

    def flush(end: int) -> None:
        nonlocal parts, start, want_delim
        if start >= 0:
            w = Word(src[start:end], start, end, parts)
            tokens.append(w)
            if want_delim is not None:
                heredocs.append((w.value or w.raw.strip("'\""), want_delim))
                want_delim = None
        parts, start = [], -1

    def begin() -> None:
        nonlocal start
        if start < 0:
            start = i

    while i < n:
        c = src[i]
        if c in " \t":
            flush(i)
            i += 1
        elif c == "\n":
            flush(i)
            tokens.append(Op(";", i))
            i += 1
            for delim, strip in heredocs:
                while i < n:
                    j = src.find("\n", i)
                    line = src[i : j if j >= 0 else n]
                    i = j + 1 if j >= 0 else n
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            heredocs = []
        elif c == "#" and start < 0:
            j = src.find("\n", i)
            i = j if j >= 0 else n
        elif c == "\\":
            if src.startswith("\\\n", i):
                i += 2
                continue
            begin()
            parts.append(("lit", src[i + 1 : i + 2]))
            i += 2
        elif c == "'":
            begin()
            j = src.find("'", i + 1)
            if j < 0:
                raise ParseError("unterminated '")
            parts.append(("lit", src[i + 1 : j]))
            i = j + 1
        elif c == '"':
            begin()
            i += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise ParseError('unterminated "')
                d = src[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and i + 1 < n and src[i + 1] in '$`"\\\n':
                    buf.append(src[i + 1])
                    i += 2
                elif d == "$" or d == "`":
                    parts.append(("lit", "".join(buf)))
                    buf = []
                    if d == "$":
                        i = _dollar(src, i, parts)
                    else:
                        j = _find_backtick(src, i + 1)
                        parts.append(("sub", src[i + 1 : j].replace("\\`", "`")))
                        i = j + 1
                else:
                    buf.append(d)
                    i += 1
            parts.append(("lit", "".join(buf)))
        elif c == "`":
            begin()
            j = _find_backtick(src, i + 1)
            parts.append(("sub", src[i + 1 : j].replace("\\`", "`")))
            i = j + 1
        elif c == "$":
            begin()
            i = _dollar(src, i, parts)
        elif c in "<>" and src.startswith("(", i + 1):
            begin()  # process substitution: dynamic, but scan its body
            j = _find_close(src, i + 2)
            parts.append(("dyn", src[i + 2 : j]))
            i = j + 1
        elif c in "<>" or src.startswith("&>", i):
            if (
                start >= 0
                and re.fullmatch(r"\d+", src[start:i])
                and all(k == "lit" for k, _ in parts)
                and "".join(t for _, t in parts) == src[start:i]
            ):
                parts, start = [], -1  # fd number belongs to the redirection
            else:
                flush(i)
            op = next(r for r in _REDIRS if src.startswith(r, i))
            red = Redir(op, i)
            i += len(op)
            if op in (">&", "<&"):
                m = re.match(r"[0-9]+-?|-", src[i:])
                if m:
                    red.target = m.group()
                    i += len(m.group())
            if op in ("<<", "<<-"):
                want_delim = op == "<<-"
            tokens.append(red)
        elif c in "|&;()":
            flush(i)
            op = next(o for o in _OPS if src.startswith(o, i))
            tokens.append(Op(op, i))
            i += len(op)
        else:
            begin()
            parts.append(("lit", c))
            i += 1
    flush(n)
    return tokens


@dataclass
class SimpleCommand:
    words: list[Word] = field(default_factory=list)
    redirs: list[tuple[str, str | None]] = field(default_factory=list)
    start: int = -1
    end: int = -1


Pipeline = list[SimpleCommand]


def parse(src: str) -> list[Pipeline]:
    """Split into pipelines of simple commands. Grouping is flattened."""
    pipelines: list[Pipeline] = []
    pipe: Pipeline = []
    cmd = SimpleCommand()
    pending: str | None = None

    def end_cmd() -> None:
        nonlocal cmd
        if cmd.words or cmd.redirs:
            pipe.append(cmd)
        cmd = SimpleCommand()

    def end_pipe() -> None:
        nonlocal pipe
        end_cmd()
        if pipe:
            pipelines.append(pipe)
        pipe = []

    for tok in lex(src):
        if isinstance(tok, Word):
            if pending is not None:
                cmd.redirs.append((pending, tok.value))
                pending = None
            else:
                cmd.words.append(tok)
            if cmd.start < 0:
                cmd.start = tok.start
            cmd.end = tok.end
        elif isinstance(tok, Redir):
            if cmd.start < 0:
                cmd.start = tok.start
            cmd.end = tok.start + len(tok.op) + len(tok.target or "")
            if tok.target is not None:
                cmd.redirs.append((tok.op, tok.target))
            else:
                pending = tok.op
        elif tok.op in ("|", "|&"):
            end_cmd()
        else:
            end_pipe()
    end_pipe()
    return pipelines


# --------------------------------------------------------------------------
# Command normalisation
# --------------------------------------------------------------------------

_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\+?=")
_KEYWORDS = {
    "!",
    "{",
    "}",
    "then",
    "do",
    "else",
    "elif",
    "if",
    "while",
    "until",
    "time",
    "done",
    "fi",
}

#: wrapper -> options that take a separate argument
_WRAPPERS: dict[str, set[str]] = {
    "sudo": {
        "-u",
        "-g",
        "-h",
        "-p",
        "-C",
        "-U",
        "-r",
        "-t",
        "-T",
        "-D",
        "--user",
        "--group",
    },
    "doas": {"-u", "-C"},
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "nohup": set(),
    "setsid": set(),
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-t"},
    "stdbuf": {"-i", "-o", "-e"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
}


def basename(word: Word) -> str | None:
    v = word.value
    return v.rsplit("/", 1)[-1] if v else None


def strip_prefixes(words: list[Word]) -> list[Word]:
    """Drop leading assignments, keywords and wrapper commands."""
    i = 0
    while i < len(words):
        v = words[i].value
        if v is not None and (v in _KEYWORDS or _ASSIGN.match(v)):
            i += 1
            continue
        name = basename(words[i])
        if name not in _WRAPPERS:
            break
        if name == "command" and any(
            w.value in ("-v", "-V") for w in words[i + 1 : i + 2]
        ):
            return []  # a lookup, not an execution
        takes_arg = _WRAPPERS[name]
        i += 1
        while i < len(words):
            a = words[i].value
            if a is None or not a.startswith("-") or a == "-":
                break
            i += 1
            if a == "--":
                break
            if a in takes_arg:
                i += 1
        if name == "nice" or name == "env":
            while i < len(words) and (words[i].value or "").startswith("-"):
                i += 1
        if name == "timeout" and i < len(words):
            i += 1  # the duration
    return words[i:]


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------

#: Commands a producer may contain. Each is read-only given the per-command
#: argument checks in ``_producer_cmd_ok``.
PRODUCER_ALLOW = {
    "ps", "pgrep", "pidof", "grep", "egrep", "fgrep", "awk", "sed", "cut",
    "head", "tail", "sort", "uniq", "tr", "cat", "echo", "printf",
}  # fmt: skip

_SAFE_VARS = {"$", "!"}  # the agent's shell / its last background child


@dataclass(frozen=True)
class Refusal:
    command: str
    target: str
    reason: str

    def message(self) -> str:
        return (
            f"kill-guard: refused `{_trunc(self.command, 160)}`: target {self.target} "
            f"{self.reason}. Killing it would end this agent's own session "
            "(syntropic137#2355). Kill only processes you started: keep the pid "
            "from `$!` (`cmd & pid=$!; ...; kill $pid`), use `kill %1` for a "
            "background job, or `pkill -P $$` for children of this shell."
        )


def _trunc(s: str, n: int = 120) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


class _Refused(Exception):
    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.reason)
        self.refusal = refusal


@dataclass
class _Ctx:
    src: str
    protected: dict[int, ProcInfo]
    run_producer: ProducerRunner
    run_pgrep: PgrepRunner
    deadline: float
    env: dict[str, list[str] | None] = field(default_factory=dict)

    def refuse(self, cmd: str, target: str, reason: str) -> None:
        raise _Refused(Refusal(cmd, target, reason))

    def describe(self, pid: int) -> str:
        p = self.protected[pid]
        return f"is pid {pid} ({_trunc(p.cmdline)})"

    # --- targets -------------------------------------------------------

    def check_target(self, cmd: str, t: str) -> None:
        if t in ("$$", "$!") or t.startswith("%"):
            return
        if t == "$PPID":
            self.refuse(cmd, t, "is the parent of the agent's shell, i.e. the harness")
        m = re.fullmatch(r"(-?)(\d+)", t)
        if not m:
            return
        n = int(m.group(2))
        if n == 0:
            self.refuse(
                cmd,
                t,
                "is the caller's own process group, which may include the harness",
            )
        if m.group(1):
            if n == 1:
                self.refuse(cmd, t, "is every process this user can signal")
            for pid, p in self.protected.items():
                if p.pgid == n:
                    self.refuse(
                        cmd,
                        t,
                        f"is process group {n}, which contains pid {pid} ({_trunc(p.cmdline)})",
                    )
        elif n in self.protected:
            self.refuse(cmd, t, self.describe(n))

    # --- word resolution ----------------------------------------------

    def resolve(self, w: Word) -> list[str] | None:
        v = w.value
        if v is not None:
            return [v]
        one = w.single()
        if one is None:
            return None
        kind, text = one
        if kind == "var":
            if text in _SAFE_VARS:
                return ["$" + text]
            if text == "PPID":
                return ["$PPID"]
            return self.env.get(text)
        if kind == "sub":
            return self.produce(text)
        return None

    def produce(self, body: str) -> list[str] | None:
        if time.monotonic() > self.deadline:
            return None
        try:
            pipes = parse(body)
        except ParseError:
            return None
        if len(pipes) != 1 or not all(_producer_cmd_ok(c) for c in pipes[0]):
            return None
        out = self.run_producer(body)
        if out is None:
            return None
        return [t for t in out.split() if re.fullmatch(r"-?\d+", t)]

    # --- commands -----------------------------------------------------

    def scan(self, src: str, depth: int = 0) -> None:
        if depth > 8:
            return
        for pipe in parse(src):
            for idx, cmd in enumerate(pipe):
                for w in cmd.words:
                    for body in w.nested:
                        self.scan(body, depth + 1)
                self.command(src, pipe, idx, depth)

    def command(self, src: str, pipe: Pipeline, idx: int, depth: int) -> None:
        cmd = pipe[idx]
        text = src[cmd.start : cmd.end]
        words = cmd.words
        # Pure assignments are tracked for later `kill $NAME`.
        vals = [w.value for w in words]
        if words and all(_ASSIGN.match(w.raw) for w in words):
            for w in words:
                self.assign(w)
            return
        if len(words) >= 3 and vals[0] == "for" and vals[2] == "in" and vals[1]:
            got: list[str] | None = []
            for w in words[3:]:
                r = self.resolve(w)
                got = None if r is None or got is None else got + r
            self.env[vals[1]] = got
            return
        words = strip_prefixes(words)
        if not words:
            return
        name = basename(words[0])
        args = words[1:]
        if name in ("export", "local", "declare", "readonly", "typeset"):
            for w in args:
                if _ASSIGN.match(w.raw):
                    self.assign(w)
        elif name in ("bash", "sh", "dash", "zsh", "ksh"):
            for k, w in enumerate(args[:-1]):
                v = w.value or ""
                if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", v) and args[k + 1].value:
                    self.scan(args[k + 1].value or "", depth + 1)
                    break
        elif name == "eval":
            body = " ".join(w.value or "" for w in args)
            self.scan(body, depth + 1)
        elif name == "kill":
            self.kill(text, args, [])
        elif name == "pkill":
            self.pkill(text, args)
        elif name == "killall":
            self.killall(text, args)
        elif name == "xargs":
            self.xargs(src, pipe, idx, text, args)

    def assign(self, w: Word) -> None:
        m = _ASSIGN.match(w.raw)
        if not m:
            return
        # Re-lex the value part so quotes and expansions are honoured.
        try:
            toks = [t for t in lex(w.raw[m.end() :]) if isinstance(t, Word)]
        except ParseError:
            toks = []
        if len(toks) == 1:
            self.env[m.group(1)] = self.resolve(toks[0])
        elif not toks:
            self.env[m.group(1)] = []
        else:
            self.env[m.group(1)] = None

    def kill(self, text: str, args: Sequence[Word], extra: Sequence[str]) -> None:
        sig_seen = ended = False
        sig: str | None = None
        targets: list[str] = []
        i = 0
        while i < len(args):
            v = args[i].value
            if (
                not ended
                and not targets
                and v is not None
                and v.startswith("-")
                and v != "-"
            ):
                if v == "--":
                    ended = True
                    i += 1
                    continue
                if v in ("-l", "-L", "--list", "--table") or v.startswith("--list="):
                    return  # listing signals, not sending one
                if v in ("-s", "-n", "--signal"):
                    sig_seen = True
                    sig = args[i + 1].value if i + 1 < len(args) else None
                    i += 2
                    continue
                if v in ("-q", "--queue"):
                    i += 2
                    continue
                if v.startswith("--"):
                    i += 1
                    continue
                if not sig_seen:
                    sig_seen = True
                    sig = v[1:]
                    i += 1
                    continue
            r = self.resolve(args[i])
            if r is not None:
                targets.extend(r)
            i += 1
        if sig in ("0", "SIG0"):
            return  # signal 0 only probes for existence
        for t in [*targets, *extra]:
            self.check_target(text, t)

    def pkill(self, text: str, args: Sequence[Word]) -> None:
        argv: list[str] = []
        first = True
        skip = 0
        for w in args:
            if skip:
                skip -= 1
                continue
            vals = self.resolve(w)
            if vals is None:
                return  # unresolvable selection: allow
            if any(v in ("$$", "$!") for v in vals):
                return  # children/self of the agent's shell, never an ancestor
            if "$PPID" in vals:
                return  # selects by the harness pid (e.g. -P), it does not name it
            for v in vals:
                if first and re.fullmatch(
                    r"-(\d+|SIG[A-Za-z0-9+-]+|[A-Z][A-Z0-9+-]+)", v
                ):
                    first = False
                    continue
                first = False
                if v in ("--signal", "-q", "--queue"):
                    skip = 1
                    continue
                if v.startswith("--signal=") or v in ("-e", "--echo", "-c", "--count"):
                    continue
                if re.fullmatch(r"-[a-zA-Z]{2,}", v):
                    v = "-" + v[1:].replace("c", "").replace("e", "")
                    if v == "-":
                        continue
                argv.append(v)
        if time.monotonic() > self.deadline:
            return
        pids = self.run_pgrep(argv)
        for pid in pids or []:
            if pid in self.protected:
                self.refuse(
                    text,
                    str(pid),
                    f"matches the pkill selection and {self.describe(pid)}",
                )

    def killall(self, text: str, args: Sequence[Word]) -> None:
        regex = icase = False
        names: list[str] = []
        skip = 0
        for w in args:
            if skip:
                skip -= 1
                continue
            v = w.value
            if v is None:
                continue
            if v in (
                "-s",
                "--signal",
                "-u",
                "--user",
                "-o",
                "--older-than",
                "-y",
                "--younger-than",
                "-n",
                "--ns",
            ):
                skip = 1
            elif v in ("-r", "--regexp"):
                regex = True
            elif v in ("-I", "--ignore-case"):
                icase = True
            elif v.startswith("-") and len(v) > 1:
                if re.fullmatch(r"-[a-zA-Z]+", v) and not re.fullmatch(
                    r"-[A-Z][A-Z0-9+-]+|-\d+", v
                ):
                    regex = regex or "r" in v
                    icase = icase or "I" in v
            else:
                names.append(v)
        for name in names:
            for pid, p in self.protected.items():
                cands = {p.comm}
                if p.argv:
                    cands.add(p.argv[0].rsplit("/", 1)[-1])
                if _killall_match(name, cands, regex, icase):
                    self.refuse(text, name, f"names {self.describe(pid)}")

    def xargs(
        self, src: str, pipe: Pipeline, idx: int, text: str, args: Sequence[Word]
    ) -> None:
        with_arg = {"-n", "-L", "-P", "-s", "-d", "-E", "-I", "-a", "--max-args", "--max-lines",
                    "--max-procs", "--max-chars", "--delimiter", "--eof", "--arg-file", "--replace"}  # fmt: skip
        i = 0
        replace: str | None = None
        from_file = False
        while i < len(args):
            v = args[i].value
            if v is None or not v.startswith("-") or v == "-":
                break
            if v == "--":
                i += 1
                break
            if v in ("-a", "--arg-file") or v.startswith("--arg-file="):
                from_file = True
            if v == "-I" and i + 1 < len(args):
                replace = args[i + 1].value
            elif v.startswith("-i") or v.startswith("--replace"):
                replace = v.split("=", 1)[1] if "=" in v else (v[2:] or "{}")
            i += 2 if v in with_arg else 1
        inner = strip_prefixes(list(args[i:]))
        if not inner or basename(inner[0]) != "kill":
            if inner and basename(inner[0]) in ("pkill", "killall"):
                self.command_words(text, inner)
            return
        fixed = [w for w in inner[1:] if not (replace and replace in (w.value or ""))]
        extra: list[str] = []
        if idx > 0 and not from_file:
            body = " | ".join(src[c.start : c.end] for c in pipe[:idx])
            extra = self.produce(body) or []
        self.kill(text, fixed, extra)

    def command_words(self, text: str, words: list[Word]) -> None:
        name = basename(words[0])
        if name == "pkill":
            self.pkill(text, words[1:])
        elif name == "killall":
            self.killall(text, words[1:])


def _killall_match(name: str, cands: Iterable[str], regex: bool, icase: bool) -> bool:
    for c in cands:
        if regex:
            try:
                if re.search(name, c, re.IGNORECASE if icase else 0):
                    return True
            except re.error:
                return False
            continue
        a, b = (name.casefold(), c.casefold()) if icase else (name, c)
        if a == b or (len(a) >= 15 and a[:15] == b[:15]):
            return True
    return False


_SED_SIMPLE = re.compile(r"(\d+|\$)?(,(\d+|\$))?[pdq]|/[^/]*/[pd]")


def _sed_script_ok(script: str) -> bool:
    for piece in script.split(";"):
        p = piece.strip()
        if not p or _SED_SIMPLE.fullmatch(p):
            continue
        if len(p) < 2 or p[0] != "s" or p[1].isalnum() or p[1] in "\\\n ":
            return False
        d = p[1]
        segs = re.split(r"(?<!\\)" + re.escape(d), p[2:])
        if len(segs) != 3 or not re.fullmatch(r"[gIip0-9]*", segs[2]):
            return False
    return True


_SORT_FLAGS = re.compile(r"-[bdfghiMnrsuVz]+")
_SORT_VALUED = re.compile(r"-[kt].*")


def _awk_args_ok(args: Sequence[str]) -> bool:
    """Only `-F SEP`, `-FSEP`, `-v NAME=VAL`, `-vNAME=VAL` and `--` as options."""
    expect = False
    for a in args:
        if expect:
            expect = False
        elif a in ("-F", "-v"):
            expect = True
        elif a == "--" or not a.startswith("-") or a.startswith(("-F", "-v")):
            continue
        else:
            return False
    return not expect


def _sort_args_ok(args: Sequence[str]) -> bool:
    """Short ordering flags and `-k`/`-t` only; no long option at all."""
    expect = False
    for a in args:
        if expect:
            expect = False
        elif a in ("-k", "-t"):
            expect = True
        elif not a.startswith("-") or _SORT_FLAGS.fullmatch(a) or _SORT_VALUED.fullmatch(a):
            continue
        else:
            return False
    return not expect


def _producer_cmd_ok(cmd: SimpleCommand) -> bool:
    for op, target in cmd.redirs:
        if (
            op in (">&", "<&")
            and target is not None
            and re.fullmatch(r"\d+-?|-", target)
        ):
            continue
        if op in (">", ">>", "&>", "&>>", ">|") and target == "/dev/null":
            continue
        if op in ("<", "<<<") and target is not None:
            continue
        return False
    if not cmd.words:
        return False
    vals = [w.value for w in cmd.words]
    if any(v is None for v in vals):
        return False
    name = vals[0] or ""
    if "/" in name or name not in PRODUCER_ALLOW:
        return False
    args = [v or "" for v in vals[1:]]
    if name == "awk":
        # Options are an allowlist, not a denylist: `-fFILE`, `-E FILE`,
        # `--include` and a GNU long-option prefix all load program text the
        # guard never sees. Only field separators and assignments pass.
        if not _awk_args_ok(args):
            return False
        return not any(re.search(r"system|getline|[|>]|fflush|close", a) for a in args)
    if name == "sed":
        scripts: list[str] = []
        expect = False
        positional = 0
        for a in args:
            if expect:
                scripts.append(a)
                expect = False
            elif a in ("-e", "--expression"):
                expect = True
            elif a in ("-n", "-E", "-r", "--quiet", "--silent", "-u", "-z"):
                continue
            elif a.startswith("-"):
                return False
            else:
                if not scripts and positional == 0:
                    scripts.append(a)
                positional += 1
        return all(_sed_script_ok(s) for s in scripts)
    if name == "sort":
        # An allowlist again: GNU accepts any unambiguous prefix of a long
        # option (`--compress-p=prog` runs prog), so naming the bad ones
        # cannot be complete.
        return _sort_args_ok(args)
    if name == "uniq":
        # A second positional is an output file. After `--` every argument is
        # positional, `-output` included.
        positional = 0
        ended = False
        for a in args:
            if ended or a == "-" or not a.startswith("-"):
                positional += 1
            elif a == "--":
                ended = True
        return positional <= 1
    return True


def decide(
    payload: object,
    protected: dict[int, ProcInfo],
    run_producer: ProducerRunner,
    run_pgrep: PgrepRunner,
    *,
    budget_s: float = TOTAL_BUDGET_S,
) -> Refusal | None:
    """Return a refusal if the Bash command would signal a protected process."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not KILL_WORD.search(command):
        return None
    ctx = _Ctx(command, protected, run_producer, run_pgrep, time.monotonic() + budget_s)
    try:
        ctx.scan(command)
    except _Refused as r:
        return r.refusal
    return None


# --------------------------------------------------------------------------
# Real runners
# --------------------------------------------------------------------------


def run_producer(body: str) -> str | None:
    try:
        proc = subprocess.Popen(  # noqa: S603 - body passed the read-only allowlist
            # `set -f +B`: no pathname and no brace expansion, so the words
            # the allowlist checked are the words that run. A glob or
            # `{-f,FILE}` could otherwise expand into an option it refused.
            ["/bin/bash", "-c", "set -f +B\n" + body],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=SAFE_ENV,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=PRODUCER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        # Bounded again: a child that left the group can still hold stdout
        # open, and an unbounded wait here would hang the hook. Give up on it
        # and answer "unknown", which allows the command.
        try:
            proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            if proc.stdout is not None:
                proc.stdout.close()
        return None
    return out.decode("utf-8", "replace")


def run_pgrep(args: Sequence[str]) -> list[int] | None:
    try:
        res = subprocess.run(  # noqa: S603
            ["pgrep", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=SAFE_ENV,
            timeout=PRODUCER_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode > 1:
        return None
    return [int(t) for t in res.stdout.split() if t.isdigit()]


# --------------------------------------------------------------------------
# Registration (image build time)
# --------------------------------------------------------------------------


def register(
    settings_path: str = MANAGED_SETTINGS, command: str = HOOK_COMMAND
) -> None:
    """Add the hook to Claude Code managed settings, keeping what is there."""
    path = Path(settings_path)
    data: object = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: not a JSON object")
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault("PreToolUse", []) if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        raise SystemExit(f"{path}: hooks.PreToolUse is not a list")
    present = any(
        isinstance(g, dict)
        and isinstance(g.get("hooks"), list)
        and any(isinstance(h, dict) and h.get("command") == command for h in g["hooks"])
        for g in groups
    )
    if not present:
        groups.append(
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}
                ],
            }
        )
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o644)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    stdin: str | None = None,
    lookup: ProcLookup = read_proc,
    self_ppid: int | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--register"]:
        register(*args[1:2])
        return 0
    try:
        raw = sys.stdin.read() if stdin is None else stdin
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str) or not KILL_WORD.search(command):
            return 0
        protected = protected_set(
            os.getppid() if self_ppid is None else self_ppid, lookup
        )
        if not protected:
            return 0
        refusal = decide(payload, protected, run_producer, run_pgrep)
    except Exception as exc:  # noqa: BLE001 - fail open, never brick Bash
        print(
            f"kill-guard: internal error, allowing ({type(exc).__name__}: {_trunc(str(exc), 80)})",
            file=sys.stderr,
        )
        return 0
    if refusal is None:
        return 0
    print(refusal.message(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
