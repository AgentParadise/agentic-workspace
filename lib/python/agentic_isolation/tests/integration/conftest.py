"""Report each docker_conformance and e2b_live test by name, whatever the -q/-v level.

CI runs `pytest -q`, which prints only dots; this section is the per-test
record that a conformance run passed rather than skipped. A skipped test is
written as NOT RUN, so a run without a backend (or without E2B_API_KEY,
#1915) can never be read as green (PC-192).
"""

import pytest

_MARKERS = ("docker_conformance", "e2b_live")
_outcomes: dict[str, str] = {}


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not any(marker in report.keywords for marker in _MARKERS):
        return
    if report.outcome != "passed":
        # The first non-pass (setup error, call failure, skip) is the outcome.
        _outcomes.setdefault(report.nodeid, report.outcome)
    elif report.when == "call":
        _outcomes.setdefault(report.nodeid, "passed")


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _outcomes:
        return
    terminalreporter.section(" / ".join(_MARKERS))
    for nodeid, outcome in sorted(_outcomes.items()):
        shown = "NOT RUN" if outcome == "skipped" else outcome.upper()
        terminalreporter.write_line(f"{shown} {nodeid}")
    not_run = sum(outcome == "skipped" for outcome in _outcomes.values())
    if not_run:
        terminalreporter.write_line(
            f"{not_run} conformance/live test(s) NOT RUN: this is not a pass", red=True
        )
