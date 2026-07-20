from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

import a2a_proof.runner as runner_module
from a2a_proof.files import PreparedFile
from a2a_proof.models import (
    DataPartResult,
    FilePartResult,
    LatencyExpectation,
    ProofConfig,
    TrialResult,
    Turn,
)
from a2a_proof.protocol import TurnOutcome
from a2a_proof.push import PushReceiver, PushTarget
from a2a_proof.runner import (
    _evaluate_latency,
    _execute_action,
    _format_error,
    _percentile,
    run_with_sender,
)


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
async def test_passes_contract_directory_to_turn_assertions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config([{"name": "contract directory", "message": "run"}])
    config.bind_contract_dir(tmp_path)
    calls: list[Path | None] = []

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome(
            state="completed",
            text=str(message),
            task_id=None,
            context_id=str(context["context_id"]),
            duration_ms=1,
        )

    monkeypatch.setattr(
        runner_module,
        "evaluate",
        lambda _expectation, _outcome, *, contract_dir=None: calls.append(contract_dir) or [],
    )

    result = await run_with_sender(config, send_turn)

    assert result.passed
    assert calls == [tmp_path]


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
        "files": [],
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
async def test_runs_cancel_and_persistence_actions() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        calls.append(("send", {"message": message, **context}))
        return TurnOutcome("working", "started", "task", "server-context", 1)

    async def cancel_task(**context: object) -> TurnOutcome:
        calls.append(("cancel", context))
        return TurnOutcome("canceled", "", "task", "server-context", 2)

    async def get_task(**context: object) -> TurnOutcome:
        calls.append(("get_task", context))
        return TurnOutcome("canceled", "stored", "task", "server-context", 3)

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "lifecycle",
                    "turns": [
                        {
                            "message": "Start",
                            "return_immediately": True,
                            "expect": {"state": "working"},
                        },
                        {"action": "cancel", "expect": {"state": "canceled"}},
                        {
                            "action": "get_task",
                            "history_length": 5,
                            "expect": {"state": "canceled", "text": {"equals": "stored"}},
                        },
                    ],
                }
            ]
        ),
        send_turn,
        cancel_task=cancel_task,
        get_task=get_task,
    )

    assert result.passed
    assert calls[0][0] == "send"
    assert calls[0][1]["return_immediately"] is True
    assert calls[1] == (
        "cancel",
        {"task_id": "task", "context_id": "server-context"},
    )
    assert calls[2] == (
        "get_task",
        {"task_id": "task", "context_id": "server-context", "history_length": 5},
    )


@pytest.mark.asyncio
async def test_runs_task_subscription_contract() -> None:
    calls: list[dict[str, object]] = []

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome("working", "started", "task", "server-context", 1)

    async def subscribe_task(**context: object) -> TurnOutcome:
        calls.append(context)
        return TurnOutcome(
            "completed",
            "finished",
            "task",
            "server-context",
            4,
            first_event_ms=1,
            states=("working", "completed"),
        )

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "resume long task",
                    "turns": [
                        {
                            "message": "Start",
                            "return_immediately": True,
                            "expect": {"state": "working"},
                        },
                        {
                            "action": "subscribe",
                            "expect": {
                                "state": "completed",
                                "states": {"contains_in_order": ["working", "completed"]},
                                "text": {"equals": "finished"},
                            },
                        },
                    ],
                }
            ]
        ),
        send_turn,
        subscribe_task=subscribe_task,
    )

    assert result.passed
    assert calls == [{"task_id": "task", "context_id": "server-context"}]
    resumed = result.scenarios[0].trials[0].turns[1]
    assert resumed.first_event_ms == 1
    assert resumed.states == ["working", "completed"]


