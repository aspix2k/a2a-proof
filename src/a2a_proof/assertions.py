from __future__ import annotations

import json

import regex
from pydantic import JsonValue

from a2a_proof.models import DataExpectation, DataPartResult, Expectation, TextExpectation
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
    if expectation.text is not None:
        failures.extend(_evaluate_text(expectation.text, outcome.text))
    failures.extend(_evaluate_data(expectation.data, outcome.data))
    return failures


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


def _evaluate_data(
    expectations: list[DataExpectation],
    parts: tuple[DataPartResult, ...],
) -> list[str]:
    failures: list[str] = []
    for expectation in expectations:
        candidates = [part for part in parts if _matches_location(expectation, part)]
        if not candidates:
            failures.append(f"no structured data matched {_location(expectation)}")
            continue
        values = [_resolve_pointer(part.value, expectation.path) for part in candidates]
        if any(
            not isinstance(value, _Missing) and _json_equal(value, expectation.equals)
            for value in values
        ):
            continue
        location = expectation.path or "<root>"
        present = [value for value in values if not isinstance(value, _Missing)]
        if not present:
            failures.append(f"structured data path {location!r} was not found")
            continue
        actual = ", ".join(_json_preview(value) for value in present[:3])
        failures.append(
            f"expected structured data at {location!r} to equal "
            f"{_json_preview(expectation.equals)}, got {actual}"
        )
    return failures


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
    return rendered if len(rendered) <= JSON_PREVIEW_CHARS else f"{rendered[:JSON_PREVIEW_CHARS]}…"
