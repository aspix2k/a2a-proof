from __future__ import annotations

from dataclasses import dataclass

from a2a.helpers import get_artifact_text, get_message_text
from a2a.types import Artifact, Message, Role, StreamResponse, Task, TaskState

MAX_EVENTS = 1_000
MAX_TEXT_CHARS = 1_000_000
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


class ResponseCollector:
    def __init__(self, context_id: str) -> None:
        self._context_id = context_id
        self._task_id: str | None = None
        self._state = "message"
        self._events = 0
        self._messages: dict[str, str] = {}
        self._artifacts: dict[str, str] = {}

    def add(self, response: StreamResponse) -> None:
        self._events += 1
        if self._events > MAX_EVENTS:
            raise ProtocolError(f"agent response exceeded {MAX_EVENTS} events")

        if response.HasField("message"):
            self._add_message(response.message)
        elif response.HasField("task"):
            self._add_task(response.task)
        elif response.HasField("status_update"):
            update = response.status_update
            self._task_id = update.task_id or self._task_id
            self._context_id = update.context_id or self._context_id
            self._state = _state_name(update.status.state)
            if update.status.HasField("message"):
                self._add_message(update.status.message)
        elif response.HasField("artifact_update"):
            update = response.artifact_update
            self._task_id = update.task_id or self._task_id
            self._context_id = update.context_id or self._context_id
            self._add_artifact(update.artifact, append=update.append)
        else:
            raise ProtocolError("agent returned an empty stream event")
        self._check_size()

    def finish(self, *, duration_ms: int) -> TurnOutcome:
        if self._events == 0:
            raise ProtocolError("agent returned no response")
        if self._state not in INTERRUPTED_STATES | {"message"}:
            raise ProtocolError(f"agent stream ended in non-terminal state {self._state!r}")
        text = "\n".join(filter(None, [*self._messages.values(), *self._artifacts.values()]))
        return TurnOutcome(
            state=self._state,
            text=text,
            task_id=self._task_id,
            context_id=self._context_id,
            duration_ms=duration_ms,
        )

    def _add_task(self, task: Task) -> None:
        self._task_id = task.id or self._task_id
        self._context_id = task.context_id or self._context_id
        self._state = _state_name(task.status.state)
        if task.status.HasField("message"):
            self._add_message(task.status.message)
        for message in task.history:
            self._add_message(message)
        for artifact in task.artifacts:
            self._add_artifact(artifact, append=False)

    def _add_message(self, message: Message) -> None:
        if message.role == Role.ROLE_USER:
            return
        key = message.message_id or f"message-{len(self._messages)}"
        self._messages[key] = get_message_text(message)
        self._task_id = message.task_id or self._task_id
        self._context_id = message.context_id or self._context_id

    def _add_artifact(self, artifact: Artifact, *, append: bool) -> None:
        key = artifact.artifact_id or f"artifact-{len(self._artifacts)}"
        value = get_artifact_text(artifact)
        self._artifacts[key] = self._artifacts.get(key, "") + value if append else value

    def _check_size(self) -> None:
        size = sum(map(len, self._messages.values())) + sum(map(len, self._artifacts.values()))
        if size > MAX_TEXT_CHARS:
            raise ProtocolError(f"agent response text exceeded {MAX_TEXT_CHARS} characters")


def _state_name(state: int) -> str:
    return TaskState.Name(state).removeprefix("TASK_STATE_").lower()
