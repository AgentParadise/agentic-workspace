"""Keep live-test secrets out of failure reports (AgentParadise/agentic-workspace#91).

A live E2B run once printed the real E2B key: pytest shows every frame's
arguments in a failing traceback, and the test passed the key to
`build_template` as `api_key`. Two mechanical guards stop that class:

- `violations(source)`: a static (AST) check, run over every integration test
  by tests/test_live_secret_guard.py, that no test holds a secret in a
  variable or passes one as an argument. Secrets stay in the environment and
  are read there by the SDK or by `build_template(registry_env=...)`.
- `pytest_runtest_makereport`: a report hook, installed by this directory's
  conftest, that replaces the value of every `SECRET_VARIABLES` entry in a
  report's traceback and captured sections with `[REDACTED]`. It is the net
  under the static check, for a secret that reaches a report some other way.

Output written straight to the terminal under `-s` is not a report and is not
redacted: never print a secret.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Generator, Iterable

import pytest

#: Environment variables whose values are secrets.
SECRET_VARIABLES: tuple[str, ...] = ("E2B_API_KEY", "E2B_GHCR_TOKEN")
#: Keyword arguments that carry a secret by name.
SECRET_KEYWORDS = frozenset({"api_key", "password", "registry_password", "secret", "token"})
REDACTED = "[REDACTED]"
# Shorter values are not redacted: they would match ordinary text.
_MIN_SECRET_LENGTH = 8


def secret_values(environ: Iterable[tuple[str, str]] | None = None) -> list[str]:
    """The current values of `SECRET_VARIABLES`, longest first."""
    env = dict(environ) if environ is not None else dict(os.environ)
    values = {
        env[name] for name in SECRET_VARIABLES if len(env.get(name, "")) >= _MIN_SECRET_LENGTH
    }
    return sorted(values, key=len, reverse=True)


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


def _is_environ_read(node: ast.AST) -> str | None:
    """The literal variable name `node` reads from the environment, if any."""

    def literal(arg: ast.AST) -> str | None:
        return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None

    def is_environ(value: ast.AST) -> bool:
        return isinstance(value, ast.Attribute) and value.attr == "environ"

    if isinstance(node, ast.Subscript) and is_environ(node.value):
        return literal(node.slice)
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Attribute) and (
            (func.attr == "get" and is_environ(func.value)) or func.attr == "getenv"
        ):
            return literal(node.args[0])
    return None


def violations(source: str, filename: str = "<source>") -> list[str]:
    """Every place `source` holds a secret or passes one as an argument."""
    found: list[str] = []
    tree = ast.parse(source, filename)
    # A read wrapped directly in `bool(...)` asks whether the secret is set
    # and keeps nothing; it is the one environment read allowed.
    presence_checks = {
        id(call.args[0])
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "bool"
        and len(call.args) == 1
    }
    for node in ast.walk(tree):
        where = f"{filename}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                is_none = isinstance(keyword.value, ast.Constant) and keyword.value.value is None
                if keyword.arg in SECRET_KEYWORDS and not is_none:
                    found.append(f"{where}: secret passed as keyword {keyword.arg}=")
        name = _is_environ_read(node)
        if name in SECRET_VARIABLES and id(node) not in presence_checks:
            found.append(f"{where}: {name} read from the environment into the test")
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            found.extend(_required_bindings(node, where))
    return found


def _required_bindings(node: ast.Assign, where: str) -> list[str]:
    """A `_required(...)` result whose secret positions are bound to a name."""
    call = node.value
    assert isinstance(call, ast.Call)
    if not (isinstance(call.func, ast.Name) and call.func.id == "_required"):
        return []
    names = [a.value if isinstance(a, ast.Constant) else None for a in call.args]
    secret_positions = [i for i, name in enumerate(names) if name in SECRET_VARIABLES]
    if not secret_positions:
        return []
    found: list[str] = []
    for target in node.targets:
        if isinstance(target, ast.Tuple):
            for i in secret_positions:
                element = target.elts[i] if i < len(target.elts) else None
                if not (isinstance(element, ast.Name) and element.id == "_"):
                    found.append(f"{where}: {names[i]} bound to a variable; unpack it as _")
        else:
            found.append(f"{where}: a _required() result holding a secret bound to a variable")
    return found


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, None]:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()  # type: ignore[attr-defined]
    secrets = secret_values()
    if not secrets:
        return
    if report.longrepr is not None:
        text = str(report.longrepr)
        if any(secret in text for secret in secrets):
            report.longrepr = redact(text, secrets)
    report.sections = [(title, redact(body, secrets)) for title, body in report.sections]
