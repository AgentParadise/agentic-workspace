"""Report each docker_conformance and e2b_live test by name, whatever the -q/-v level.

CI runs `pytest -q`, which prints only dots; this section is the per-test
record that a conformance run passed rather than skipped. A skipped test is
written as NOT RUN, so a run without a backend (or without E2B_API_KEY,
#1915) can never be read as green (PC-192).
"""

import pytest

_MARKERS = ("docker_conformance", "e2b_live")
_outcomes: dict[str, str] = {}
# A later phase can only make a test's record worse: a teardown or cleanup
# failure after a passed call is a failure, never PASSED.
_PRECEDENCE = {"passed": 0, "skipped": 1, "failed": 2}


def record(outcomes: dict[str, str], report: pytest.TestReport) -> None:
    """Fold one phase report into the per-test outcome, worst outcome winning."""
    if report.outcome == "passed" and report.when != "call":
        return
    known = outcomes.get(report.nodeid)
    if known is None or _PRECEDENCE[report.outcome] > _PRECEDENCE[known]:
        outcomes[report.nodeid] = report.outcome


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if any(marker in report.keywords for marker in _MARKERS):
        record(_outcomes, report)


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
