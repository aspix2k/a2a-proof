from __future__ import annotations

import pytest

import a2a_proof.assertions as assertions_module
from a2a_proof.assertions import evaluate
from a2a_proof.models import Expectation, TextExpectation
from a2a_proof.protocol import TurnOutcome


def _outcome(
    *,
    state: str = "completed",
    text: str = "Hello, World!",
    duration_ms: int = 100,
) -> TurnOutcome:
    return TurnOutcome(
        state=state,
        text=text,
        task_id=None,
        context_id="context",
        duration_ms=duration_ms,
    )


def test_accepts_all_supported_text_assertions() -> None:
    expectation = Expectation(
        state="COMPLETED",
        max_seconds=0.2,
        text=TextExpectation(
            contains="hello",
            not_contains="error",
            matches=r"world!$",
            equals="hello, world!",
            case_sensitive=False,
        ),
    )

    assert evaluate(expectation, _outcome()) == []


def test_reports_each_failed_assertion() -> None:
    expectation = Expectation(
        state="input-required",
        max_seconds=0.05,
        text=TextExpectation(
            contains=["missing"],
            not_contains=["World"],
            matches=[r"^bye"],
            equals="different",
        ),
    )

    failures = evaluate(expectation, _outcome())

    assert failures == [
        "expected state 'input_required', got 'completed'",
        "expected at most 0.05s, got 0.100s",
        "response text does not contain 'missing'",
        "response text contains forbidden value 'World'",
        "response text is not equal to the expected value",
        "response text does not match /^bye/",
    ]


def test_failed_state_is_only_accepted_when_explicitly_expected() -> None:
    outcome = _outcome(state="failed")

    assert evaluate(Expectation(), outcome) == ["agent ended in 'failed' state"]
    assert evaluate(Expectation(state="failed"), outcome) == []


def test_duration_limit_is_inclusive() -> None:
    expectation = Expectation(max_seconds=1)

    assert evaluate(expectation, _outcome(duration_ms=1_000)) == []
    assert evaluate(expectation, _outcome(duration_ms=1_001)) == ["expected at most 1s, got 1.001s"]


def test_normalizes_state_separators() -> None:
    assert evaluate(Expectation(state=" INPUT-REQUIRED "), _outcome(state="input_required")) == []
    assert evaluate(Expectation(state="input required"), _outcome(state="input_required")) == []


def test_equals_respects_case_sensitivity() -> None:
    outcome = _outcome(text="Answer")

    assert (
        evaluate(Expectation(text=TextExpectation(equals="ANSWER", case_sensitive=False)), outcome)
        == []
    )
    assert evaluate(Expectation(text=TextExpectation(equals="answer")), outcome) == [
        "response text is not equal to the expected value"
    ]


def test_case_sensitive_regex_uses_standard_flags() -> None:
    expectation = Expectation(text=TextExpectation(matches=r"A+"))

    assert evaluate(expectation, _outcome(text="AAA")) == []


def test_reports_regular_expression_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def time_out(*args: object, **kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(assertions_module.regex, "search", time_out)
    expectation = Expectation(text=TextExpectation(matches=["(a+)+$"]))

    assert evaluate(expectation, _outcome(text="aaaa!")) == [
        "regular expression /(a+)+$/ timed out"
    ]


def test_enforces_regular_expression_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assertions_module, "REGEX_TIMEOUT_SECONDS", 0.001)
    expectation = Expectation(text=TextExpectation(matches=["(a+)+$"]))

    assert evaluate(expectation, _outcome(text="a" * 500 + "!")) == [
        "regular expression /(a+)+$/ timed out"
    ]
