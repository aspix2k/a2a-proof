from __future__ import annotations

from a2a_proof.diffing import compare_results
from a2a_proof.models import CardResult, ScenarioResult, SuiteResult


def _scenario(name: str, passed: bool) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        passed=passed,
        passed_trials=int(passed),
        required_trials=1,
        trials=[],
    )


def test_compares_card_and_scenario_contract_outcomes() -> None:
    baseline = SuiteResult(
        passed=False,
        duration_ms=1,
        card=CardResult(passed=True),
        scenarios=[
            _scenario("unchanged pass", True),
            _scenario("regression", True),
            _scenario("improvement", False),
            _scenario("changed", False),
        ],
    )
    candidate = SuiteResult(
        passed=False,
        duration_ms=1,
        card=CardResult(passed=False),
        scenarios=[
            _scenario("unchanged pass", True),
            _scenario("regression", False),
            _scenario("improvement", True),
        ],
    )

    result = compare_results(
        baseline,
        candidate,
        ["unchanged pass", "regression", "improvement", "changed"],
    )

    assert not result.passed
    assert [(check.name, check.change) for check in result.checks] == [
        ("Agent Card", "regression"),
        ("unchanged pass", "unchanged"),
        ("regression", "regression"),
        ("improvement", "improvement"),
        ("changed", "changed"),
    ]
    assert result.checks[-1].candidate == "not_run"


def test_omits_unconfigured_card_check_and_preserves_candidate_status() -> None:
    baseline = SuiteResult(passed=True, duration_ms=1, scenarios=[])
    candidate = SuiteResult(passed=True, duration_ms=1, scenarios=[])

    result = compare_results(baseline, candidate, [])

    assert result.passed
    assert result.checks == []

    candidate.card = CardResult(passed=True)
    with_candidate_card = compare_results(baseline, candidate, [])
    assert with_candidate_card.checks[0].baseline == "not_run"
    assert with_candidate_card.checks[0].change == "improvement"