@pytest.mark.asyncio
async def test_runs_push_notification_behavior_contract() -> None:
    class Subscription:
        target = PushTarget(url="http://127.0.0.1/push", token="token")

        def __init__(self) -> None:
            self.bound: tuple[str, str] | None = None
            self.closed = 0

        def bind(self, *, task_id: str, context_id: str) -> None:
            self.bound = (task_id, context_id)

        async def wait(self, timeout_seconds: float) -> TurnOutcome:
            assert timeout_seconds == 5
            return TurnOutcome(
                state="completed",
                text="export ready",
                task_id="task",
                context_id="server-context",
                duration_ms=20,
                first_event_ms=10,
                states=("working", "completed"),
            )

        def close(self) -> None:
            self.closed += 1

    class Receiver:
        def __init__(self) -> None:
            self.subscription = Subscription()

        def register(self) -> Subscription:
            return self.subscription

    receiver = Receiver()
    calls: list[object] = []

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        calls.append(context["push_notification"])
        assert message == "Start export"
        return TurnOutcome(
            state="working",
            text="accepted",
            task_id="task",
            context_id="server-context",
            duration_ms=2,
        )

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "push_notifications": {},
            "scenarios": [
                {
                    "name": "async export",
                    "turns": [
                        {
                            "message": "Start export",
                            "return_immediately": True,
                            "push_notification": True,
                            "expect": {"state": "working"},
                        },
                        {
                            "action": "await_push",
                            "timeout_seconds": 5,
                            "expect": {
                                "state": "completed",
                                "text": {"contains": "ready"},
                            },
                        },
                    ],
                }
            ],
        }
    )

    result = await run_with_sender(
        config,
        send_turn,
        push_receiver=cast(PushReceiver, receiver),
    )

    assert result.passed
    assert calls == [receiver.subscription.target]
    assert receiver.subscription.bound == ("task", "server-context")
    assert receiver.subscription.closed == 1
    assert result.scenarios[0].trials[0].turns[1].states == ["working", "completed"]


@pytest.mark.asyncio
async def test_push_contract_requires_receiver_and_task_id() -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "push_notifications": {},
            "scenarios": [
                {
                    "name": "async job",
                    "turns": [
                        {
                            "message": "Start",
                            "return_immediately": True,
                            "push_notification": True,
                        },
                        {"action": "await_push"},
                    ],
                }
            ],
        }
    )

    async def accepted_without_task(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome("working", "accepted", None, str(context["context_id"]), 1)

    missing_receiver = await run_with_sender(config, accepted_without_task)
    assert missing_receiver.scenarios[0].trials[0].error == (
        "ValueError: push receiver is not available"
    )

    class Subscription:
        target = PushTarget(url="http://127.0.0.1/push", token="token")

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Receiver:
        def __init__(self) -> None:
            self.subscription = Subscription()

        def register(self) -> Subscription:
            return self.subscription

    receiver = Receiver()
    missing_task = await run_with_sender(
        config,
        accepted_without_task,
        push_receiver=cast(PushReceiver, receiver),
    )
    assert missing_task.scenarios[0].trials[0].error == (
        "ValueError: push-enabled turn did not return a task ID"
    )
    assert receiver.subscription.closed


@pytest.mark.asyncio
async def test_await_push_requires_active_subscription() -> None:
    config = _config([{"name": "unused", "message": "Start"}])
    with pytest.raises(ValueError, match="no active push subscription"):
        await _execute_action(
            Turn(action="await_push"),
            None,
            None,
            None,
            None,
            config,
            "context",
            "task",
        )


@pytest.mark.asyncio
async def test_task_action_requires_prior_task_id() -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome("message", "done", None, str(context["context_id"]), 1)

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "no task",
                    "turns": [{"message": "Start"}, {"action": "get_task"}],
                }
            ]
        ),
        send_turn,
    )

    assert not result.passed
    assert result.scenarios[0].trials[0].error == (
        "ValueError: action 'get_task' requires a task from a prior turn"
    )


@pytest.mark.asyncio
async def test_task_action_requires_available_operation() -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome("completed", "done", "task", str(context["context_id"]), 1)

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "unavailable",
                    "turns": [{"message": "Start"}, {"action": "cancel"}],
                }
            ]
        ),
        send_turn,
    )

    assert result.scenarios[0].trials[0].error == "ValueError: action 'cancel' is not available"


@pytest.mark.asyncio
async def test_applies_global_invariants_to_every_turn() -> None:
    responses = iter(["safe", "system prompt contains secret-value"])
    calls = 0

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        nonlocal calls
        calls += 1
        return TurnOutcome(
            state="completed",
            text=next(responses),
            task_id=None,
            context_id=str(context["context_id"]),
            duration_ms=1,
            data=(DataPartResult(source="message", value={"secret": "secret-value"}),),
            files=(FilePartResult(source="message", kind="url", filename="secret.txt"),),
        )

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "invariants": {
                "text": {
                    "not_contains": "system prompt",
                    "not_contains_env": "API_TOKEN",
                }
            },
            "scenarios": [
                {
                    "name": "conversation",
                    "turns": [{"message": "one"}, {"message": "two"}, {"message": "three"}],
                }
            ],
        }
    )

    result = await run_with_sender(
        config,
        send_turn,
        invariant_secrets={"API_TOKEN": "secret-value"},
    )

    assert not result.passed
    assert calls == 2
    turn = result.scenarios[0].trials[0].turns[1]
    failures = turn.failures
    assert failures == [
        "response text violates global not_contains invariant 1",
        "response text contains value from environment variable 'API_TOKEN'",
    ]
    assert "secret-value" not in " ".join(failures)
    assert turn.response_redacted
    assert turn.text == "[REDACTED: global invariant]"
    assert turn.data == []
    assert turn.files == []


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


