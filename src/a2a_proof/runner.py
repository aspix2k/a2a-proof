from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from time import perf_counter
from uuid import uuid4

from a2a.types import AgentCard

from a2a_proof.a2a import A2ASession
from a2a_proof.ap2 import ensure_ap2_sdk, redact_ap2
from a2a_proof.assertions import evaluate, evaluate_card, evaluate_invariants
from a2a_proof.config import resolve_invariant_secrets
from a2a_proof.evidence import agent_card_sha256
from a2a_proof.files import prepare_files
from a2a_proof.models import (
    CardResult,
    LatencyExpectation,
    LatencyResult,
    ProofConfig,
    Scenario,
    ScenarioResult,
    SuiteResult,
    TrialResult,
    Turn,
    TurnResult,
)
from a2a_proof.protocol import TurnOutcome
from a2a_proof.push import PushReceiver, PushSubscription

SendTurn = Callable[..., Awaitable[TurnOutcome]]
TaskAction = Callable[..., Awaitable[TurnOutcome]]
MAX_PARALLEL_TRIALS = 32
REDACTED_RESPONSE = "[REDACTED: global invariant]"


async def run(
    config: ProofConfig,
    *,
    environ: Mapping[str, str] | None = None,
    max_parallel_trials: int = 1,
) -> SuiteResult:
    _validate_parallel_trials(max_parallel_trials)
    ensure_ap2_sdk(config)
    invariant_secrets = resolve_invariant_secrets(config, environ)
    async with AsyncExitStack() as stack:
        session = await stack.enter_async_context(await A2ASession.connect(config.agent))
        push_receiver = (
            await stack.enter_async_context(PushReceiver(config.push_notifications))
            if config.push_notifications is not None and config.uses_push_notifications
            else None
        )
        return await run_with_sender(
            config,
            session.send_turn,
            cancel_task=session.cancel_task,
            get_task=session.get_task,
            card=session.card,
            invariant_secrets=invariant_secrets,
            max_parallel_trials=max_parallel_trials,
            push_receiver=push_receiver,
        )


async def run_with_sender(
    config: ProofConfig,
    send_turn: SendTurn,
    *,
    cancel_task: TaskAction | None = None,
    get_task: TaskAction | None = None,
    card: AgentCard | None = None,
    invariant_secrets: Mapping[str, str] | None = None,
    max_parallel_trials: int = 1,
    push_receiver: PushReceiver | None = None,
) -> SuiteResult:
    _validate_parallel_trials(max_parallel_trials)
    ensure_ap2_sdk(config)
    started = perf_counter()
    card_result: CardResult | None = None
    if config.card is not None:
        if card is None:
            raise ValueError("Agent Card is required for configured card assertions")
        failures = evaluate_card(config.card, card)
        card_result = CardResult(passed=not failures, failures=failures)
    scenarios = []
    if card_result is None or card_result.passed:
        secrets = (
            resolve_invariant_secrets(config) if invariant_secrets is None else invariant_secrets
        )
        scenarios = [
            await _run_scenario(
                scenario,
                send_turn,
                cancel_task,
                get_task,
                config,
                secrets,
                max_parallel_trials,
                push_receiver,
            )
            for scenario in config.resolved_scenarios()
        ]
    return SuiteResult(
        passed=(card_result is None or card_result.passed)
        and all(scenario.passed for scenario in scenarios),
        duration_ms=round((perf_counter() - started) * 1_000),
        card=card_result,
        scenarios=scenarios,
        agent_card_sha256=agent_card_sha256(card) if card is not None else None,
    )


