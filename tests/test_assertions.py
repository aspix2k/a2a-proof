from __future__ import annotations

import pytest

import a2a_proof.assertions as assertions_module
from a2a_proof.assertions import evaluate
from a2a_proof.models import DataExpectation, DataPartResult, Expectation, TextExpectation
from a2a_proof.protocol import TurnOutcome


def _outcome(
    *,
    state: str = "completed",
    text: str = "Hello, World!",
    duration_ms: int = 100,
    data: tuple[DataPartResult, ...] = (),
) -> TurnOutcome:
    return TurnOutcome(
        state=state,
        text=text,
        task_id=None,
        context_id="context",
        duration_ms=duration_ms,
        data=data,
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


def test_matches_structured_data_by_source_artifact_and_json_pointer() -> None:
    data = (
        DataPartResult(source="message", value={"phase": "working"}),
        DataPartResult(
            source="artifact",
            artifact_id="result",
            artifact_name="forecast",
            media_type="application/json",
            value={
                "city/name": "Paris",
                "temperatures": [21.0],
                "meta": {"unit~name": "C"},
            },
        ),
    )
    expectation = Expectation(
        data=[
            DataExpectation(
                source="artifact",
                artifact_name="forecast",
                media_type="APPLICATION/JSON",
                path="/city~1name",
                equals="Paris",
            ),
            DataExpectation(path="/temperatures/0", equals=21),
            DataExpectation(path="/meta/unit~0name", equals="C"),
        ]
    )

    assert evaluate(expectation, _outcome(data=data)) == []


def test_reports_structured_data_location_path_and_value_failures() -> None:
    data = (
        DataPartResult(
            source="artifact",
            artifact_name="forecast",
            value={"city": "London"},
        ),
    )
    expectation = Expectation(
        data=[
            DataExpectation(source="message", path="/city", equals="Paris"),
            DataExpectation(path="/missing", equals=True),
            DataExpectation(path="/city", equals="Paris"),
        ]
    )

    assert evaluate(expectation, _outcome(data=data)) == [
        "no structured data matched source 'message'",
        "structured data path '/missing' was not found",
        'expected structured data at \'/city\' to equal "Paris", got "London"',
    ]


def test_structured_data_equality_distinguishes_booleans_from_numbers() -> None:
    data = (DataPartResult(source="message", value={"flag": True, "number": 1.0}),)

    assert evaluate(
        Expectation(
            data=[
                DataExpectation(path="/flag", equals=1),
                DataExpectation(path="/number", equals=1),
            ]
        ),
        _outcome(data=data),
    ) == ["expected structured data at '/flag' to equal 1, got true"]


def test_structured_data_can_equal_null_at_the_root() -> None:
    data = (DataPartResult(source="message", value=None),)

    assert (
        evaluate(
            Expectation(data=DataExpectation(equals=None)),
            _outcome(data=data),
        )
        == []
    )


def test_structured_data_matches_nested_objects_and_arrays_at_the_root() -> None:
    value = {"items": [1.0, {"ok": True}]}

    assert (
        evaluate(
            Expectation(data=DataExpectation(equals={"items": [1, {"ok": True}]})),
            _outcome(data=(DataPartResult(source="message", value=value),)),
        )
        == []
    )


def test_structured_data_reports_different_object_at_the_root() -> None:
    expectation = Expectation(data=DataExpectation(equals={"city": "Paris"}))
    data = (DataPartResult(source="message", value={"city": "London"}),)

    assert evaluate(expectation, _outcome(data=data)) == [
        'expected structured data at \'<root>\' to equal {"city":"Paris"}, got {"city":"London"}'
    ]


def test_structured_data_checks_expectations_after_a_match() -> None:
    data = (DataPartResult(source="message", value={"city": "Paris"}),)
    expectation = Expectation(
        data=[
            DataExpectation(path="/city", equals="Paris"),
            DataExpectation(path="/city", equals="London"),
        ]
    )

    assert evaluate(expectation, _outcome(data=data)) == [
        'expected structured data at \'/city\' to equal "London", got "Paris"'
    ]


def test_structured_data_reports_bounded_candidate_values() -> None:
    data = tuple(DataPartResult(source="message", value=value) for value in range(4))

    assert evaluate(
        Expectation(data=DataExpectation(equals=9)),
        _outcome(data=data),
    ) == ["expected structured data at '<root>' to equal 9, got 0, 1, 2"]


def test_structured_data_reports_missing_unfiltered_parts() -> None:
    assert evaluate(Expectation(data=DataExpectation(equals={})), _outcome()) == [
        "no structured data matched the expectation"
    ]


def test_structured_data_reports_all_location_filters() -> None:
    part = DataPartResult(source="artifact", value={}, artifact_name="forecast")
    expectation = Expectation(
        data=[
            DataExpectation(
                source="artifact",
                artifact_name="forecast",
                media_type="application/json",
                equals={},
            ),
            DataExpectation(artifact_name="other", equals={}),
        ]
    )

    assert evaluate(expectation, _outcome(data=(part,))) == [
        "no structured data matched source 'artifact', artifact 'forecast', "
        "media type 'application/json'",
        "no structured data matched artifact 'other'",
    ]


def test_structured_data_rejects_invalid_array_location_and_bounds_preview() -> None:
    expectation = Expectation(
        data=[
            DataExpectation(path="/items/01", equals="value"),
            DataExpectation(path="/items/2", equals="value"),
            DataExpectation(path="/text/value", equals="value"),
            DataExpectation(path="/text", equals="y" * 300),
        ]
    )
    part = DataPartResult(
        source="message",
        value={"items": ["value"], "text": "x" * 300},
    )

    failures = evaluate(expectation, _outcome(data=(part,)))

    assert failures[:3] == [
        "structured data path '/items/01' was not found",
        "structured data path '/items/2' was not found",
        "structured data path '/text/value' was not found",
    ]
    assert failures[3].endswith("…")


def test_structured_data_preview_includes_the_exact_limit() -> None:
    value = "x" * (assertions_module.JSON_PREVIEW_CHARS - 2)

    assert assertions_module._json_preview(value) == f'"{value}"'


def test_structured_data_pointer_handles_arrays_strictly() -> None:
    part = DataPartResult(
        source="message",
        value={
            "items": [{"name": "first"}, {"name": "second"}],
            "scalar": "x",
        },
    )
    expectation = Expectation(
        data=[
            DataExpectation(path="/items/0/name", equals="first"),
            DataExpectation(path="/items/1/name", equals="second"),
            DataExpectation(path="/items/01/name", equals="second"),
            DataExpectation(path="/items/2", equals=None),
            DataExpectation(path="/items/name", equals=None),
            DataExpectation(path="/scalar/0", equals="x"),
        ]
    )

    assert evaluate(expectation, _outcome(data=(part,))) == [
        "structured data path '/items/01/name' was not found",
        "structured data path '/items/2' was not found",
        "structured data path '/items/name' was not found",
        "structured data path '/scalar/0' was not found",
    ]


def test_structured_data_equality_requires_matching_container_types() -> None:
    data = (
        DataPartResult(source="message", value={"value": 1}),
        DataPartResult(source="message", value=[1]),
    )
    assert evaluate(
        Expectation(data=DataExpectation(equals="value")),
        _outcome(data=data),
    ) == ['expected structured data at \'<root>\' to equal "value", got {"value":1}, [1]']
    assert evaluate(
        Expectation(data=DataExpectation(equals=[2])),
        _outcome(data=(data[1],)),
    ) == ["expected structured data at '<root>' to equal [2], got [1]"]


def test_structured_data_preview_is_stable_and_readable() -> None:
    assert assertions_module._json_preview({"é": 1, "a": 2}) == '{"a":2,"é":1}'
