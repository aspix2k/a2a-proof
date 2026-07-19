from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest

from a2a_proof.models import DataPartResult, ProofConfig
from a2a_proof.protocol import TurnOutcome
from a2a_proof.runner import _format_error, run_with_sender


def _config(scenarios: list[dict[str, object]]) -> ProofConfig:
    return ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": scenarios,
        }
    )


def _outcomes(*values: TurnOutcome) -> Iterator[TurnOutcome]:
    return iter(values)


@pytest.mark.asyncio
async def test_runs_multi_turn_scenario_with_task_continuation() -> None:
    values = _outcomes(
        TurnOutcome(
            state="input_required",
            text="Which city?",
            task_id="task",
            context_id="server-context",
            duration_ms=10,
        ),
        TurnOutcome(
            state="completed",
            text="Sunny",
            task_id="task",
            context_id="server-context",
            duration_ms=20,
        ),
    )
    calls: list[dict[str, object]] = []

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        calls.append({"message": message, **context})
        return next(values)

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "weather",
                    "turns": [
                        {
                            "message": "Weather?",
                            "expect": {"state": "input_required", "text": {"contains": "city"}},
                        },
                        {
                            "message": "Moscow",
                            "expect": {"state": "completed", "text": {"contains": "Sunny"}},
                        },
                    ],
                }
            ]
        ),
        send_turn,
    )

    assert result.passed
    assert calls[0]["message"] == "Weather?"
    assert calls[0]["task_id"] is None
    UUID(str(calls[0]["context_id"]))
    assert calls[1] == {
        "message": "Moscow",
        "data": [],
        "context_id": "server-context",
        "task_id": "task",
    }


@pytest.mark.asyncio
async def test_stops_trial_after_failed_turn() -> None:
    calls = 0

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        nonlocal calls
        calls += 1
        return TurnOutcome(
            state="completed",
            text="wrong",
            task_id=None,
            context_id=str(context["context_id"]),
            duration_ms=1,
        )

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "two turns",
                    "turns": [
                        {"message": "one", "expect": {"text": {"equals": "right"}}},
                        {"message": "two"},
                    ],
                }
            ]
        ),
        send_turn,
    )

    assert not result.passed
    assert calls == 1
    turn = result.scenarios[0].trials[0].turns[0]
    assert turn.index == 1
    assert turn.failures == ["response text is not equal to the expected value"]


@pytest.mark.asyncio
async def test_applies_trials_and_pass_rate() -> None:
    responses = iter(["yes", "no", "yes"])

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome(
            state="completed",
            text=next(responses),
            task_id=None,
            context_id=str(context["context_id"]),
            duration_ms=1,
        )

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "nondeterministic",
                    "message": "answer",
                    "expect": {"text": {"equals": "yes"}},
                    "trials": 3,
                    "pass_rate": 0.66,
                }
            ]
        ),
        send_turn,
    )

    scenario = result.scenarios[0]
    assert result.passed
    assert scenario.passed_trials == 2
    assert scenario.required_trials == 2


@pytest.mark.asyncio
async def test_converts_sender_exception_to_trial_error() -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        raise RuntimeError("connection closed")

    result = await run_with_sender(
        _config([{"name": "broken", "message": "hello"}]),
        send_turn,
    )

    trial = result.scenarios[0].trials[0]
    assert not result.passed
    assert trial.error == "RuntimeError: connection closed"


@pytest.mark.asyncio
async def test_continues_auth_required_task() -> None:
    values = _outcomes(
        TurnOutcome("auth_required", "Sign in", "task", "server-context", 1),
        TurnOutcome("completed", "Welcome", "task", "server-context", 1),
    )
    calls: list[dict[str, object]] = []

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        calls.append(context)
        return next(values)

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "authentication",
                    "turns": [
                        {"message": "Start", "expect": {"state": "auth_required"}},
                        {"message": "Signed in", "expect": {"state": "completed"}},
                    ],
                }
            ]
        ),
        send_turn,
    )

    assert result.passed
    assert calls[1] == {"data": [], "context_id": "server-context", "task_id": "task"}


@pytest.mark.asyncio
async def test_records_suite_and_trial_durations(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter([10.0, 10.5, 11.5, 12.0])
    monkeypatch.setattr("a2a_proof.runner.perf_counter", lambda: next(timestamps))

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    result = await run_with_sender(
        _config([{"name": "timed", "message": "hello"}]),
        send_turn,
    )

    assert result.duration_ms == 2_000
    assert result.scenarios[0].trials[0].duration_ms == 1_000


@pytest.mark.asyncio
async def test_preserves_completed_turns_and_duration_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([10.0, 10.5, 11.5, 12.5])
    monkeypatch.setattr("a2a_proof.runner.perf_counter", lambda: next(timestamps))
    calls = 0

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("connection closed")
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "partial",
                    "turns": [{"message": "one"}, {"message": "two"}],
                }
            ]
        ),
        send_turn,
    )

    trial = result.scenarios[0].trials[0]
    assert result.duration_ms == 2_500
    assert trial.duration_ms == 1_000
    assert [turn.text for turn in trial.turns] == ["ok"]


def test_formats_empty_exception_without_separator() -> None:
    assert _format_error(RuntimeError()) == "RuntimeError"


@pytest.mark.asyncio
async def test_preserves_structured_data_in_turn_results() -> None:
    part = DataPartResult(
        source="artifact",
        value={"city": "Paris"},
        artifact_id="result",
        artifact_name="forecast",
    )

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome(
            "completed",
            "",
            "task",
            str(context["context_id"]),
            1,
            (part,),
            first_event_ms=2,
        )

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "forecast",
                    "message": "Weather?",
                    "expect": {"data": {"path": "/city", "equals": "Paris"}},
                }
            ]
        ),
        send_turn,
    )

    assert result.passed
    turn = result.scenarios[0].trials[0].turns[0]
    assert turn.data == [part]
    assert turn.first_event_ms == 2


@pytest.mark.asyncio
async def test_sends_structured_input_without_text() -> None:
    calls: list[dict[str, object]] = []

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        calls.append({"message": message, **context})
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    result = await run_with_sender(
        _config([{"name": "structured", "data": {"order_id": "order-42"}}]),
        send_turn,
    )

    assert result.passed
    assert calls[0]["message"] is None
    assert calls[0]["data"] == [{"order_id": "order-42"}]