def test_evaluates_interpolated_trial_latency_percentiles() -> None:
    trials = [
        TrialResult(index=index, passed=True, duration_ms=duration)
        for index, duration in enumerate([100, 200, 1_000], start=1)
    ]

    result = _evaluate_latency(
        LatencyExpectation(p50_seconds=0.3, p95_seconds=0.9),
        trials,
    )

    assert not result.passed
    assert result.samples == 3
    assert result.p50_ms == 200
    assert result.p95_ms == 920
    assert result.failures == ["expected p95 trial latency at most 0.9s, got 0.920s"]


def test_latency_threshold_is_inclusive_and_reports_p50() -> None:
    trials = [
        TrialResult(index=index, passed=True, duration_ms=duration)
        for index, duration in enumerate([100, 200, 1_000], start=1)
    ]

    passing = _evaluate_latency(
        LatencyExpectation(p50_seconds=0.2, p95_seconds=0.92),
        trials,
    )
    failing = _evaluate_latency(LatencyExpectation(p50_seconds=0.1999), trials)

    assert passing.passed
    assert passing.failures == []
    assert failing.failures == ["expected p50 trial latency at most 0.1999s, got 0.200s"]


def test_latency_requires_a_completed_trial() -> None:
    result = _evaluate_latency(
        LatencyExpectation(p95_seconds=1),
        [TrialResult(index=1, passed=False, duration_ms=5, error="failed")],
    )

    assert not result.passed
    assert result.samples == 0
    assert result.p50_ms is None
    assert result.p95_ms is None
    assert result.failures == ["cannot evaluate latency because every trial errored"]
    assert _percentile([123], 0.95) == 123


@pytest.mark.asyncio
async def test_applies_scenario_latency_contract() -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    result = await run_with_sender(
        _config(
            [
                {
                    "name": "timed",
                    "message": "hello",
                    "trials": 2,
                    "latency": {"p50_seconds": 1, "p95_seconds": 1},
                }
            ]
        ),
        send_turn,
    )

    latency = result.scenarios[0].latency
    assert result.passed
    assert latency is not None
    assert latency.passed
    assert latency.samples == 2


@pytest.mark.asyncio
async def test_runs_trials_with_bounded_parallelism_and_stable_result_order() -> None:
    active = 0
    maximum = 0

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    result = await run_with_sender(
        _config([{"name": "parallel", "message": "hello", "trials": 4}]),
        send_turn,
        max_parallel_trials=2,
    )

    assert result.passed
    assert maximum == 2
    assert [trial.index for trial in result.scenarios[0].trials] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_run_defaults_to_sequential_trials(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    maximum = 0

    class Session:
        card = AgentCard(name="Agent")

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send_turn(self, message: str | None, **context: object) -> TurnOutcome:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

        async def cancel_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

        async def get_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

        async def subscribe_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

    config = _config([{"name": "default", "message": "hello", "trials": 2}])

    async def connect(agent: object, *, trust_env: bool) -> Session:
        assert agent is config.agent
        assert trust_env is True
        return Session()

    monkeypatch.setattr(runner_module.A2ASession, "connect", connect)

    result = await runner_module.run(config)

    assert result.passed
    assert maximum == 1


@pytest.mark.asyncio
async def test_run_does_not_open_unused_push_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        card = AgentCard(name="Agent")

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send_turn(self, message: str | None, **context: object) -> TurnOutcome:
            return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

        async def cancel_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

        async def get_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

        async def subscribe_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "push_notifications": {},
            "scenarios": [{"name": "ordinary", "message": "hello"}],
        }
    )

    async def connect(agent: object, *, trust_env: bool) -> Session:
        assert agent is config.agent
        assert trust_env is True
        return Session()

    def unexpected_receiver(config: object) -> None:
        raise AssertionError("unused push receiver was opened")

    monkeypatch.setattr(runner_module.A2ASession, "connect", connect)
    monkeypatch.setattr(runner_module, "PushReceiver", unexpected_receiver)

    assert (await runner_module.run(config)).passed


