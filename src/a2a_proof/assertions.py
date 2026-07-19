from __future__ import annotations

import json

import regex
from a2a.types import AgentCard
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from pydantic import JsonValue

from a2a_proof.models import (
    AgentCardExpectation,
    DataExpectation,
    DataPartResult,
    Expectation,
    FileExpectation,
    FilePartResult,
    StateSequenceExpectation,
    TextExpectation,
)
from a2a_proof.protocol import TurnOutcome

FAILURE_STATES = {"canceled", "failed", "rejected"}
REGEX_TIMEOUT_SECONDS = 0.1
JSON_PREVIEW_CHARS = 200


class _Missing:
    pass


_MISSING = _Missing()


def evaluate(expectation: Expectation, outcome: TurnOutcome) -> list[str]:
    failures: list[str] = []
    expected_state = _normalize_state(expectation.state) if expectation.state else None

    if outcome.state in FAILURE_STATES and expected_state != outcome.state:
        failures.append(f"agent ended in {outcome.state!r} state")
    if expected_state is not None and outcome.state != expected_state:
        failures.append(f"expected state {expected_state!r}, got {outcome.state!r}")
    if expectation.max_seconds is not None:
        actual_seconds = outcome.duration_ms / 1_000
        if actual_seconds > expectation.max_seconds:
            failures.append(
                f"expected at most {expectation.max_seconds:g}s, got {actual_seconds:.3f}s"
            )
    if expectation.max_first_event_seconds is not None:
        first_event_seconds = (
            outcome.first_event_ms / 1_000 if outcome.first_event_ms is not None else None
        )
        if first_event_seconds is None:
            failures.append("agent returned no first-event timing")
        elif first_event_seconds > expectation.max_first_event_seconds:
            failures.append(
                f"expected first event within {expectation.max_first_event_seconds:g}s, "
                f"got {first_event_seconds:.3f}s"
            )
    if expectation.text is not None:
        failures.extend(_evaluate_text(expectation.text, outcome.text))
    if expectation.states is not None:
        failures.extend(_evaluate_states(expectation.states, outcome.states))
    failures.extend(_evaluate_data(expectation.data, outcome.data))
    failures.extend(_evaluate_files(expectation.files, outcome.files))
    return failures


def evaluate_card(expectation: AgentCardExpectation, card: AgentCard) -> list[str]:
    failures: list[str] = []
    if expectation.skills is not None:
        failures.extend(
            _missing_values(
                "skill ID", expectation.skills.contains, [skill.id for skill in card.skills]
            )
        )
    if expectation.input_modes is not None:
        failures.extend(
            _missing_values(
                "input mode",
                expectation.input_modes.contains,
                list(card.default_input_modes),
                case_sensitive=False,
            )
        )
    if expectation.output_modes is not None:
        failures.extend(
            _missing_values(
                "output mode",
                expectation.output_modes.contains,
                list(card.default_output_modes),
                case_sensitive=False,
            )
        )
    if expectation.capabilities is not None:
        for name in ("streaming", "push_notifications", "extended_agent_card"):
            if name not in expectation.capabilities.model_fields_set:
                continue
            expected = getattr(expectation.capabilities, name)
            actual = bool(getattr(card.capabilities, name))
            if actual != expected:
                label = name.replace("_", " ")
                failures.append(
                    f"expected Agent Card capability {label!r} to be {expected}, got {actual}"
                )
    return failures


def _missing_values(
    label: str,
    expected: list[str],
    actual: list[str],
    *,
    case_sensitive: bool = True,
) -> list[str]:
    available = set(actual if case_sensitive else (value.casefold() for value in actual))
    missing = [
        value
        for value in expected
        if (value if case_sensitive else value.casefold()) not in available
    ]
    return [f"Agent Card does not contain {label} {value!r}" for value in missing]


