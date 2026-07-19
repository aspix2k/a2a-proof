from __future__ import annotations

import unicodedata
import xml.etree.ElementTree as ET

from rich.console import Console
from rich.table import Table
from rich.text import Text

from a2a_proof.models import DiffResult, ScenarioResult, SuiteResult

MAX_DIAGNOSTIC_CHARS = 2_000
MILLISECONDS_PER_SECOND = 1_000
XML_CODEPOINT_RANGES = ((0x20, 0xD7FF), (0xE000, 0xFFFD), (0x10000, 0x10FFFF))


def render_terminal(result: SuiteResult, console: Console, *, verbose: bool) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Result", width=6)
    table.add_column("Scenario")
    table.add_column("Trials", justify="right")
    table.add_column("Time", justify="right")

    if result.card is not None:
        table.add_row(
            Text("PASS", style="green") if result.card.passed else Text("FAIL", style="red"),
            "Agent Card",
            "—",
            "—",
        )
    for scenario in result.scenarios:
        elapsed = sum(trial.duration_ms for trial in scenario.trials)
        table.add_row(
            Text("PASS", style="green") if scenario.passed else Text("FAIL", style="red"),
            Text(_safe_text(scenario.name, single_line=True)),
            f"{scenario.passed_trials}/{len(scenario.trials)}",
            _duration(elapsed),
        )
    console.print(table)

    if result.card is not None and not result.card.passed:
        console.print(Text("\nAgent Card", style="bold red"))
        for failure in result.card.failures:
            console.print(Text(f"  {_diagnostic(failure)}"))

    for scenario in result.scenarios:
        if scenario.passed:
            continue
        _render_scenario_failures(scenario, console, verbose=verbose)

    status = "passed" if result.passed else "failed"
    style = "bold green" if result.passed else "bold red"
    console.print(
        Text(
            f"\n{_result_count(result)} {status} in {_duration(result.duration_ms)}",
            style=style,
        )
    )


def render_json(result: SuiteResult) -> str:
    return result.model_dump_json(indent=2)


def render_diff_terminal(result: DiffResult, console: Console) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Baseline")
    table.add_column("Candidate")
    table.add_column("Change")
    for check in result.checks:
        table.add_row(
            Text(_safe_text(check.name, single_line=True)),
            _diff_status(check.baseline),
            _diff_status(check.candidate),
            _diff_change(check.change),
        )
    console.print(table)

    if result.candidate.card is not None and not result.candidate.card.passed:
        console.print(Text("\nCandidate Agent Card", style="bold red"))
        for failure in result.candidate.card.failures:
            console.print(Text(f"  {_diagnostic(failure)}"))
    for scenario in result.candidate.scenarios:
        if not scenario.passed:
            _render_scenario_failures(scenario, console, verbose=False)

    regressions = sum(check.change == "regression" for check in result.checks)
    improvements = sum(check.change == "improvement" for check in result.checks)
    status = "passed" if result.passed else "failed"
    style = "bold green" if result.passed else "bold red"
    console.print(
        Text(
            f"\nCandidate {status}; {regressions} regressions, {improvements} improvements.",
            style=style,
        )
    )


def render_diff_json(result: DiffResult) -> str:
    return result.model_dump_json(indent=2)


def _render_scenario_failures(
    scenario: ScenarioResult,
    console: Console,
    *,
    verbose: bool,
) -> None:
    console.print(Text(f"\n{_safe_text(scenario.name, single_line=True)}", style="bold red"))
    if scenario.latency is not None:
        for failure in scenario.latency.failures:
            console.print(Text(f"  latency: {_diagnostic(failure)}"))
    for trial in scenario.trials:
        if trial.passed:
            continue
        if trial.error:
            console.print(Text(f"  trial {trial.index}: {_diagnostic(trial.error)}"))
        for turn in trial.turns:
            for failure in turn.failures:
                console.print(
                    Text(f"  trial {trial.index}, turn {turn.index}: {_diagnostic(failure)}")
                )
            if verbose and turn.text:
                response = _diagnostic(turn.text).replace("\n", "\n            ")
                console.print(Text(f"  response: {response}", style="dim"))


