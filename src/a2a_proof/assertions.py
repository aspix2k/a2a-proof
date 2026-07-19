from __future__ import annotations

import regex

from a2a_proof.models import Expectation, TextExpectation
from a2a_proof.protocol import TurnOutcome

FAILURE_STATES = {"canceled", "failed", "rejected"}
REGEX_TIMEOUT_SECONDS = 0.1


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