def _evaluate_text(expectation: TextExpectation, actual: str) -> list[str]:
    failures: list[str] = []
    comparable = actual if expectation.case_sensitive else actual.casefold()

    for expected in expectation.contains:
        needle = expected if expectation.case_sensitive else expected.casefold()
        if needle not in comparable:
            failures.append(f"response text does not contain {expected!r}")
    for forbidden in expectation.not_contains:
        needle = forbidden if expectation.case_sensitive else forbidden.casefold()
        if needle in comparable:
            failures.append(f"response text contains forbidden value {forbidden!r}")
    if expectation.equals is not None:
        expected = expectation.equals
        if not expectation.case_sensitive:
            expected = expected.casefold()
        if comparable != expected:
            failures.append("response text is not equal to the expected value")
    flags = 0 if expectation.case_sensitive else regex.IGNORECASE
    for pattern in expectation.matches:
        try:
            matched = regex.search(pattern, actual, flags, timeout=REGEX_TIMEOUT_SECONDS)
        except TimeoutError:
            failures.append(f"regular expression /{pattern}/ timed out")
        else:
            if matched is None:
                failures.append(f"response text does not match /{pattern}/")
    return failures


def _normalize_state(state: str) -> str:
    return state.strip().casefold().replace("-", "_").replace(" ", "_")


def _evaluate_states(
    expectation: StateSequenceExpectation,
    states: tuple[str, ...],
) -> list[str]:
    actual = [_normalize_state(state) for state in states]
    if expectation.equals is not None:
        expected = [_normalize_state(state) for state in expectation.equals]
        if actual != expected:
            return [f"expected state sequence {expected!r}, got {actual!r}"]
        return []
    expected = [_normalize_state(state) for state in expectation.contains_in_order or []]
    pending = iter(actual)
    if all(any(state == item for state in pending) for item in expected):
        return []
    return [f"expected state sequence to contain {expected!r} in order, got {actual!r}"]


def _evaluate_files(
    expectations: list[FileExpectation],
    parts: tuple[FilePartResult, ...],
) -> list[str]:
    failures: list[str] = []
    for expectation in expectations:
        matched = [part for part in parts if _matches_file(expectation, part)]
        if len(matched) != expectation.count:
            failures.append(
                f"expected {expectation.count} file part(s) matching "
                f"{_file_location(expectation)}, got {len(matched)}"
            )
    return failures


def _matches_file(expectation: FileExpectation, part: FilePartResult) -> bool:
    return (
        (expectation.source is None or part.source == expectation.source)
        and (expectation.artifact_name is None or part.artifact_name == expectation.artifact_name)
        and (expectation.filename is None or part.filename == expectation.filename)
        and (expectation.kind is None or part.kind == expectation.kind)
        and (
            expectation.media_type is None
            or (
                part.media_type is not None
                and part.media_type.casefold() == expectation.media_type.casefold()
            )
        )
    )


def _file_location(expectation: FileExpectation) -> str:
    filters = [
        f"{name.replace('_', ' ')} {value!r}"
        for name, value in (
            ("source", expectation.source),
            ("artifact_name", expectation.artifact_name),
            ("filename", expectation.filename),
            ("media_type", expectation.media_type),
            ("kind", expectation.kind),
        )
        if value is not None
    ]
    return ", ".join(filters) or "all file parts"


def _evaluate_data(
    expectations: list[DataExpectation],
    parts: tuple[DataPartResult, ...],
) -> list[str]:
    failures: list[str] = []
    for expectation in expectations:
        candidates = [part for part in parts if _matches_location(expectation, part)]
        failure = _evaluate_data_expectation(expectation, candidates)
        if failure is not None:
            failures.append(failure)
    return failures


def _evaluate_data_expectation(
    expectation: DataExpectation,
    candidates: list[DataPartResult],
) -> str | None:
    if not candidates:
        return f"no structured data matched {_location(expectation)}"
    values = [_resolve_pointer(part.value, expectation.path) for part in candidates]
    location = expectation.path or "<root>"
    present = [value for value in values if not isinstance(value, _Missing)]
    actual = ", ".join(_json_preview(value) for value in present[:3])

    if expectation.exists is not None:
        return _existence_failure(expectation.exists, location, present, actual)
    if not present:
        return f"structured data path {location!r} was not found"
    return _data_predicate_failure(expectation, location, present, actual)


def _existence_failure(
    expected: bool,
    location: str,
    values: list[JsonValue],
    actual: str,
) -> str | None:
    if bool(values) == expected:
        return None
    if expected:
        return f"structured data path {location!r} was not found"
    return f"expected structured data path {location!r} to be absent, got {actual}"