def render_junit(result: SuiteResult) -> str:
    trials = [trial for scenario in result.scenarios for trial in scenario.trials]
    latency_results = [
        scenario.latency for scenario in result.scenarios if scenario.latency is not None
    ]
    card_failed = result.card is not None and not result.card.passed
    tolerated_trials = [
        trial
        for scenario in result.scenarios
        if scenario.passed_trials >= scenario.required_trials
        for trial in scenario.trials
        if not trial.passed
    ]
    failures = (
        sum(
            not trial.passed
            and trial.error is None
            and scenario.passed_trials < scenario.required_trials
            for scenario in result.scenarios
            for trial in scenario.trials
        )
        + sum(not latency.passed for latency in latency_results)
        + card_failed
    )
    errors = sum(
        trial.error is not None and scenario.passed_trials < scenario.required_trials
        for scenario in result.scenarios
        for trial in scenario.trials
    )
    attributes = {
        "name": "a2a-proof",
        "tests": str(len(trials) + len(latency_results) + (result.card is not None)),
        "failures": str(failures),
        "errors": str(errors),
        "time": f"{result.duration_ms / MILLISECONDS_PER_SECOND:.3f}",
    }
    if tolerated_trials:
        attributes["skipped"] = str(len(tolerated_trials))
    suite = ET.Element(
        "testsuite",
        attributes,
    )
    if result.card is not None:
        case = ET.SubElement(
            suite,
            "testcase",
            {"classname": "a2a-proof", "name": "Agent Card", "time": "0.000"},
        )
        if not result.card.passed:
            message = _xml_text(_diagnostic("; ".join(result.card.failures)))
            failure = ET.SubElement(case, "failure", {"message": message})
            failure.text = _xml_text("\n".join(result.card.failures))
    for scenario in result.scenarios:
        for trial in scenario.trials:
            name = scenario.name
            if len(scenario.trials) > 1:
                name = f"{name} [trial {trial.index}]"
            case = ET.SubElement(
                suite,
                "testcase",
                {
                    "classname": "a2a-proof",
                    "name": _xml_text(name),
                    "time": f"{trial.duration_ms / MILLISECONDS_PER_SECOND:.3f}",
                },
            )
            if not trial.passed and scenario.passed_trials >= scenario.required_trials:
                ET.SubElement(case, "skipped", {"message": "tolerated by pass_rate"})
                continue
            if trial.error is not None:
                message = _xml_text(_diagnostic(trial.error))
                error = ET.SubElement(case, "error", {"message": message})
                error.text = message
                continue
            trial_failures = [failure for turn in trial.turns for failure in turn.failures]
            if trial_failures:
                message = _xml_text(_diagnostic("; ".join(trial_failures)))
                failure = ET.SubElement(case, "failure", {"message": message})
                failure.text = _xml_text("\n".join(trial_failures))
        if scenario.latency is not None:
            case = ET.SubElement(
                suite,
                "testcase",
                {
                    "classname": "a2a-proof",
                    "name": _xml_text(f"{scenario.name} [latency]"),
                    "time": "0.000",
                },
            )
            if not scenario.latency.passed:
                message = _xml_text(_diagnostic("; ".join(scenario.latency.failures)))
                failure = ET.SubElement(case, "failure", {"message": message})
                failure.text = _xml_text("\n".join(scenario.latency.failures))
    ET.indent(suite)
    return ET.tostring(suite, encoding="unicode", xml_declaration=True)


def _diagnostic(value: str) -> str:
    safe = _safe_text(value, single_line=False)
    if len(safe) <= MAX_DIAGNOSTIC_CHARS:
        return safe
    return f"{safe[:MAX_DIAGNOSTIC_CHARS]}…"


def _safe_text(value: str, *, single_line: bool) -> str:
    normalized = "".join(
        character
        for character in value
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    if single_line:
        return " ".join(normalized.splitlines()).strip()
    return normalized


def _xml_text(value: str) -> str:
    return "".join(
        character
        for character in value
        if character in "\t\n\r"
        or any(start <= ord(character) <= end for start, end in XML_CODEPOINT_RANGES)
    )


def _duration(milliseconds: int) -> str:
    if milliseconds < MILLISECONDS_PER_SECOND:
        return f"{milliseconds}ms"
    return f"{milliseconds / MILLISECONDS_PER_SECOND:.2f}s"


def _diff_status(status: str) -> Text:
    styles = {"passed": "green", "failed": "red", "not_run": "yellow"}
    return Text(status.upper(), style=styles[status])


def _diff_change(change: str) -> Text:
    styles = {"regression": "red", "improvement": "green", "changed": "yellow"}
    return Text(change, style=styles.get(change, ""))


def _scenario_count(count: int) -> str:
    return f"{count} scenario{'s' if count != 1 else ''}"


def _result_count(result: SuiteResult) -> str:
    scenarios = _scenario_count(len(result.scenarios))
    return f"Agent Card and {scenarios}" if result.card is not None else scenarios
