from __future__ import annotations

from pathlib import Path

import pytest
from a2a.types import AgentCard, AgentSkill

import a2a_proof.assertions as assertions_module
from a2a_proof.assertions import evaluate, evaluate_card, evaluate_invariants
from a2a_proof.models import (
    AgentCapabilitiesExpectation,
    AgentCardExpectation,
    AP2MandateExpectation,
    DataExpectation,
    DataPartResult,
    Expectation,
    FileExpectation,
    FilePartResult,
    Invariants,
    StateSequenceExpectation,
    TextExpectation,
    TextInvariant,
)
from a2a_proof.protocol import TurnOutcome


def _outcome(
    *,
    state: str = "completed",
    text: str = "Hello, World!",
    duration_ms: int = 100,
    first_event_ms: int | None = 50,
    data: tuple[DataPartResult, ...] = (),
    states: tuple[str, ...] = ("working", "completed"),
    files: tuple[FilePartResult, ...] = (),
) -> TurnOutcome:
    return TurnOutcome(
        state=state,
        text=text,
        task_id=None,
        context_id="context",
        duration_ms=duration_ms,
        data=data,
        first_event_ms=first_event_ms,
        states=states,
        files=files,
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


def test_passes_ap2_inputs_to_the_ap2_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_dir = Path("contract")
    ap2 = [
        AP2MandateExpectation(
            type="payment",
            trusted_root_jwk="root.jwk",
            audience="merchant",
            nonce="nonce",
        )
    ]
    data = (
        DataPartResult(
            source="artifact",
            artifact_name="payment",
            media_type="application/json",
            value={"mandate": "token"},
        ),
    )
    calls: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        assertions_module,
        "evaluate_ap2",
        lambda *args: calls.append(args) or ["AP2 failure"],
    )

    failures = evaluate(Expectation(ap2=ap2), _outcome(data=data), contract_dir=contract_dir)

    assert failures == ["AP2 failure"]
    assert calls == [(ap2, data, contract_dir)]


def test_evaluates_global_text_invariants_without_exposing_secret_values() -> None:
    invariants = Invariants(
        text=TextInvariant(
            not_contains="system prompt",
            not_contains_env="API_TOKEN",
            case_sensitive=False,
        )
    )

    failures = evaluate_invariants(
        invariants,
        _outcome(text="SYSTEM PROMPT: SeCrEt"),
        {"API_TOKEN": "secret"},
    )

    assert failures == [
        "response text violates global not_contains invariant 1",
        "response text contains value from environment variable 'API_TOKEN'",
    ]
    assert "secret" not in " ".join(failures).casefold()


def test_accepts_response_that_satisfies_global_text_invariants() -> None:
    invariants = Invariants(text=TextInvariant(not_contains=["private"], case_sensitive=True))

    assert evaluate_invariants(invariants, _outcome(text="PRIVATE"), {}) == []


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


def test_first_event_limit_is_inclusive_and_requires_timing() -> None:
    expectation = Expectation(max_first_event_seconds=0.1)

    assert evaluate(expectation, _outcome(first_event_ms=100)) == []
    assert evaluate(expectation, _outcome(first_event_ms=101)) == [
        "expected first event within 0.1s, got 0.101s"
    ]
    assert evaluate(expectation, _outcome(first_event_ms=None)) == [
        "agent returned no first-event timing"
    ]
    assert evaluate(Expectation(max_first_event_seconds=1), _outcome(first_event_ms=1_001)) == [
        "expected first event within 1s, got 1.001s"
    ]


def test_normalizes_state_separators() -> None:
    assert evaluate(Expectation(state=" INPUT-REQUIRED "), _outcome(state="input_required")) == []
    assert evaluate(Expectation(state="input required"), _outcome(state="input_required")) == []


def test_checks_exact_and_partial_state_trajectories() -> None:
    exact = Expectation(
        states=StateSequenceExpectation(equals=["WORKING", "input-required", "completed"])
    )
    partial = Expectation(
        states=StateSequenceExpectation(contains_in_order=["working", "completed"])
    )
    outcome = _outcome(states=("working", "input_required", "completed"))

    assert evaluate(exact, outcome) == []
    assert evaluate(partial, outcome) == []
    assert evaluate(
        Expectation(states=StateSequenceExpectation(equals=["working", "completed"])), outcome
    ) == [
        "expected state sequence ['working', 'completed'], got "
        "['working', 'input_required', 'completed']"
    ]
    assert evaluate(
        Expectation(states=StateSequenceExpectation(contains_in_order=["completed", "working"])),
        outcome,
    ) == [
        "expected state sequence to contain ['completed', 'working'] in order, got "
        "['working', 'input_required', 'completed']"
    ]


