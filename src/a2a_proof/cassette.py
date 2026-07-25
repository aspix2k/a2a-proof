from __future__ import annotations

import json
import os
import tempfile
from base64 import b64decode, b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from a2a.types import AgentCard
from pydantic import ValidationError

from a2a_proof.models import DataPartResult, FilePartResult, ProofConfig, StrictModel
from a2a_proof.protocol import TurnOutcome

CASSETTE_VERSION = 1
MAX_CASSETTE_BYTES = 50_000_000
Operation = Callable[..., Awaitable[TurnOutcome]]


class CassetteError(ValueError):
    pass


@dataclass(slots=True)
class Recorder:
    turns: list[dict[str, Any]] = field(default_factory=list)
    card: AgentCard | None = None

    def wrap(self, operation: Operation) -> Operation:
        async def record(*args: object, **kwargs: object) -> TurnOutcome:
            outcome = await operation(*args, **kwargs)
            self.turns.append(_outcome_dict(outcome))
            return outcome

        return record


@dataclass(slots=True)
class Cassette:
    turns: list[TurnOutcome]
    contract_sha256: str | None
    agent_url: str
    card: AgentCard | None = None
    _position: int = 0

    def sender(self) -> Operation:
        async def replay(*args: object, **kwargs: object) -> TurnOutcome:
            return self.next_turn()

        return replay

    def next_turn(self) -> TurnOutcome:
        if self._position >= len(self.turns):
            raise CassetteError(
                f"cassette holds {len(self.turns)} recorded turns, and the contract asked for more"
            )
        outcome = self.turns[self._position]
        self._position += 1
        return outcome


def write_cassette(path: Path, config: ProofConfig, recorder: Recorder) -> None:
    if path.exists():
        raise CassetteError(f"cassette path already exists: {path}")
    if not path.parent.is_dir():
        raise CassetteError(f"cassette parent directory does not exist: {path.parent}")
    document = {
        "cassette_version": CASSETTE_VERSION,
        "agent_url": str(config.agent.url),
        "contract_sha256": config.contract_sha256,
        "agent_card": (
            b64encode(recorder.card.SerializeToString(deterministic=True)).decode("ascii")
            if recorder.card is not None
            else None
        ),
        "turns": recorder.turns,
    }
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise CassetteError(f"cannot write {path}: {error.strerror or error}") from error


def load_cassette(path: Path) -> Cassette:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_CASSETTE_BYTES + 1)
    except OSError as error:
        raise CassetteError(f"cannot read {path}: {error.strerror or error}") from error
    if len(content) > MAX_CASSETTE_BYTES:
        raise CassetteError(f"cassette exceeds {MAX_CASSETTE_BYTES} bytes")
    try:
        document = CassetteDocument.model_validate_json(content)
    except (UnicodeError, ValidationError, ValueError) as error:
        raise CassetteError(f"cannot parse {path}: {error}") from error
    return Cassette(
        turns=[turn.outcome() for turn in document.turns],
        contract_sha256=document.contract_sha256,
        agent_url=document.agent_url,
        card=_card(document.agent_card),
    )


def _card(encoded: str | None) -> AgentCard | None:
    if encoded is None:
        return None
    try:
        return AgentCard.FromString(b64decode(encoded, validate=True))
    except (ValueError, TypeError) as error:
        raise CassetteError(f"invalid cassette agent_card: {error}") from error


def _outcome_dict(outcome: TurnOutcome) -> dict[str, Any]:
    return {
        "state": outcome.state,
        "text": outcome.text,
        "task_id": outcome.task_id,
        "context_id": outcome.context_id,
        "duration_ms": outcome.duration_ms,
        "first_event_ms": outcome.first_event_ms,
        "states": list(outcome.states),
        "data": [part.model_dump() for part in outcome.data],
        "files": [_file_dict(part) for part in outcome.files],
    }


def _file_dict(part: FilePartResult) -> dict[str, Any]:
    return {**part.model_dump(), "sha256": part.sha256}


class RecordedTurn(StrictModel):
    state: str
    text: str
    task_id: str | None = None
    context_id: str | None = None
    duration_ms: int
    first_event_ms: int | None = None
    states: tuple[str, ...] = ()
    data: tuple[DataPartResult, ...] = ()
    files: tuple[FilePartResult, ...] = ()

    def outcome(self) -> TurnOutcome:
        return TurnOutcome(
            state=self.state,
            text=self.text,
            task_id=self.task_id,
            context_id=self.context_id,
            duration_ms=self.duration_ms,
            first_event_ms=self.first_event_ms,
            states=self.states,
            data=self.data,
            files=self.files,
        )


class CassetteDocument(StrictModel):
    cassette_version: Literal[1]
    agent_url: str
    contract_sha256: str | None = None
    agent_card: str | None = None
    turns: list[RecordedTurn]
