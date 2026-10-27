"""Keep live-test secrets out of failure reports (AgentParadise/agentic-workspace#91).

A live E2B run once printed the real E2B key: pytest shows every frame's
arguments in a failing traceback, and the test passed the key to
`build_template` as `api_key`. Two mechanical guards stop that class:

- `violations(source)`: a static (AST) check, run over every integration test
  by tests/test_live_secret_guard.py, that no test ever obtains a secret's
  value. Secrets stay in the environment and are read there by the SDK or by
  `build_template(registry_env=...)`. The rules:

  1. No call passes a keyword named like a secret (`api_key=`, `token=`, ...).
  2. A secret variable's NAME, as a string literal, may appear only as an
     argument of `_require_set(...)` (a presence check that returns nothing)
     or `RegistryCredentialsEnv(...)` (names, read later by the library), or
     as the key of an environment read wrapped directly in `bool(...)`.
     Anywhere else (`_required("E2B_API_KEY")[0]`, `os.environ[...]`, an
     annotated or aliased assignment, a `_` binding that still holds the
     value in a local) it is a violation.
  3. An environment read whose key is not a literal is allowed only inside
     the helpers `_ENV_HELPERS`, which rule 2 keeps from being handed a
     secret's name.

  Not covered: a whole-environment copy (`{**os.environ}`, as a subprocess
  `env=`) is a mapping, not a read, and is allowed.
- Report hooks, installed by this directory's conftest, that replace the
  value of every `SECRET_VARIABLES` entry with `[REDACTED]` in a report's
  traceback, captured sections and `user_properties` (which JUnit XML
  writes), and in recorded warnings (the terminal warnings summary and JUnit
  read those). They are the net under the static check.

Output written straight to the terminal under `-s` is not a report and is not
redacted (AgentParadise/agentic-workspace#114): never print a secret.
"""

from __future__ import annotations

import ast
import os
import warnings
from collections.abc import Generator, Iterable

import pytest

#: Environment variables whose values are secrets.
SECRET_VARIABLES: tuple[str, ...] = ("E2B_API_KEY", "E2B_GHCR_TOKEN")
#: Keyword arguments that carry a secret by name.
SECRET_KEYWORDS = frozenset({"api_key", "password", "registry_password", "secret", "token"})
#: Calls that may be given a secret variable's name: neither returns its value.
_NAME_ONLY_CALLS = frozenset({"_require_set", "RegistryCredentialsEnv"})
#: The only functions that may read the environment by a non-literal key.
_ENV_HELPERS = frozenset({"_required", "_require_set"})
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


def _environ_key(node: ast.AST) -> ast.expr | None:
    """The key expression of an environment read (`environ[k]`, `.get(k)`, `getenv(k)`)."""

    def is_environ(value: ast.AST) -> bool:
        return (isinstance(value, ast.Attribute) and value.attr == "environ") or (
            isinstance(value, ast.Name) and value.id == "environ"
        )

    if isinstance(node, ast.Subscript) and is_environ(node.value):
        return node.slice
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"get", "pop"}
            and is_environ(func.value)
        ) or name == "getenv":
            return node.args[0]
    return None


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def violations(source: str, filename: str = "<source>") -> list[str]:
    """Every place `source` could obtain a secret's value or pass one."""
    tree = ast.parse(source, filename)
    parents: dict[int, ast.AST] = {
        id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    found: list[str] = []

    def allowed_name_literal(node: ast.AST) -> bool:
        parent = parents.get(id(node))
        if _call_name(parent) in _NAME_ONLY_CALLS and node in getattr(parent, "args", ()):
            return True
        # The key of an environment read wrapped directly in bool(...).
        if parent is not None and _environ_key(parent) is node:
            wrapper = parents.get(id(parent))
            return _call_name(wrapper) == "bool" and len(getattr(wrapper, "args", ())) == 1
        return False

    def enclosing_function(node: ast.AST) -> str | None:
        current = parents.get(id(node))
        while current is not None:
            if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                return current.name
            current = parents.get(id(current))
        return None

    for node in ast.walk(tree):
        where = f"{filename}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                is_none = isinstance(keyword.value, ast.Constant) and keyword.value.value is None
                if keyword.arg in SECRET_KEYWORDS and not is_none:
                    found.append(f"{where}: secret passed as keyword {keyword.arg}=")
        if (
            isinstance(node, ast.Constant)
            and node.value in SECRET_VARIABLES
            and not allowed_name_literal(node)
        ):
            found.append(f"{where}: {node.value} named where its value could be obtained")
        key = _environ_key(node)
        if (
            key is not None
            and not isinstance(key, ast.Constant)
            and enclosing_function(node) not in _ENV_HELPERS
        ):
            found.append(f"{where}: environment read by a computed key outside {_ENV_HELPERS}")
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
    report.user_properties = [
        (name, redact(value, secrets) if isinstance(value, str) else value)
        for name, value in report.user_properties
    ]


@pytest.hookimpl(tryfirst=True)
def pytest_warning_recorded(warning_message: warnings.WarningMessage) -> None:
    """Redact a recorded warning before the terminal summary or JUnit formats it."""
    secrets = secret_values()
    if not secrets:
        return
    text = str(warning_message.message)
    if any(secret in text for secret in secrets):
        try:
            warning_message.message = warning_message.category(redact(text, secrets))
        except Exception:
            # A category whose constructor takes other arguments.
            warning_message.message = UserWarning(redact(text, secrets))
    if isinstance(warning_message.line, str):
        warning_message.line = redact(warning_message.line, secrets)