def test_matches_file_parts_by_metadata_and_exact_count() -> None:
    files = (
        FilePartResult(
            source="message",
            kind="raw",
            filename="progress.txt",
            media_type="text/plain",
            size_bytes=3,
        ),
        FilePartResult(
            source="artifact",
            kind="url",
            filename="report.pdf",
            media_type="application/pdf",
            artifact_name="report",
        ),
    )
    expectation = Expectation(
        files=[
            FileExpectation(
                source="artifact",
                artifact_name="report",
                filename="report.pdf",
                media_type="APPLICATION/PDF",
                kind="url",
            ),
            FileExpectation(kind="raw", count=1),
            FileExpectation(media_type="image/png", count=0),
        ]
    )

    assert evaluate(expectation, _outcome(files=files)) == []
    assert evaluate(
        Expectation(files=[FileExpectation(filename="missing.pdf")]),
        _outcome(files=files),
    ) == ["expected 1 file part(s) matching filename 'missing.pdf', got 0"]
    assert evaluate(
        Expectation(files=[FileExpectation(count=1)]),
        _outcome(files=files),
    ) == ["expected 1 file part(s) matching all file parts, got 2"]
    assert evaluate(
        Expectation(
            files=[
                FileExpectation(
                    source="artifact",
                    artifact_name="report",
                    filename="report.pdf",
                    media_type="application/pdf",
                    kind="url",
                    count=2,
                )
            ]
        ),
        _outcome(files=files),
    ) == [
        "expected 2 file part(s) matching source 'artifact', artifact name 'report', "
        "filename 'report.pdf', media type 'application/pdf', kind 'url', got 1"
    ]


def test_checks_a_single_agent_card_capability() -> None:
    expectation = AgentCardExpectation(capabilities=AgentCapabilitiesExpectation(streaming=False))

    assert evaluate_card(expectation, AgentCard(name="Agent")) == []
    skills_only = AgentCardExpectation.model_validate({"skills": {"contains": "echo"}})
    assert (
        evaluate_card(
            skills_only,
            AgentCard(name="Agent", skills=[AgentSkill(id="echo")]),
        )
        == []
    )
    assert evaluate_card(
        skills_only,
        AgentCard(name="Agent", skills=[AgentSkill(id="ECHO")]),
    ) == ["Agent Card does not contain skill ID 'echo'"]

    extended = AgentCardExpectation(
        capabilities=AgentCapabilitiesExpectation(extended_agent_card=True)
    )
    assert evaluate_card(extended, AgentCard(name="Agent")) == [
        "expected Agent Card capability 'extended agent card' to be True, got False"
    ]


def test_equals_respects_case_sensitivity() -> None:
    outcome = _outcome(text="Answer")

    assert (
        evaluate(Expectation(text=TextExpectation(equals="ANSWER", case_sensitive=False)), outcome)
        == []
    )
    assert evaluate(Expectation(text=TextExpectation(equals="answer")), outcome) == [
        "response text is not equal to the expected value"
    ]


def test_case_sensitive_regex_uses_standard_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, int, float]] = []

    def search(pattern: str, value: str, flags: int, *, timeout: float) -> object:
        calls.append((pattern, value, flags, timeout))
        return object()

    monkeypatch.setattr(assertions_module.regex, "search", search)
    expectation = Expectation(text=TextExpectation(matches=r"A+"))

    assert evaluate(expectation, _outcome(text="AAA")) == []
    assert calls == [("A+", "AAA", 0, assertions_module.REGEX_TIMEOUT_SECONDS)]


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


def test_structured_data_equality_does_not_coerce_arrays() -> None:
    data = (DataPartResult(source="message", value=[1]),)

    assert evaluate(Expectation(data=DataExpectation(equals=1)), _outcome(data=data)) == [
        "expected structured data at '<root>' to equal 1, got [1]"
    ]


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


def test_structured_data_checks_existence() -> None:
    data = (DataPartResult(source="message", value={"city": "Paris"}),)

    assert (
        evaluate(
            Expectation(
                data=[
                    DataExpectation(path="/city", exists=True),
                    DataExpectation(path="/country", exists=False),
                ]
            ),
            _outcome(data=data),
        )
        == []
    )
    assert evaluate(
        Expectation(data=DataExpectation(path="/city", exists=False)),
        _outcome(data=data),
    ) == ["expected structured data path '/city' to be absent, got \"Paris\""]
    assert evaluate(
        Expectation(data=DataExpectation(path="/country", exists=True)),
        _outcome(data=data),
    ) == ["structured data path '/country' was not found"]


