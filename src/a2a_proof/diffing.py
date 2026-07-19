from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from a2a_proof.models import DiffCheck, DiffResult, ScenarioResult, SuiteResult

CheckStatus = Literal["passed", "failed", "not_run"]


def compare_results(
    baseline: SuiteResult,
    candidate: SuiteResult,
    scenario_names: Sequence[str],
) -> DiffResult:
    checks: list[DiffCheck] = []
    if baseline.card is not None or candidate.card is not None:
        checks.append(
            _check(
                "Agent Card",
                _card_status(baseline),
                _card_status(candidate),
            )
        )

    baseline_scenarios = {scenario.name: scenario for scenario in baseline.scenarios}
    candidate_scenarios = {scenario.name: scenario for scenario in candidate.scenarios}
    checks.extend(
        _check(
            name,
            _scenario_status(baseline_scenarios.get(name)),
            _scenario_status(candidate_scenarios.get(name)),
        )
        for name in scenario_names
    )
    return DiffResult(
        passed=candidate.passed,
        baseline=baseline,
        candidate=candidate,
        checks=checks,
    )


def _card_status(result: SuiteResult) -> CheckStatus:
    if result.card is None:
        return "not_run"
    return "passed" if result.card.passed else "failed"


def _scenario_status(scenario: ScenarioResult | None) -> CheckStatus:
    if scenario is None:
        return "not_run"
    return "passed" if scenario.passed else "failed"


def _check(name: str, baseline: CheckStatus, candidate: CheckStatus) -> DiffCheck:
    if baseline == candidate:
        change = "unchanged"
    elif baseline == "passed":
        change = "regression"
    elif candidate == "passed":
        change = "improvement"
    else:
        change = "changed"
    return DiffCheck(name=name, baseline=baseline, candidate=candidate, change=change)