@pytest.mark.asyncio
async def test_run_wires_card_lifecycle_and_parallelism(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    maximum = 0
    cancel_calls = 0
    get_calls = 0

    class Session:
        card = AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=False))

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send_turn(self, message: str | None, **context: object) -> TurnOutcome:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return TurnOutcome("working", "started", "task", str(context["context_id"]), 1)

        async def cancel_task(self, **context: object) -> TurnOutcome:
            nonlocal cancel_calls
            cancel_calls += 1
            return TurnOutcome("canceled", "", "task", str(context["context_id"]), 1)

        async def get_task(self, **context: object) -> TurnOutcome:
            nonlocal get_calls
            get_calls += 1
            return TurnOutcome("canceled", "stored", "task", str(context["context_id"]), 1)

        async def subscribe_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "card": {"capabilities": {"streaming": False}},
            "scenarios": [
                {
                    "name": "lifecycle",
                    "trials": 2,
                    "turns": [
                        {
                            "message": "Start",
                            "return_immediately": True,
                            "expect": {"state": "working"},
                        },
                        {"action": "cancel", "expect": {"state": "canceled"}},
                        {"action": "get_task", "expect": {"state": "canceled"}},
                    ],
                }
            ],
        }
    )

    async def connect(agent: object, *, trust_env: bool) -> Session:
        assert agent is config.agent
        assert trust_env is True
        return Session()

    monkeypatch.setattr(runner_module.A2ASession, "connect", connect)

    result = await runner_module.run(config, max_parallel_trials=2)

    assert result.passed
    assert result.card is not None
    assert maximum == 2
    assert cancel_calls == 2
    assert get_calls == 2


@pytest.mark.asyncio
async def test_run_wires_task_subscription_for_all_trial_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscription_calls = 0

    class Session:
        card = AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True))

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send_turn(self, message: str | None, **context: object) -> TurnOutcome:
            return TurnOutcome("working", "started", "task", str(context["context_id"]), 1)

        async def cancel_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

        async def get_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

        async def subscribe_task(self, **context: object) -> TurnOutcome:
            nonlocal subscription_calls
            subscription_calls += 1
            return TurnOutcome(
                "completed",
                "finished",
                "task",
                str(context["context_id"]),
                1,
                states=("working", "completed"),
            )

    config = _config(
        [
            {
                "name": "resume",
                "trials": 2,
                "turns": [
                    {
                        "message": "Start",
                        "return_immediately": True,
                        "expect": {"state": "working"},
                    },
                    {"action": "subscribe", "expect": {"state": "completed"}},
                ],
            }
        ]
    )

    async def connect(agent: object, *, trust_env: bool) -> Session:
        assert agent is config.agent
        assert trust_env is True
        return Session()

    monkeypatch.setattr(runner_module.A2ASession, "connect", connect)

    for jobs in (1, 2):
        assert (await runner_module.run(config, max_parallel_trials=jobs)).passed
    assert subscription_calls == 4


@pytest.mark.asyncio
async def test_run_uses_explicit_environment_for_invariant_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        card = AgentCard(name="Agent")

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send_turn(self, message: str | None, **context: object) -> TurnOutcome:
            return TurnOutcome("completed", "runtime-secret", None, str(context["context_id"]), 1)

        async def cancel_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

        async def get_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

        async def subscribe_task(self, **context: object) -> TurnOutcome:
            raise AssertionError("not called")

    async def connect(agent: object, *, trust_env: bool) -> Session:
        assert trust_env is True
        return Session()

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "invariants": {"text": {"not_contains_env": "API_TOKEN"}},
            "scenarios": [{"name": "secret", "message": "Hello"}],
        }
    )
    monkeypatch.setattr(runner_module.A2ASession, "connect", connect)

    result = await runner_module.run(config, environ={"API_TOKEN": "runtime-secret"})

    assert not result.passed
    turn = result.scenarios[0].trials[0].turns[0]
    assert turn.response_redacted
    assert turn.failures == ["response text contains value from environment variable 'API_TOKEN'"]


@pytest.mark.asyncio
async def test_default_trial_execution_is_sequential() -> None:
    active = 0
    maximum = 0

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    result = await run_with_sender(
        _config([{"name": "sequential", "message": "hello", "trials": 2}]),
        send_turn,
    )

    assert result.passed
    assert maximum == 1


@pytest.mark.asyncio
async def test_parallel_trials_receive_invariant_secrets() -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        await asyncio.sleep(0)
        return TurnOutcome("completed", "secret", None, str(context["context_id"]), 1)

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "invariants": {"text": {"not_contains_env": "API_TOKEN"}},
            "scenarios": [{"name": "parallel", "message": "hello", "trials": 2}],
        }
    )

    result = await run_with_sender(
        config,
        send_turn,
        invariant_secrets={"API_TOKEN": "secret"},
        max_parallel_trials=2,
    )

    assert not result.passed
    assert all(trial.turns[0].response_redacted for trial in result.scenarios[0].trials)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 33])
