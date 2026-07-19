from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import uuid4

from a2a.types import AgentCard

from a2a_proof.a2a import A2ASession
from a2a_proof.assertions import evaluate, evaluate_card
from a2a_proof.files import prepare_files
from a2a_proof.models import (
    CardResult,
    ProofConfig,
    Scenario,
    ScenarioResult,
    SuiteResult,
    TrialResult,
    TurnResult,
)
from a2a_proof.protocol import TurnOutcome

SendTurn = Callable[..., Awaitable[TurnOutcome]]


async def run(config: ProofConfig) -> SuiteResult:
    async with await A2ASession.connect(config.agent) as session:
        return await run_with_sender(config, session.send_turn, card=session.card)


async def run_with_sender(
    config: ProofConfig,
    send_turn: SendTurn,
    *,
    card: AgentCard | None = None,
) -> SuiteResult:
    started = perf_counter()
    card_result: CardResult | None = None
    if config.card is not None:
        if card is None:
            raise ValueError("Agent Card is required for configured card assertions")
        failures = evaluate_card(config.card, card)
        card_result = CardResult(passed=not failures, failures=failures)
    scenarios = []
    if card_result is None or card_result.passed:
        scenarios = [
            await _run_scenario(scenario, send_turn, config)
            for scenario in config.resolved_scenarios()
        ]
    return SuiteResult(
        passed=(card_result is None or card_result.passed)
        and all(scenario.passed for scenario in scenarios),
        duration_ms=round((perf_counter() - started) * 1_000),
        card=card_result,
        scenarios=scenarios,
    )


async def _run_scenario(
    scenario: Scenario,
    send_turn: SendTurn,
    config: ProofConfig,
) -> ScenarioResult:
    trials = [
        await _run_trial(index, scenario, send_turn, config)
        for index in range(1, scenario.trials + 1)
    ]
    passed_trials = sum(trial.passed for trial in trials)
    required_trials = math.ceil(scenario.trials * scenario.pass_rate)
    return ScenarioResult(
        name=scenario.name,
        passed=passed_trials >= required_trials,
        passed_trials=passed_trials,
        required_trials=required_trials,
        trials=trials,
    )


async def _run_trial(
    index: int,
    scenario: Scenario,
    send_turn: SendTurn,
    config: ProofConfig,
) -> TrialResult:
    started = perf_counter()
    context_id = str(uuid4())
    task_id: str | None = None
    results: list[TurnResult] = []

    try:
        for turn_index, turn in enumerate(scenario.resolved_turns(), start=1):
            outcome = await send_turn(
                turn.message,
                data=turn.data,
                files=prepare_files(turn.files, config.contract_dir),
                context_id=context_id,
                task_id=task_id,
            )
            failures = evaluate(turn.expect, outcome)
            results.append(
                TurnResult(
                    index=turn_index,
                    passed=not failures,
                    state=outcome.state,
                    states=list(outcome.states),
                    duration_ms=outcome.duration_ms,
                    first_event_ms=outcome.first_event_ms,
                    text=outcome.text,
                    data=list(outcome.data),
                    files=list(outcome.files),
                    failures=failures,
                )
            )
            if failures:
                break
            context_id = outcome.context_id or context_id
            task_id = (
                outcome.task_id if outcome.state in {"auth_required", "input_required"} else None
            )
    except Exception as error:
        return TrialResult(
            index=index,
            passed=False,
            duration_ms=round((perf_counter() - started) * 1_000),
            turns=results,
            error=_format_error(error),
        )

    return TrialResult(
        index=index,
        passed=all(result.passed for result in results),
        duration_ms=round((perf_counter() - started) * 1_000),
        turns=results,
    )


def _format_error(error: Exception) -> str:
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__
