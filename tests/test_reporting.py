from __future__ import annotations

import xml.etree.ElementTree as ET

from rich.console import Console

from a2a_proof.models import ScenarioResult, SuiteResult, TrialResult, TurnResult
from a2a_proof.reporting import (
    _diagnostic,
    _duration,
    render_json,
    render_junit,
    render_terminal,
)


def _result(*, passed: bool) -> SuiteResult:
    turn = TurnResult(
        index=1,
        passed=passed,
        state="completed",
        duration_ms=5,
        text="response\x1b[31m",
        failures=[] if passed else ["missing\x1b[31m value"],
    )
    trial = TrialResult(index=1, passed=passed, duration_ms=5, turns=[turn])
    scenario = ScenarioResult(
        name="scenario\x1b[31m",
        passed=passed,
        passed_trials=int(passed),
        required_trials=1,
        trials=[trial],
    )
    return SuiteResult(passed=passed, duration_ms=5, scenarios=[scenario])


def test_renders_terminal_without_control_characters() -> None:
    console = Console(record=True, color_system=None, width=100)

    render_terminal(_result(passed=False), console, verbose=True)

    output = console.export_text()
    assert "FAIL" in output
    assert "missing[31m value" in output
    assert "\x1b" not in output


def test_renders_machine_readable_json() -> None:
    rendered = render_json(_result(passed=True))

    assert '"passed": true' in rendered
    assert '"scenario' in rendered


def test_renders_success_and_trial_error() -> None:
    console = Console(record=True, color_system=None, width=100)
    success = _result(passed=True)
    render_terminal(success, console, verbose=False)

    failed = _result(passed=False)
    failed.scenarios[0].trials.append(
        TrialResult(index=2, passed=False, duration_ms=1, error="connection failed")
    )
    failed.scenarios[0].trials.append(TrialResult(index=3, passed=True, duration_ms=1))
    render_terminal(failed, console, verbose=False)

    output = console.export_text()
    assert "PASS" in output
    assert "connection failed" in output


def test_truncates_diagnostics_and_formats_seconds() -> None:
    assert _diagnostic("x" * 2_001).endswith("…")
    assert _duration(1_500) == "1.50s"


def test_renders_junit_failures_errors_and_trials() -> None:
    result = _result(passed=False)
    result.scenarios[0].trials.append(
        TrialResult(index=2, passed=False, duration_ms=2, error="connection failed")
    )

    root = ET.fromstring(render_junit(result))

    assert root.attrib == {
        "name": "a2a-proof",
        "tests": "2",
        "failures": "1",
        "errors": "1",
        "time": "0.005",
    }
    cases = root.findall("testcase")
    assert cases[0].attrib["name"] == "scenario[31m [trial 1]"
    assert cases[0].find("failure") is not None
    error = cases[1].find("error")
    assert error is not None
    assert error.attrib["message"] == "connection failed"