async def test_rejects_invalid_parallel_trial_limit(value: int) -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        raise AssertionError("sender must not run")

    with pytest.raises(ValueError, match="must be between 1 and 32"):
        await run_with_sender(
            _config([{"name": "smoke", "message": "hello"}]),
            send_turn,
            max_parallel_trials=value,
        )


@pytest.mark.asyncio
async def test_accepts_maximum_parallel_trial_limit() -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    result = await run_with_sender(
        _config([{"name": "smoke", "message": "hello"}]),
        send_turn,
        max_parallel_trials=32,
    )

    assert result.passed


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
    assert calls[1] == {
        "data": [],
        "files": [],
        "context_id": "server-context",
        "task_id": "task",
    }


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
    file = FilePartResult(
        source="artifact",
        kind="url",
        filename="forecast.json",
        media_type="application/json",
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
            states=("working", "completed"),
            files=(file,),
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
    assert turn.files == [file]
    assert turn.states == ["working", "completed"]
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


@pytest.mark.asyncio
async def test_prepares_file_inputs_for_each_turn(tmp_path: Path) -> None:
    (tmp_path / "report.txt").write_text("report", encoding="utf-8")
    calls: list[dict[str, object]] = []

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        calls.append({"message": message, **context})
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    config = _config([{"name": "file", "files": ["report.txt"]}])
    config.bind_contract_dir(tmp_path)

    result = await run_with_sender(config, send_turn)

    assert result.passed
    prepared = cast(list[PreparedFile], calls[0]["files"])
    assert prepared[0].content == b"report"
    assert prepared[0].filename == "report.txt"
    assert prepared[0].media_type == "text/plain"


@pytest.mark.asyncio
async def test_applies_defaults_to_runner_trials() -> None:
    calls = 0

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        nonlocal calls
        calls += 1
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "defaults": {"trials": 2, "pass_rate": 0.5},
            "scenarios": [{"name": "defaulted", "message": "hello"}],
        }
    )

    result = await run_with_sender(config, send_turn)

    assert result.passed
    assert calls == 2
    assert result.scenarios[0].required_trials == 1


@pytest.mark.asyncio
async def test_agent_card_assertions_run_before_scenarios() -> None:
    calls = 0

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        nonlocal calls
        calls += 1
        return TurnOutcome("completed", "ok", None, str(context["context_id"]), 1)

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "card": {
                "skills": {"contains": "summarize"},
                "capabilities": {"streaming": True},
                "input_modes": {"contains": "application/pdf"},
                "output_modes": {"contains": "text/plain"},
            },
            "scenarios": [{"name": "smoke", "message": "hello"}],
        }
    )
    card = AgentCard(
        name="Agent",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["APPLICATION/PDF"],
        default_output_modes=["TEXT/PLAIN"],
        skills=[AgentSkill(id="summarize")],
    )

    result = await run_with_sender(config, send_turn, card=card)

    assert result.passed
    assert result.card is not None
    assert result.card.passed
    assert result.agent_card_sha256 is not None
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_agent_card_preflight_skips_agent_messages() -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        raise AssertionError("scenario must not run")

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "card": {
                "skills": {"contains": ["missing"]},
                "capabilities": {
                    "streaming": True,
                    "push_notifications": True,
                    "extended_agent_card": True,
                },
                "input_modes": {"contains": "application/pdf"},
                "output_modes": {"contains": "application/json"},
            },
            "scenarios": [{"name": "smoke", "message": "hello"}],
        }
    )

    result = await run_with_sender(config, send_turn, card=AgentCard(name="Agent"))

    assert not result.passed
    assert result.scenarios == []
    assert result.card is not None
    assert result.card.failures == [
        "Agent Card does not contain skill ID 'missing'",
        "Agent Card does not contain input mode 'application/pdf'",
        "Agent Card does not contain output mode 'application/json'",
        "expected Agent Card capability 'streaming' to be True, got False",
        "expected Agent Card capability 'push notifications' to be True, got False",
        "expected Agent Card capability 'extended agent card' to be True, got False",
    ]


@pytest.mark.asyncio
async def test_requires_card_when_card_assertions_are_configured() -> None:
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        raise AssertionError("scenario must not run")

    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "card": {"skills": {"contains": "summarize"}},
            "scenarios": [{"name": "smoke", "message": "hello"}],
        }
    )

    with pytest.raises(ValueError, match="Agent Card is required") as raised:
        await run_with_sender(config, send_turn)
    assert str(raised.value) == "Agent Card is required for configured card assertions"
