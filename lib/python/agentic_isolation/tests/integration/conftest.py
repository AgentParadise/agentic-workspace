"""Report each docker_conformance test by name, whatever the -q/-v level.

CI runs `pytest -q`, which prints only dots; this section is the per-test
record that a Docker conformance run passed rather than skipped.
"""

import pytest

_MARKER = "docker_conformance"
_outcomes: dict[str, str] = {}


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _MARKER not in report.keywords:
        return
    if report.outcome != "passed":
        # The first non-pass (setup error, call failure, skip) is the outcome.
        _outcomes.setdefault(report.nodeid, report.outcome)
    elif report.when == "call":
        _outcomes.setdefault(report.nodeid, "passed")


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _outcomes:
        return
    terminalreporter.section(_MARKER)
    for nodeid, outcome in sorted(_outcomes.items()):
        terminalreporter.write_line(f"{outcome.upper()} {nodeid}")
