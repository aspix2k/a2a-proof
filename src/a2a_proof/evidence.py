from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any

import regex
from a2a.types import AgentCard

from a2a_proof.config import resolve_invariant_secrets
from a2a_proof.models import ProofConfig, SuiteResult, TrialResult, TurnResult

MAX_EVIDENCE_TRIALS = 100
MAX_EVIDENCE_TEXT_CHARS = 100_000
MAX_EVIDENCE_DATA_CHARS = 100_000
MAX_EVIDENCE_FILE_PARTS = 100
REDACTION = "[REDACTED]"


class EvidenceError(ValueError):
    pass


def agent_card_sha256(card: AgentCard) -> str:
    return sha256(card.SerializeToString(deterministic=True)).hexdigest()


def write_evidence(
    directory: Path,
    config: ProofConfig,
    result: SuiteResult,
    environ: Mapping[str, str] | None = None,
    *,
    max_parallel_trials: int = 1,
) -> None:
    if os.path.lexists(directory):
        raise EvidenceError(f"evidence path already exists: {directory}")
    if not directory.parent.is_dir():
        raise EvidenceError(f"evidence parent directory does not exist: {directory.parent}")

    secrets = {*config.agent.headers.values(), *config.redaction_values}
    secrets.update(resolve_invariant_secrets(config, environ).values())
    redactor = _Redactor(secrets)
    failed = [
        (scenario.name, trial)
        for scenario in result.scenarios
        for trial in scenario.trials
        if not trial.passed
    ]
    selected = failed[:MAX_EVIDENCE_TRIALS]
    latency_failures = [
        (scenario.name, scenario.latency)
        for scenario in result.scenarios
        if scenario.latency is not None and not scenario.latency.passed
    ]
    records_to_write: list[dict[str, Any]] = []
    if result.card is not None and not result.card.passed:
        records_to_write.append({"kind": "agent_card", "failures": result.card.failures})
    records_to_write.extend(
        {
            "kind": "latency",
            "scenario": name,
            **latency.model_dump(),
        }
        for name, latency in latency_failures
    )
    records_to_write.extend(_trial_record(scenario, trial) for scenario, trial in selected)
    temporary = Path(tempfile.mkdtemp(prefix=f".{directory.name}.", dir=directory.parent))
    try:
        records = "".join(
            _json_line(_bound_record(redactor.redact(record))) for record in records_to_write
        )
        manifest = {
            "schema_version": 1,
            "tool": {"name": "a2a-proof", "version": version("a2a-proof")},
            "execution": {
                "scenarios": redactor.redact([scenario.name for scenario in config.scenarios]),
                "max_parallel_trials": max_parallel_trials,
                "transport": config.agent.transport,
            },
            "passed": result.passed,
            "duration_ms": result.duration_ms,
            "contract_sha256": config.contract_sha256,
            "agent_card_sha256": result.agent_card_sha256,
            "failed_trials": len(failed),
            "recorded_trials": len(selected),
            "failed_latency_contracts": len(latency_failures),
            "agent_card_failed": result.card is not None and not result.card.passed,
            "truncated": len(selected) != len(failed),
            "records": "failures.jsonl",
        }
        _write_file(temporary / "manifest.json", json.dumps(manifest, indent=2) + "\n")
        _write_file(temporary / "failures.jsonl", records)
        os.replace(temporary, directory)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


class _Redactor:
    def __init__(self, secrets: set[str]) -> None:
        self._patterns = [
            regex.compile(regex.escape(secret), regex.IGNORECASE)
            for secret in sorted((value for value in secrets if value), key=len, reverse=True)
        ]

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for pattern in self._patterns:
                value = pattern.sub(REDACTION, value, timeout=0.1)
            return value
        if isinstance(value, Mapping):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [self.redact(item) for item in value]
        return value


def _trial_record(scenario: str, trial: TrialResult) -> dict[str, Any]:
    failure_turn = next((turn for turn in reversed(trial.turns) if not turn.passed), None)
    return {
        "kind": "trial",
        "scenario": scenario,
        "trial": trial.index,
        "duration_ms": trial.duration_ms,
        "error": trial.error,
        "turns": [_turn_summary(turn) for turn in trial.turns],
        "events": _turn_events(failure_turn) if failure_turn is not None else [],
    }


def _turn_summary(turn: TurnResult) -> dict[str, Any]:
    return {
        "index": turn.index,
        "passed": turn.passed,
        "state": turn.state,
        "states": turn.states,
        "duration_ms": turn.duration_ms,
        "first_event_ms": turn.first_event_ms,
        "response_redacted": turn.response_redacted,
        "failures": turn.failures,
    }


def _turn_events(turn: TurnResult) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{"type": "state", "state": state} for state in turn.states]
    events.append(
        {
            "type": "response",
            "text": turn.text,
            "data": [part.model_dump() for part in turn.data],
            "files": [part.model_dump() for part in turn.files],
        }
    )
    return events


def _bound_record(record: dict[str, Any]) -> dict[str, Any]:
    if "failures" in record:
        record["failures"] = [_truncate_text(failure) for failure in record["failures"]]
    if record["kind"] != "trial":
        return record
    if record["error"] is not None:
        record["error"] = _truncate_text(record["error"])
    for event in record["events"]:
        if event["type"] == "response":
            event["text"] = _truncate_text(event["text"])
            event["data"] = _bounded_data(event["data"])
            event["files"] = _bounded_files(event["files"])
    return record


def _truncate_text(value: str) -> str:
    if len(value) <= MAX_EVIDENCE_TEXT_CHARS:
        return value
    return _bounded_prefix(value, MAX_EVIDENCE_TEXT_CHARS) + "…[truncated]"


def _bounded_data(value: list[dict[str, Any]]) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= MAX_EVIDENCE_DATA_CHARS:
        return value
    return {"preview": _bounded_prefix(encoded, MAX_EVIDENCE_DATA_CHARS), "truncated": True}


def _bounded_files(value: list[dict[str, Any]]) -> Any:
    if len(value) <= MAX_EVIDENCE_FILE_PARTS:
        return value
    return {
        "items": value[:MAX_EVIDENCE_FILE_PARTS],
        "total": len(value),
        "truncated": True,
    }


def _bounded_prefix(value: str, limit: int) -> str:
    prefix = value[:limit]
    marker = prefix.rfind("[")
    if marker >= 0 and value.startswith(REDACTION, marker):
        return value[:marker] + REDACTION
    return prefix


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


def _write_file(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