async def _run_scenario(
    scenario: Scenario,
    send_turn: SendTurn,
    cancel_task: TaskAction | None,
    get_task: TaskAction | None,
    config: ProofConfig,
    invariant_secrets: Mapping[str, str],
    max_parallel_trials: int,
    push_receiver: PushReceiver | None,
) -> ScenarioResult:
    if max_parallel_trials == 1:
        trials = [
            await _run_trial(
                index,
                scenario,
                send_turn,
                cancel_task,
                get_task,
                config,
                invariant_secrets,
                push_receiver,
            )
            for index in range(1, scenario.trials + 1)
        ]
    else:
        semaphore = asyncio.Semaphore(max_parallel_trials)

        async def run_trial(index: int) -> TrialResult:
            async with semaphore:
                return await _run_trial(
                    index,
                    scenario,
                    send_turn,
                    cancel_task,
                    get_task,
                    config,
                    invariant_secrets,
                    push_receiver,
                )

        trials = list(
            await asyncio.gather(*(run_trial(index) for index in range(1, scenario.trials + 1)))
        )
    passed_trials = sum(trial.passed for trial in trials)
    required_trials = math.ceil(scenario.trials * scenario.pass_rate)
    latency = _evaluate_latency(scenario.latency, trials) if scenario.latency is not None else None
    return ScenarioResult(
        name=scenario.name,
        passed=passed_trials >= required_trials and (latency is None or latency.passed),
        passed_trials=passed_trials,
        required_trials=required_trials,
        trials=trials,
        latency=latency,
    )


async def _run_trial(
    index: int,
    scenario: Scenario,
    send_turn: SendTurn,
    cancel_task: TaskAction | None,
    get_task: TaskAction | None,
    config: ProofConfig,
    invariant_secrets: Mapping[str, str],
    push_receiver: PushReceiver | None,
) -> TrialResult:
    started = perf_counter()
    context_id = str(uuid4())
    task_id: str | None = None
    results: list[TurnResult] = []
    push_subscription: PushSubscription | None = None

    try:
        turns = scenario.resolved_turns()
        for turn_index, turn in enumerate(turns, start=1):
            outcome, push_subscription = await _execute_turn(
                turn,
                send_turn,
                cancel_task,
                get_task,
                push_receiver,
                push_subscription,
                config,
                context_id,
                task_id,
            )
            failures = evaluate(turn.expect, outcome, contract_dir=config.contract_dir)
            invariant_failures: list[str] = []
            if config.invariants is not None:
                invariant_failures = evaluate_invariants(
                    config.invariants,
                    outcome,
                    invariant_secrets,
                )
                failures.extend(invariant_failures)
            redact_response = bool(invariant_failures)
            reported_data = redact_ap2(turn.expect.ap2, outcome.data)
            results.append(
                TurnResult(
                    index=turn_index,
                    passed=not failures,
                    state=outcome.state,
                    states=list(outcome.states),
                    duration_ms=outcome.duration_ms,
                    first_event_ms=outcome.first_event_ms,
                    text=REDACTED_RESPONSE if redact_response else outcome.text,
                    data=[] if redact_response else list(reported_data),
                    files=[] if redact_response else list(outcome.files),
                    response_redacted=redact_response,
                    failures=failures,
                )
            )
            if failures:
                break
            context_id = outcome.context_id or context_id
            next_turn = turns[turn_index] if turn_index < len(turns) else None
            keep_task = outcome.state in {"auth_required", "input_required"} or (
                next_turn is not None and next_turn.action is not None
            )
            task_id = (outcome.task_id or task_id) if keep_task else None
    except Exception as error:
        return TrialResult(
            index=index,
            passed=False,
            duration_ms=round((perf_counter() - started) * 1_000),
            turns=results,
            error=_format_error(error),
        )
    finally:
        if push_subscription is not None:
            push_subscription.close()

    return TrialResult(
        index=index,
        passed=all(result.passed for result in results),
        duration_ms=round((perf_counter() - started) * 1_000),
        turns=results,
    )