def test_structured_data_matches_string_values_with_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = (
        DataPartResult(source="message", value={"value": 42}),
        DataPartResult(source="message", value={"value": "order-42"}),
    )
    expectation = Expectation(data=DataExpectation(path="/value", matches=r"^order-\d+$"))

    assert evaluate(expectation, _outcome(data=data)) == []

    monkeypatch.setattr(assertions_module.regex, "search", lambda *args, **kwargs: None)
    assert evaluate(expectation, _outcome(data=data)) == [
        "expected structured data at '/value' to match /^order-\\d+$/, got 42, \"order-42\""
    ]

    def time_out(*args: object, **kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(assertions_module.regex, "search", time_out)
    assert evaluate(expectation, _outcome(data=data)) == [
        "regular expression /^order-\\d+$/ timed out"
    ]


def test_structured_data_regex_uses_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, float]] = []

    def search(pattern: str, value: str, *, timeout: float) -> object:
        calls.append((pattern, value, timeout))
        return object()

    monkeypatch.setattr(assertions_module.regex, "search", search)
    expectation = Expectation(data=DataExpectation(matches="Paris"))
    data = (DataPartResult(source="message", value="Paris"),)

    assert evaluate(expectation, _outcome(data=data)) == []
    assert calls == [("Paris", "Paris", assertions_module.REGEX_TIMEOUT_SECONDS)]


def test_structured_data_checks_numeric_bounds_without_treating_booleans_as_numbers() -> None:
    data = (
        DataPartResult(source="message", value={"price": True}),
        DataPartResult(source="message", value={"price": 19.5}),
    )
    expectation = Expectation(data=DataExpectation(path="/price", gt=10, gte=19.5, lt=20, lte=19.5))

    assert evaluate(expectation, _outcome(data=data)) == []
    assert evaluate(
        Expectation(data=DataExpectation(path="/price", gt=20)),
        _outcome(data=data),
    ) == ["expected structured data at '/price' to be > 20, got true, 19.5"]
    assert evaluate(
        Expectation(data=DataExpectation(path="/price", gt=19.5)),
        _outcome(data=data),
    ) == ["expected structured data at '/price' to be > 19.5, got true, 19.5"]


@pytest.mark.parametrize(
    ("expectation", "value", "description"),
    [
        (DataExpectation(gte=10), 9, ">= 10"),
        (DataExpectation(lt=10), 10, "< 10"),
        (DataExpectation(lte=10), 11, "<= 10"),
    ],
)
def test_structured_data_reports_each_numeric_boundary(
    expectation: DataExpectation,
    value: int,
    description: str,
) -> None:
    assert evaluate(
        Expectation(data=expectation),
        _outcome(data=(DataPartResult(source="message", value=value),)),
    ) == [f"expected structured data at '<root>' to be {description}, got {value}"]


def test_structured_data_reports_combined_numeric_bounds() -> None:
    expectation = Expectation(data=DataExpectation(gte=10, lt=20, lte=15))

    assert evaluate(
        expectation,
        _outcome(data=(DataPartResult(source="message", value=30),)),
    ) == ["expected structured data at '<root>' to be >= 10 and < 20 and <= 15, got 30"]


def test_structured_data_rejects_non_numeric_candidates_for_numeric_bounds() -> None:
    expectation = Expectation(data=DataExpectation(gt=0))

    assert evaluate(
        expectation,
        _outcome(data=(DataPartResult(source="message", value="one"),)),
    ) == ["expected structured data at '<root>' to be > 0, got \"one\""]


def test_structured_data_validates_json_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    }
    expectation = Expectation(data=DataExpectation(json_schema=schema))

    assert (
        evaluate(
            expectation,
            _outcome(data=(DataPartResult(source="message", value={"city": "Paris"}),)),
        )
        == []
    )
    assert evaluate(
        expectation,
        _outcome(data=(DataPartResult(source="message", value={"country": "France"}),)),
    ) == ["structured data at '<root>' does not match JSON Schema: 'city' is a required property"]

    multiple = (
        DataPartResult(source="message", value={"country": "France"}),
        DataPartResult(source="message", value={"city": 42}),
    )
    assert evaluate(expectation, _outcome(data=multiple)) == [
        "structured data at '<root>' does not match JSON Schema: 'city' is a required property"
    ]
    mixed = (
        DataPartResult(source="message", value={"country": "France"}),
        DataPartResult(source="message", value={"city": "Paris"}),
    )
    assert evaluate(expectation, _outcome(data=mixed)) == []

    referenced = Expectation(
        data=DataExpectation(
            json_schema={
                "$defs": {"city": {"type": "string"}},
                "$ref": "#/$defs/city",
            }
        )
    )
    assert (
        evaluate(
            referenced,
            _outcome(data=(DataPartResult(source="message", value="Paris"),)),
        )
        == []
    )


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
