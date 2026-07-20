from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from a2a.helpers import get_artifact_text, get_data_parts, get_message_text
from a2a.types import Artifact, Message, Part, Role, StreamResponse, Task, TaskState
from pydantic import ValidationError

from a2a_proof.models import DataPartResult, FilePartResult

MAX_EVENTS = 1_000
MAX_TEXT_CHARS = 1_000_000
MAX_DATA_BYTES = 1_000_000
MAX_DATA_PARTS = 1_000
MAX_FILE_PARTS = 1_000
MAX_RAW_BYTES = 20_000_000
MAX_FILE_URL_CHARS = 10_000
INTERRUPTED_STATES = {
    "auth_required",
    "canceled",
    "completed",
    "failed",
    "input_required",
    "rejected",
}


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    state: str
    text: str
    task_id: str | None
    context_id: str | None
    duration_ms: int
    data: tuple[DataPartResult, ...] = ()
    first_event_ms: int | None = None
    states: tuple[str, ...] = ()
    files: tuple[FilePartResult, ...] = ()


@dataclass(frozen=True, slots=True)
class _CollectedContent:
    text: str
    data: tuple[DataPartResult, ...]
    files: tuple[FilePartResult, ...]
    raw_bytes: int
    artifact_name: str | None = None


class ResponseCollector:
    def __init__(
        self,
        context_id: str,
        *,
        expected_identity: tuple[str, str] | None = None,
    ) -> None:
        self._context_id = context_id
        self._expected_identity = expected_identity
        self._task_id: str | None = None
        self._state = "message"
        self._states: list[str] = []
        self._events = 0
        self._messages: dict[str, _CollectedContent] = {}
        self._artifacts: dict[str, _CollectedContent] = {}

    def add(self, response: StreamResponse) -> None:
        self._events += 1
        if self._events > MAX_EVENTS:
            raise ProtocolError(f"agent response exceeded {MAX_EVENTS} events")
        self._validate_response_identity(response)

        if response.HasField("message"):
            self._add_message(response.message)
            self._record_state("message")
        elif response.HasField("task"):
            self._add_task(response.task)
        elif response.HasField("status_update"):
            update = response.status_update
            self._record_task_id(update.task_id)
            self._record_context_id(update.context_id)
            self._record_state(_state_name(update.status.state))
            if update.status.HasField("message"):
                self._add_message(update.status.message)
        elif response.HasField("artifact_update"):
            update = response.artifact_update
            self._record_task_id(update.task_id)
            self._record_context_id(update.context_id)
            self._add_artifact(update.artifact, append=update.append)
        else:
            raise ProtocolError("agent returned an empty stream event")
        self._check_size()

    def finish(
        self,
        *,
        duration_ms: int,
        first_event_ms: int | None = None,
        require_terminal: bool = True,
    ) -> TurnOutcome:
        if self._events == 0:
            raise ProtocolError("agent returned no response")
        if require_terminal and self._state not in INTERRUPTED_STATES | {"message"}:
            raise ProtocolError(f"agent stream ended in non-terminal state {self._state!r}")
        contents = [*self._messages.values(), *self._artifacts.values()]
        text = "\n".join(content.text for content in contents if content.text)
        data = tuple(part for content in contents for part in content.data)
        files = tuple(part for content in contents for part in content.files)
        if not self._states:
            self._record_state(self._state)
        return TurnOutcome(
            state=self._state,
            text=text,
            task_id=self._task_id,
            context_id=self._context_id,
            duration_ms=duration_ms,
            data=data,
            first_event_ms=first_event_ms,
            states=tuple(self._states),
            files=files,
        )

    def _add_task(self, task: Task) -> None:
        self._record_task_id(task.id)
        self._record_context_id(task.context_id)
        self._record_state(_state_name(task.status.state))
        if task.status.HasField("message"):
            self._add_message(task.status.message)
        for message in task.history:
            self._add_message(message)
        for artifact in task.artifacts:
            self._add_artifact(artifact, append=False)

    def _add_message(self, message: Message) -> None:
        self._validate_task_id(message.task_id)
        self._validate_context_id(message.context_id)
        if message.role == Role.ROLE_USER:
            return
        key = message.message_id or f"message-{len(self._messages)}"
        self._messages[key] = _CollectedContent(
            text=get_message_text(message),
            data=_collect_data(message.parts, source="message"),
            files=_collect_files(message.parts, source="message"),
            raw_bytes=_raw_size(message.parts),
        )
        self._record_task_id(message.task_id)
        self._record_context_id(message.context_id)

    def _add_artifact(self, artifact: Artifact, *, append: bool) -> None:
        key = artifact.artifact_id or f"artifact-{len(self._artifacts)}"
        previous = self._artifacts.get(key)
        name = artifact.name or (previous.artifact_name if previous is not None else None)
        text = get_artifact_text(artifact)
        data = _collect_data(
            artifact.parts,
            source="artifact",
            artifact_id=artifact.artifact_id or None,
            artifact_name=name,
        )
        files = _collect_files(
            artifact.parts,
            source="artifact",
            artifact_id=artifact.artifact_id or None,
            artifact_name=name,
        )
        raw_bytes = _raw_size(artifact.parts)
        if append and previous is not None:
            text = previous.text + text
            data = previous.data + data
            files = previous.files + files
            raw_bytes += previous.raw_bytes
        self._artifacts[key] = _CollectedContent(
            text=text,
            data=data,
            files=files,
            raw_bytes=raw_bytes,
            artifact_name=name,
        )

    def _check_size(self) -> None:
        contents = [*self._messages.values(), *self._artifacts.values()]
        text_size = sum(len(content.text) for content in contents)
        if text_size > MAX_TEXT_CHARS:
            raise ProtocolError(f"agent response text exceeded {MAX_TEXT_CHARS} characters")
        raw_size = sum(content.raw_bytes for content in contents)
        if raw_size > MAX_RAW_BYTES:
            raise ProtocolError(f"agent response raw data exceeded {MAX_RAW_BYTES} bytes")
        data = [part for content in contents for part in content.data]
        if len(data) > MAX_DATA_PARTS:
            raise ProtocolError(f"agent response exceeded {MAX_DATA_PARTS} structured data parts")
        data_size = sum(
            len(
                json.dumps(
                    part.value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            + sum(
                len(value.encode("utf-8"))
                for value in (part.media_type, part.artifact_id, part.artifact_name)
                if value is not None
            )
            for part in data
        )
        if data_size > MAX_DATA_BYTES:
            raise ProtocolError(f"agent response structured data exceeded {MAX_DATA_BYTES} bytes")
        files = [part for content in contents for part in content.files]
        if len(files) > MAX_FILE_PARTS:
            raise ProtocolError(f"agent response exceeded {MAX_FILE_PARTS} file parts")

    def _record_state(self, state: str) -> None:
        self._state = state
        if not self._states or self._states[-1] != state:
            self._states.append(state)

    def _record_task_id(self, task_id: str) -> None:
        if not task_id:
            return
        self._task_id = task_id

    def _record_context_id(self, context_id: str) -> None:
        if not context_id:
            return
        self._context_id = context_id

    def _validate_response_identity(self, response: StreamResponse) -> None:
        if self._expected_identity is None:
            return
        if response.HasField("task"):
            task_id, context_id = response.task.id, response.task.context_id
        elif response.HasField("status_update"):
            task_id = response.status_update.task_id
            context_id = response.status_update.context_id
        elif response.HasField("artifact_update"):
            task_id = response.artifact_update.task_id
            context_id = response.artifact_update.context_id
        elif response.HasField("message"):
            task_id, context_id = response.message.task_id, response.message.context_id
        else:
            return
        expected_task_id, expected_context_id = self._expected_identity
        if task_id != expected_task_id:
            raise ProtocolError("agent response changed the subscribed task ID")
        if context_id != expected_context_id:
            raise ProtocolError("agent response changed the subscribed task context")

    def _validate_task_id(self, task_id: str) -> None:
        if (
            task_id
            and self._expected_identity is not None
            and task_id != self._expected_identity[0]
        ):
            raise ProtocolError("agent response changed the subscribed task ID")

    def _validate_context_id(self, context_id: str) -> None:
        if (
            context_id
            and self._expected_identity is not None
            and context_id != self._expected_identity[1]
        ):
            raise ProtocolError("agent response changed the subscribed task context")


def _collect_data(
    parts: Sequence[Part],
    *,
    source: Literal["message", "artifact"],
    artifact_id: str | None = None,
    artifact_name: str | None = None,
) -> tuple[DataPartResult, ...]:
    try:
        return tuple(
            DataPartResult(
                source=source,
                value=get_data_parts([part])[0],
                media_type=part.media_type or None,
                artifact_id=artifact_id,
                artifact_name=artifact_name,
            )
            for part in parts
            if part.WhichOneof("content") == "data"
        )
    except (ValidationError, ValueError) as error:
        raise ProtocolError("agent returned invalid structured data") from error


def _collect_files(
    parts: Sequence[Part],
    *,
    source: Literal["message", "artifact"],
    artifact_id: str | None = None,
    artifact_name: str | None = None,
) -> tuple[FilePartResult, ...]:
    files: list[FilePartResult] = []
    try:
        for part in parts:
            kind = part.WhichOneof("content")
            if kind not in {"raw", "url"}:
                continue
            if kind == "url" and len(part.url) > MAX_FILE_URL_CHARS:
                raise ProtocolError(
                    f"agent response file URL exceeded {MAX_FILE_URL_CHARS} characters"
                )
            files.append(
                FilePartResult(
                    source=source,
                    kind=kind,
                    filename=part.filename or None,
                    media_type=part.media_type or None,
                    size_bytes=len(part.raw) if kind == "raw" else None,
                    sha256=sha256(part.raw).hexdigest() if kind == "raw" else None,
                    artifact_id=artifact_id,
                    artifact_name=artifact_name,
                )
            )
    except (ValidationError, ValueError) as error:
        raise ProtocolError("agent returned invalid file metadata") from error
    return tuple(files)


def _raw_size(parts: Sequence[Part]) -> int:
    return sum(len(part.raw) for part in parts if part.WhichOneof("content") == "raw")


def _state_name(state: int) -> str:
    return TaskState.Name(state).removeprefix("TASK_STATE_").lower()