async def _execute_turn(
    turn: Turn,
    send_turn: SendTurn,
    cancel_task: TaskAction | None,
    get_task: TaskAction | None,
    push_receiver: PushReceiver | None,
    push_subscription: PushSubscription | None,
    config: ProofConfig,
    context_id: str,
    task_id: str | None,
) -> tuple[TurnOutcome, PushSubscription | None]:
    if turn.action is not None:
        return await _execute_action(
            turn,
            cancel_task,
            get_task,
            push_subscription,
            config,
            context_id,
            task_id,
        )
    return await _execute_input(
        turn,
        send_turn,
        push_receiver,
        push_subscription,
        config,
        context_id,
        task_id,
    )


async def _execute_input(
    turn: Turn,
    send_turn: SendTurn,
    push_receiver: PushReceiver | None,
    push_subscription: PushSubscription | None,
    config: ProofConfig,
    context_id: str,
    task_id: str | None,
) -> tuple[TurnOutcome, PushSubscription | None]:
    registered_subscription: PushSubscription | None = None
    if turn.push_notification:
        if push_receiver is None:
            raise ValueError("push receiver is not available")
        registered_subscription = push_receiver.register()
        push_subscription = registered_subscription
    try:
        arguments = {
            "data": turn.data,
            "files": prepare_files(turn.files, config.contract_dir),
            "context_id": context_id,
            "task_id": task_id,
        }
        if turn.return_immediately:
            arguments["return_immediately"] = True
        if push_subscription is not None:
            arguments["push_notification"] = push_subscription.target
        outcome = await send_turn(turn.message, **arguments)
        if registered_subscription is not None:
            if outcome.task_id is None:
                raise ValueError("push-enabled turn did not return a task ID")
            registered_subscription.bind(
                task_id=outcome.task_id,
                context_id=outcome.context_id or context_id,
            )
        return outcome, push_subscription
    except BaseException:
        if registered_subscription is not None:
            registered_subscription.close()
        raise


async def _execute_action(
    turn: Turn,
    cancel_task: TaskAction | None,
    get_task: TaskAction | None,
    push_subscription: PushSubscription | None,
    config: ProofConfig,
    context_id: str,
    task_id: str | None,
) -> tuple[TurnOutcome, PushSubscription | None]:
    if task_id is None:
        raise ValueError(f"action {turn.action!r} requires a task from a prior turn")
    if turn.action == "await_push":
        if push_subscription is None:
            raise ValueError("action 'await_push' has no active push subscription")
        outcome = await push_subscription.wait(turn.timeout_seconds or config.agent.timeout)
        push_subscription.close()
        return outcome, None
    operation = cancel_task if turn.action == "cancel" else get_task
    if operation is None:
        raise ValueError(f"action {turn.action!r} is not available")
    arguments: dict[str, object] = {"task_id": task_id, "context_id": context_id}
    if turn.action == "get_task":
        arguments["history_length"] = turn.history_length
    return await operation(**arguments), push_subscription


def _format_error(error: Exception) -> str:
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _evaluate_latency(
    expectation: LatencyExpectation,
    trials: list[TrialResult],
) -> LatencyResult:
    durations = [trial.duration_ms for trial in trials if trial.error is None]
    if not durations:
        return LatencyResult(
            passed=False,
            samples=0,
            failures=["cannot evaluate latency because every trial errored"],
        )
    p50_ms = _percentile(durations, 0.50)
    p95_ms = _percentile(durations, 0.95)
    failures: list[str] = []
    for label, maximum, actual in (
        ("p50", expectation.p50_seconds, p50_ms),
        ("p95", expectation.p95_seconds, p95_ms),
    ):
        if maximum is not None and actual > maximum * 1_000:
            failures.append(
                f"expected {label} trial latency at most {maximum:g}s, got {actual / 1_000:.3f}s"
            )
    return LatencyResult(
        passed=not failures,
        samples=len(durations),
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        failures=failures,
    )


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(interpolated)


def _validate_parallel_trials(value: int) -> None:
    if not 1 <= value <= MAX_PARALLEL_TRIALS:
        raise ValueError(f"max_parallel_trials must be between 1 and {MAX_PARALLEL_TRIALS}")