def _data_predicate_failure(
    expectation: DataExpectation,
    location: str,
    values: list[JsonValue],
    actual: str,
) -> str | None:
    if "equals" in expectation.model_fields_set:
        if any(_json_equal(value, expectation.equals) for value in values):
            return None
        return (
            f"expected structured data at {location!r} to equal "
            f"{_json_preview(expectation.equals)}, got {actual}"
        )
    if expectation.matches is not None:
        return _data_pattern_failure(expectation.matches, location, values, actual)
    if expectation.json_schema is not None:
        schema_error = _json_schema_error(expectation.json_schema, values)
        return (
            None
            if schema_error is None
            else f"structured data at {location!r} does not match JSON Schema: {schema_error}"
        )
    if any(_matches_numeric_bounds(expectation, value) for value in values):
        return None
    return (
        f"expected structured data at {location!r} to be "
        f"{_numeric_bounds(expectation)}, got {actual}"
    )


def _data_pattern_failure(
    pattern: str,
    location: str,
    values: list[JsonValue],
    actual: str,
) -> str | None:
    result = _matches_pattern(pattern, values)
    if result is True:
        return None
    if result is None:
        return f"regular expression /{pattern}/ timed out"
    return f"expected structured data at {location!r} to match /{pattern}/, got {actual}"


def _matches_pattern(pattern: str, values: list[JsonValue]) -> bool | None:
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            if regex.search(pattern, value, timeout=REGEX_TIMEOUT_SECONDS) is not None:
                return True
        except TimeoutError:
            return None
    return False


def _json_schema_error(
    schema: dict[str, JsonValue] | bool,
    values: list[JsonValue],
) -> str | None:
    validator = Draft202012Validator(schema)
    first, *remaining = values
    first_error = best_match(validator.iter_errors(first))
    if first_error is None:
        return None
    if any(best_match(validator.iter_errors(value)) is None for value in remaining):
        return None
    return _bounded_text(first_error.message)


def _matches_numeric_bounds(expectation: DataExpectation, value: JsonValue) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return (
        (expectation.gt is None or value > expectation.gt)
        and (expectation.gte is None or value >= expectation.gte)
        and (expectation.lt is None or value < expectation.lt)
        and (expectation.lte is None or value <= expectation.lte)
    )


def _numeric_bounds(expectation: DataExpectation) -> str:
    bounds = (
        (">", expectation.gt),
        (">=", expectation.gte),
        ("<", expectation.lt),
        ("<=", expectation.lte),
    )
    return " and ".join(f"{operator} {value:g}" for operator, value in bounds if value is not None)


def _matches_location(expectation: DataExpectation, part: DataPartResult) -> bool:
    if expectation.source is not None and part.source != expectation.source:
        return False
    if expectation.artifact_name is not None and part.artifact_name != expectation.artifact_name:
        return False
    return expectation.media_type is None or (
        part.media_type is not None
        and part.media_type.casefold() == expectation.media_type.casefold()
    )


def _location(expectation: DataExpectation) -> str:
    filters: list[str] = []
    if expectation.source is not None:
        filters.append(f"source {expectation.source!r}")
    if expectation.artifact_name is not None:
        filters.append(f"artifact {expectation.artifact_name!r}")
    if expectation.media_type is not None:
        filters.append(f"media type {expectation.media_type!r}")
    return ", ".join(filters) or "the expectation"


def _resolve_pointer(value: JsonValue, pointer: str) -> JsonValue | _Missing:
    current: JsonValue = value
    for encoded in pointer.split("/")[1:]:
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
            continue
        if isinstance(current, list) and (
            token == "0" or (token.isdigit() and not token.startswith("0"))
        ):
            index = int(token)
            if index < len(current):
                current = current[index]
                continue
        return _MISSING
    return current


def _json_equal(actual: JsonValue, expected: JsonValue) -> bool:
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _json_equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _json_equal(left, right) for left, right in zip(actual, expected, strict=True)
        )
    if (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
    ):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def _json_preview(value: JsonValue) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _bounded_text(rendered)


def _bounded_text(value: str) -> str:
    return value if len(value) <= JSON_PREVIEW_CHARS else f"{value[:JSON_PREVIEW_CHARS]}…"
