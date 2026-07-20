from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx
from a2a.client.client import Client, ClientCallContext
from a2a.client.errors import AgentCardResolutionError
from a2a.helpers import (
    get_data_parts,
    get_message_text,
    new_data_artifact,
    new_data_message,
    new_data_part,
)
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    Artifact,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

import a2a_proof.a2a as a2a_module
import a2a_proof.protocol as protocol_module
from a2a_proof.a2a import (
    A2ASession,
    _grpc_channel,
    _grpc_target,
    _protocol_bindings,
    _validate_card_extensions,
    _validate_card_interfaces,
    discover_agent,
)
from a2a_proof.files import PreparedFile
from a2a_proof.models import AgentConfig
from a2a_proof.protocol import ProtocolError, ResponseCollector
from a2a_proof.push import PushTarget


def _agent_message(text: str, *, message_id: str = "message") -> Message:
    return Message(
        message_id=message_id,
        context_id="context",
        role=Role.ROLE_AGENT,
        parts=[Part(text=text)],
    )


def _agent_card(url: str = "https://example.com/a2a") -> dict[str, object]:
    return {
        "name": "Test agent",
        "description": "Test",
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }


class _FakeClient:
    def __init__(self, responses: list[StreamResponse]) -> None:
        self.responses = responses
        self.request: SendMessageRequest | None = None
        self.context: ClientCallContext | None = None
        self.closed = False
        self.cancel_request: CancelTaskRequest | None = None
        self.get_request: GetTaskRequest | None = None
        self.subscribe_request: SubscribeToTaskRequest | None = None
        self.task_response = Task(
            id="task",
            context_id="context",
            status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
        )

    async def send_message(
        self,
        request,
        *,
        context: ClientCallContext | None = None,
    ) -> AsyncIterator[StreamResponse]:
        self.request = request
        self.context = context
        for response in self.responses:
            yield response

    async def close(self) -> None:
        self.closed = True

    async def cancel_task(
        self,
        request: CancelTaskRequest,
        *,
        context: ClientCallContext | None = None,
    ) -> Task:
        self.cancel_request = request
        self.context = context
        return self.task_response

    async def get_task(
        self,
        request: GetTaskRequest,
        *,
        context: ClientCallContext | None = None,
    ) -> Task:
        self.get_request = request
        self.context = context
        return self.task_response

    async def subscribe(
        self,
        request: SubscribeToTaskRequest,
        *,
        context: ClientCallContext | None = None,
    ) -> AsyncIterator[StreamResponse]:
        self.subscribe_request = request
        self.context = context
        for response in self.responses:
            yield response


class _ForcedTimeout:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        raise TimeoutError


def test_collects_direct_message() -> None:
    collector = ResponseCollector("initial")
    collector.add(StreamResponse(message=_agent_message("Hello")))

    outcome = collector.finish(duration_ms=12)

    assert outcome.state == "message"
    assert outcome.text == "Hello"
    assert outcome.context_id == "context"
    assert outcome.task_id is None
    assert outcome.duration_ms == 12
    assert outcome.states == ("message",)


def test_collects_task_identity_from_direct_agent_message() -> None:
    collector = ResponseCollector("initial")
    collector.add(
        StreamResponse(
            message=Message(
                message_id="message",
                task_id="task",
                context_id="context",
                role=Role.ROLE_AGENT,
                parts=[Part(text="Hello")],
            )
        )
    )

    outcome = collector.finish(duration_ms=1)

    assert outcome.task_id == "task"
    assert outcome.context_id == "context"


def test_preserves_initial_ids_when_message_omits_them() -> None:
    collector = ResponseCollector("initial")
    collector.add(
        StreamResponse(
            message=Message(
                role=Role.ROLE_AGENT,
                parts=[Part(text="Hello")],
            )
        )
    )

    outcome = collector.finish(duration_ms=1)

    assert outcome.context_id == "initial"
    assert outcome.task_id is None


def test_collects_task_history_status_and_artifact_chunks() -> None:
    collector = ResponseCollector("initial")
    collector.add(
        StreamResponse(
            task=Task(
                id="task",
                context_id="context",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                history=[
                    Message(
                        message_id="user",
                        role=Role.ROLE_USER,
                        parts=[Part(text="ignored")],
                    ),
                    _agent_message("Working", message_id="progress"),
                ],
            )
        )
    )
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                task_id="task",
                context_id="context",
                artifact=Artifact(artifact_id="answer", parts=[Part(text="Hel")]),
            )
        )
    )
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                task_id="task",
                context_id="context",
                artifact=Artifact(artifact_id="answer", parts=[Part(text="lo")]),
                append=True,
                last_chunk=True,
            )
        )
    )
    collector.add(
        StreamResponse(
            status_update=TaskStatusUpdateEvent(
                task_id="task",
                context_id="context",
                status=TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED,
                    message=_agent_message("Done", message_id="done"),
                ),
            )
        )
    )

    outcome = collector.finish(duration_ms=5)

    assert outcome.state == "completed"
    assert outcome.task_id == "task"
    assert outcome.context_id == "context"
    assert outcome.text == "Working\nDone\nHello"
    assert outcome.states == ("working", "completed")


def test_task_snapshot_collects_artifacts() -> None:
    collector = ResponseCollector("context")
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                task_id="task",
                context_id="context",
                artifact=Artifact(artifact_id="answer", parts=[Part(text="old")]),
            )
        )
    )
    collector.add(
        StreamResponse(
            task=Task(
                id="task",
                context_id="context",
                status=TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED,
                    message=_agent_message("Done"),
                ),
                artifacts=[Artifact(artifact_id="answer", parts=[Part(text="42")])],
            )
        )
    )

    outcome = collector.finish(duration_ms=1)

    assert outcome.text == "Done\n42"
    assert outcome.task_id == "task"
    assert outcome.context_id == "context"


def test_collects_ids_from_an_isolated_task_snapshot() -> None:
    collector = ResponseCollector("initial")
    collector.add(
        StreamResponse(
            task=Task(
                id="task",
                context_id="task-context",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        )
    )

    outcome = collector.finish(duration_ms=1)

    assert outcome.task_id == "task"
    assert outcome.context_id == "task-context"


def test_collects_ids_from_status_and_artifact_updates() -> None:
    status_collector = ResponseCollector("initial")
    status_collector.add(
        StreamResponse(
            status_update=TaskStatusUpdateEvent(
                task_id="status-task",
                context_id="status-context",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        )
    )
    status = status_collector.finish(duration_ms=1)
    assert status.task_id == "status-task"
    assert status.context_id == "status-context"

    artifact_collector = ResponseCollector("initial")
    artifact_collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                task_id="artifact-task",
                context_id="artifact-context",
                artifact=Artifact(parts=[Part(text="result")]),
            )
        )
    )
    artifact = artifact_collector.finish(duration_ms=1)
    assert artifact.task_id == "artifact-task"
    assert artifact.context_id == "artifact-context"
    assert artifact.states == ("message",)


def test_replaces_messages_by_id_and_joins_text_parts() -> None:
    collector = ResponseCollector("context")
    collector.add(StreamResponse(message=_agent_message("old", message_id="same")))
    collector.add(
        StreamResponse(
            message=Message(
                message_id="same",
                role=Role.ROLE_AGENT,
                parts=[Part(text="line one"), Part(text="line two")],
            )
        )
    )

    assert collector.finish(duration_ms=1).text == "line one\nline two"


def test_keeps_distinct_artifacts_and_accepts_an_initial_append() -> None:
    collector = ResponseCollector("context")
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=Artifact(
                    artifact_id="first",
                    parts=[Part(text="one"), Part(text="two")],
                )
            )
        )
    )
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=Artifact(artifact_id="second", parts=[Part(text="three")]),
            )
        )
    )

    assert collector.finish(duration_ms=1).text == "one\ntwo\nthree"

    appended = ResponseCollector("context")
    appended.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=Artifact(artifact_id="answer", parts=[Part(text="tail")]),
                append=True,
            )
        )
    )
    assert appended.finish(duration_ms=1).text == "tail"


def test_collects_structured_message_and_artifact_data() -> None:
    collector = ResponseCollector("context")
    message = new_data_message({"phase": "working"}, media_type="application/json")
    message.message_id = "progress"
    collector.add(StreamResponse(message=message))
    collector.add(
        StreamResponse(
            task=Task(
                id="task",
                context_id="context",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                artifacts=[
                    new_data_artifact(
                        "forecast",
                        {"city": "Paris", "temperature": 21},
                        media_type="application/json",
                        artifact_id="result",
                    )
                ],
            )
        )
    )

    outcome = collector.finish(duration_ms=1)

    assert [part.model_dump() for part in outcome.data] == [
        {
            "source": "message",
            "value": {"phase": "working"},
            "media_type": "application/json",
            "artifact_id": None,
            "artifact_name": None,
        },
        {
            "source": "artifact",
            "value": {"city": "Paris", "temperature": 21.0},
            "media_type": "application/json",
            "artifact_id": "result",
            "artifact_name": "forecast",
        },
    ]


def test_collects_file_metadata_without_fetching_urls() -> None:
    collector = ResponseCollector("context")
    collector.add(
        StreamResponse(
            message=Message(
                role=Role.ROLE_AGENT,
                parts=[
                    Part(text="attachment"),
                    Part(raw=b"hello", filename="answer.txt", media_type="text/plain"),
                    Part(url="https://files.example/report.pdf?token=secret"),
                ],
            )
        )
    )
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=Artifact(
                    artifact_id="report",
                    name="generated report",
                    parts=[
                        Part(
                            url="https://files.example/report.pdf",
                            filename="report.pdf",
                            media_type="application/pdf",
                        )
                    ],
                )
            )
        )
    )

    outcome = collector.finish(duration_ms=1)

    assert [part.model_dump() for part in outcome.files] == [
        {
            "source": "message",
            "kind": "raw",
            "filename": "answer.txt",
            "media_type": "text/plain",
            "size_bytes": 5,
            "artifact_id": None,
            "artifact_name": None,
        },
        {
            "source": "message",
            "kind": "url",
            "filename": None,
            "media_type": None,
            "size_bytes": None,
            "artifact_id": None,
            "artifact_name": None,
        },
        {
            "source": "artifact",
            "kind": "url",
            "filename": "report.pdf",
            "media_type": "application/pdf",
            "size_bytes": None,
            "artifact_id": "report",
            "artifact_name": "generated report",
        },
    ]
    digest = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert outcome.files[0].sha256 == digest
    assert digest not in repr(outcome.files)
    assert "secret" not in repr(outcome.files)


def test_appends_file_parts_and_collapses_duplicate_states() -> None:
    collector = ResponseCollector("context")
    collector.add(
        StreamResponse(
            task=Task(
                id="task",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        )
    )
    collector.add(
        StreamResponse(
            status_update=TaskStatusUpdateEvent(
                task_id="task",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        )
    )
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=Artifact(
                    artifact_id="answer",
                    parts=[Part(raw=b"a", filename="one.bin")],
                )
            )
        )
    )
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=Artifact(
                    artifact_id="answer",
                    parts=[Part(raw=b"b", filename="two.bin")],
                ),
                append=True,
            )
        )
    )
    collector.add(
        StreamResponse(
            status_update=TaskStatusUpdateEvent(
                task_id="task",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        )
    )

    outcome = collector.finish(duration_ms=1)

    assert outcome.states == ("working", "completed")
    assert [part.filename for part in outcome.files] == ["one.bin", "two.bin"]


def test_appends_and_replaces_structured_artifact_parts() -> None:
    collector = ResponseCollector("context")
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=new_data_artifact(
                    "result",
                    {"chunk": 1},
                    artifact_id="result",
                )
            )
        )
    )
    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=Artifact(
                    artifact_id="result",
                    parts=[new_data_part({"chunk": 2})],
                ),
                append=True,
            )
        )
    )

    data = collector.finish(duration_ms=1).data
    assert [part.value for part in data] == [
        {"chunk": 1.0},
        {"chunk": 2.0},
    ]
    assert [part.artifact_name for part in data] == ["result", "result"]

    collector.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=new_data_artifact(
                    "replacement",
                    {"chunk": 3},
                    artifact_id="result",
                )
            )
        )
    )
    outcome = collector.finish(duration_ms=1)
    assert [part.value for part in outcome.data] == [{"chunk": 3.0}]
    assert outcome.data[0].artifact_name == "replacement"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (None, "agent returned no response"),
        (StreamResponse(), "agent returned an empty stream event"),
        (
            StreamResponse(
                task=Task(
                    id="task",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            ),
            "agent stream ended in non-terminal state 'working'",
        ),
    ],
)
def test_rejects_invalid_streams(response: StreamResponse | None, expected: str) -> None:
    collector = ResponseCollector("context")
    if response is not None:
        if response == StreamResponse():
            with pytest.raises(ProtocolError) as raised:
                collector.add(response)
            assert str(raised.value) == expected
            return
        collector.add(response)

    with pytest.raises(ProtocolError) as raised:
        collector.finish(duration_ms=1)
    assert str(raised.value) == expected


def test_enforces_event_and_text_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol_module, "MAX_EVENTS", 1)
    collector = ResponseCollector("context")
    collector.add(StreamResponse(message=_agent_message("one", message_id="one")))
    with pytest.raises(ProtocolError, match="exceeded 1 events"):
        collector.add(StreamResponse(message=_agent_message("two", message_id="two")))

    monkeypatch.setattr(protocol_module, "MAX_TEXT_CHARS", 2)
    collector = ResponseCollector("context")
    with pytest.raises(ProtocolError, match="exceeded 2 characters"):
        collector.add(StreamResponse(message=_agent_message("long")))


def test_text_limit_counts_messages_and_artifacts_and_allows_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol_module, "MAX_TEXT_CHARS", 3)
    boundary = ResponseCollector("context")
    boundary.add(StreamResponse(message=_agent_message("abc")))
    assert boundary.finish(duration_ms=1).text == "abc"

    monkeypatch.setattr(protocol_module, "MAX_TEXT_CHARS", 2)
    combined = ResponseCollector("context")
    combined.add(StreamResponse(message=_agent_message("a")))
    with pytest.raises(ProtocolError, match="exceeded 2 characters"):
        combined.add(
            StreamResponse(
                artifact_update=TaskArtifactUpdateEvent(
                    artifact=Artifact(parts=[Part(text="bb")]),
                )
            )
        )


def test_enforces_structured_data_count_and_size_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol_module, "MAX_DATA_PARTS", 1)
    collector = ResponseCollector("context")
    with pytest.raises(ProtocolError, match="exceeded 1 structured data parts"):
        collector.add(
            StreamResponse(
                message=Message(
                    role=Role.ROLE_AGENT,
                    parts=[new_data_part(1), new_data_part(2)],
                )
            )
        )

    monkeypatch.setattr(protocol_module, "MAX_DATA_PARTS", 1_000)
    monkeypatch.setattr(protocol_module, "MAX_DATA_BYTES", 4)
    boundary = ResponseCollector("context")
    boundary.add(StreamResponse(message=new_data_message(10)))
    assert boundary.finish(duration_ms=1).data[0].value == 10.0

    oversized = ResponseCollector("context")
    with pytest.raises(ProtocolError, match="structured data exceeded 4 bytes"):
        oversized.add(StreamResponse(message=new_data_message("long")))

    metadata = ResponseCollector("context")
    with pytest.raises(ProtocolError, match="structured data exceeded 4 bytes"):
        metadata.add(StreamResponse(message=new_data_message(10, media_type="x")))


def test_structured_data_count_limit_allows_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol_module, "MAX_DATA_PARTS", 2)
    collector = ResponseCollector("context")

    collector.add(
        StreamResponse(
            message=Message(
                role=Role.ROLE_AGENT,
                parts=[new_data_part(1), new_data_part(2)],
            )
        )
    )

    assert len(collector.finish(duration_ms=1).data) == 2


def test_rejects_non_finite_structured_data() -> None:
    collector = ResponseCollector("context")

    with pytest.raises(ProtocolError) as raised:
        collector.add(StreamResponse(message=new_data_message(float("nan"))))
    assert str(raised.value) == "agent returned invalid structured data"


def test_enforces_raw_data_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol_module, "MAX_RAW_BYTES", 2)
    collector = ResponseCollector("context")

    with pytest.raises(ProtocolError, match="raw data exceeded 2 bytes"):
        collector.add(
            StreamResponse(
                message=Message(
                    role=Role.ROLE_AGENT,
                    parts=[Part(raw=b"abc")],
                )
            )
        )

    monkeypatch.setattr(protocol_module, "MAX_RAW_BYTES", 3)
    boundary = ResponseCollector("context")
    boundary.add(
        StreamResponse(
            message=Message(
                role=Role.ROLE_AGENT,
                parts=[Part(raw=b"abc")],
            )
        )
    )

    appended = ResponseCollector("context")
    appended.add(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                artifact=Artifact(artifact_id="raw", parts=[Part(raw=b"aa")]),
            )
        )
    )
    with pytest.raises(ProtocolError, match="raw data exceeded 3 bytes"):
        appended.add(
            StreamResponse(
                artifact_update=TaskArtifactUpdateEvent(
                    artifact=Artifact(artifact_id="raw", parts=[Part(raw=b"bb")]),
                    append=True,
                )
            )
        )


def test_enforces_file_count_url_and_metadata_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol_module, "MAX_FILE_PARTS", 1)
    collector = ResponseCollector("context")
    with pytest.raises(ProtocolError, match="exceeded 1 file parts"):
        collector.add(
            StreamResponse(
                message=Message(
                    role=Role.ROLE_AGENT,
                    parts=[Part(raw=b"a"), Part(url="https://example.com/b")],
                )
            )
        )

    monkeypatch.setattr(protocol_module, "MAX_FILE_PARTS", 2)
    boundary = ResponseCollector("context")
    boundary.add(
        StreamResponse(
            message=Message(
                role=Role.ROLE_AGENT,
                parts=[Part(raw=b"a"), Part(url="https://example.com/b")],
            )
        )
    )
    assert len(boundary.finish(duration_ms=1).files) == 2

    url = "https://example.com"
    monkeypatch.setattr(protocol_module, "MAX_FILE_URL_CHARS", len(url))
    boundary = ResponseCollector("context")
    boundary.add(StreamResponse(message=Message(role=Role.ROLE_AGENT, parts=[Part(url=url)])))
    assert len(boundary.finish(duration_ms=1).files) == 1

    monkeypatch.setattr(protocol_module, "MAX_FILE_URL_CHARS", 5)
    collector = ResponseCollector("context")
    with pytest.raises(ProtocolError, match="file URL exceeded 5 characters"):
        collector.add(
            StreamResponse(
                message=Message(role=Role.ROLE_AGENT, parts=[Part(url="https://example.com")])
            )
        )

    collector = ResponseCollector("context")
    with pytest.raises(ProtocolError) as raised:
        collector.add(
            StreamResponse(
                message=Message(
                    role=Role.ROLE_AGENT,
                    parts=[Part(raw=b"a", filename="x" * 1_001)],
                )
            )
        )
    assert str(raised.value) == "agent returned invalid file metadata"


@pytest.mark.asyncio
async def test_session_sends_context_and_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter([10.0, 10.025, 10.1])
    monkeypatch.setattr(a2a_module, "perf_counter", lambda: next(timestamps))
    client = _FakeClient([StreamResponse(message=_agent_message("Hello"))])
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent"),
        timeout=2,
        headers={"Authorization": "Bearer secret"},
    )

    async with session:
        outcome = await session.send_turn(
            "Hi",
            context_id="context",
            task_id="task",
        )

    assert outcome.text == "Hello"
    assert outcome.first_event_ms == 25
    assert outcome.duration_ms == 100
    assert client.request is not None
    assert client.context is not None
    assert client.request.message.context_id == "context"
    assert client.request.message.task_id == "task"
    assert not client.request.HasField("configuration")
    assert get_message_text(client.request.message) == "Hi"
    assert client.context.timeout == 2
    assert client.context.service_parameters == {"Authorization": "Bearer secret"}
    assert client.closed


@pytest.mark.asyncio
async def test_session_sends_structured_parts_and_normalizes_extensions() -> None:
    client = _FakeClient([StreamResponse(message=_agent_message("accepted"))])
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent"),
        timeout=2,
        headers={
            "Authorization": "Bearer secret",
            "a2a-extensions": "https://example.com/extensions/one",
        },
        extensions=["https://example.com/extensions/two"],
    )

    await session.send_turn(
        None,
        data=[{"order_id": "order-42"}, ["one", "two"]],
        files=[
            PreparedFile(
                content=b"report",
                filename="report.pdf",
                media_type="application/pdf",
            )
        ],
        context_id="context",
        task_id=None,
    )

    assert client.request is not None
    assert client.context is not None
    assert get_message_text(client.request.message) == ""
    assert get_data_parts(client.request.message.parts) == [
        {"order_id": "order-42"},
        ["one", "two"],
    ]
    assert client.request.message.parts[-1] == Part(
        raw=b"report",
        filename="report.pdf",
        media_type="application/pdf",
    )
    assert client.context.service_parameters == {
        "Authorization": "Bearer secret",
        "A2A-Extensions": ("https://example.com/extensions/one,https://example.com/extensions/two"),
    }


@pytest.mark.asyncio
async def test_session_records_only_the_first_event_time(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter([10.0, 10.01, 10.1])
    monkeypatch.setattr(a2a_module, "perf_counter", lambda: next(timestamps))
    client = _FakeClient(
        [
            StreamResponse(message=_agent_message("one", message_id="one")),
            StreamResponse(message=_agent_message("two", message_id="two")),
        ]
    )
    session = A2ASession(cast(Client, client), AgentCard(name="Agent"), timeout=2)

    outcome = await session.send_turn("Hi", context_id="context", task_id=None)

    assert outcome.first_event_ms == 10
    assert outcome.duration_ms == 100


@pytest.mark.asyncio
async def test_session_uses_non_streaming_client_for_immediate_task() -> None:
    streaming_client = _FakeClient([])
    lifecycle_client = _FakeClient(
        [
            StreamResponse(
                task=Task(
                    id="task",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )
        ]
    )
    session = A2ASession(
        cast(Client, streaming_client),
        AgentCard(name="Agent"),
        timeout=2,
        lifecycle_client=cast(Client, lifecycle_client),
    )

    outcome = await session.send_turn(
        "Start",
        context_id="context",
        task_id=None,
        return_immediately=True,
    )

    assert outcome.state == "working"
    assert streaming_client.request is None
    assert lifecycle_client.request is not None
    assert lifecycle_client.request.configuration.return_immediately


@pytest.mark.asyncio
async def test_session_registers_inline_push_notification() -> None:
    client = _FakeClient(
        [
            StreamResponse(
                task=Task(
                    id="task",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )
        ]
    )
    session = A2ASession(
        cast(Client, client),
        AgentCard(
            name="Agent",
            capabilities=AgentCapabilities(push_notifications=True),
        ),
        timeout=2,
    )

    await session.send_turn(
        "Start",
        context_id="context",
        task_id=None,
        return_immediately=True,
        push_notification=PushTarget(
            url="https://hooks.example/.a2a-proof/push/route",
            token="secret-token",
        ),
    )

    assert client.request is not None
    configuration = client.request.configuration
    assert configuration.return_immediately
    assert configuration.task_push_notification_config.url == (
        "https://hooks.example/.a2a-proof/push/route"
    )
    assert configuration.task_push_notification_config.token == "secret-token"
    assert configuration.task_push_notification_config.task_id == ""
    assert configuration.task_push_notification_config.authentication.scheme == "Bearer"
    assert configuration.task_push_notification_config.authentication.credentials == "secret-token"


@pytest.mark.asyncio
async def test_session_rejects_push_when_card_does_not_advertise_it() -> None:
    client = _FakeClient([])
    session = A2ASession(cast(Client, client), AgentCard(name="Agent"), timeout=2)

    with pytest.raises(ProtocolError, match="does not advertise push notifications"):
        await session.send_turn(
            "Start",
            context_id="context",
            task_id=None,
            return_immediately=True,
            push_notification=PushTarget(
                url="https://hooks.example/.a2a-proof/push/route",
                token="secret-token",
            ),
        )

    assert client.request is None


@pytest.mark.asyncio
async def test_session_runs_cancel_and_get_task_operations() -> None:
    client = _FakeClient([])
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent"),
        timeout=2,
        headers={"Authorization": "Bearer secret"},
    )

    canceled = await session.cancel_task(task_id="task", context_id="fallback")
    retrieved = await session.get_task(
        task_id="task",
        context_id="fallback",
        history_length=7,
    )

    assert canceled.state == "canceled"
    assert retrieved.task_id == "task"
    assert client.cancel_request == CancelTaskRequest(id="task")
    assert client.get_request == GetTaskRequest(id="task", history_length=7)
    assert client.context is not None
    assert client.context.service_parameters == {"Authorization": "Bearer secret"}

    await session.get_task(task_id="task", context_id="fallback")
    assert client.get_request is not None
    assert not client.get_request.HasField("history_length")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_id", "context_id", "error"),
    [
        ("other", "context", "changed the subscribed task ID"),
        ("", "context", "changed the subscribed task ID"),
        ("task", "other", "changed the subscribed task context"),
        ("task", "", "changed the subscribed task context"),
    ],
)
async def test_session_rejects_subscription_to_different_task_or_context(
    task_id: str,
    context_id: str,
    error: str,
) -> None:
    client = _FakeClient(
        [
            StreamResponse(
                task=Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )
        ]
    )
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True)),
        timeout=2,
    )

    with pytest.raises(ProtocolError) as raised:
        await session.subscribe_task(task_id="task", context_id="context")
    assert str(raised.value) == f"agent response {error}"


@pytest.mark.asyncio
async def test_session_requires_subscription_task_snapshot_first() -> None:
    client = _FakeClient(
        [
            StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="task",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )
        ]
    )
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True)),
        timeout=2,
    )

    with pytest.raises(ProtocolError, match="did not start with a task snapshot"):
        await session.subscribe_task(task_id="task", context_id="context")


@pytest.mark.asyncio
async def test_session_rejects_subscription_event_for_another_task() -> None:
    client = _FakeClient(
        [
            StreamResponse(
                task=Task(
                    id="task",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            ),
            StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="other",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                )
            ),
        ]
    )
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True)),
        timeout=2,
    )

    with pytest.raises(ProtocolError, match="changed the subscribed task ID"):
        await session.subscribe_task(task_id="task", context_id="context")


@pytest.mark.asyncio
async def test_session_rejects_standalone_subscription_message() -> None:
    client = _FakeClient(
        [
            StreamResponse(
                task=Task(
                    id="task",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            ),
            StreamResponse(
                message=Message(
                    message_id="message",
                    task_id="task",
                    context_id="context",
                    role=Role.ROLE_USER,
                    parts=[Part(text="ignored")],
                )
            ),
        ]
    )
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True)),
        timeout=2,
    )

    with pytest.raises(ProtocolError, match="returned a standalone message"):
        await session.subscribe_task(task_id="task", context_id="context")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_task_id", "message_context_id", "error"),
    [
        ("other", "context", "agent response changed the subscribed task ID"),
        ("task", "other", "agent response changed the subscribed task context"),
    ],
)
async def test_session_rejects_foreign_task_history_message(
    message_task_id: str,
    message_context_id: str,
    error: str,
) -> None:
    client = _FakeClient(
        [
            StreamResponse(
                task=Task(
                    id="task",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                    history=[
                        Message(
                            message_id="foreign",
                            task_id=message_task_id,
                            context_id=message_context_id,
                            role=Role.ROLE_USER,
                            parts=[Part(text="ignored")],
                        )
                    ],
                )
            )
        ]
    )
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True)),
        timeout=2,
    )

    with pytest.raises(ProtocolError) as raised:
        await session.subscribe_task(task_id="task", context_id="context")
    assert str(raised.value) == error


@pytest.mark.asyncio
async def test_session_reports_task_operation_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient([])
    session = A2ASession(cast(Client, client), AgentCard(name="Agent"), timeout=2)
    monkeypatch.setattr(a2a_module.asyncio, "timeout", lambda seconds: _ForcedTimeout())

    with pytest.raises(ProtocolError, match="did not cancel task"):
        await session.cancel_task(task_id="task", context_id="context")
    with pytest.raises(ProtocolError, match="did not return task"):
        await session.get_task(task_id="task", context_id="context")


@pytest.mark.asyncio
async def test_session_subscribes_to_existing_task() -> None:
    client = _FakeClient(
        [
            StreamResponse(
                task=Task(
                    id="task",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            ),
            StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="task",
                    context_id="context",
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_COMPLETED,
                        message=_agent_message("finished"),
                    ),
                )
            ),
        ]
    )
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True)),
        timeout=2,
        headers={"Authorization": "Bearer secret"},
    )

    outcome = await session.subscribe_task(task_id="task", context_id="context")

    assert outcome.state == "completed"
    assert outcome.states == ("working", "completed")
    assert outcome.text == "finished"
    assert outcome.task_id == "task"
    assert outcome.context_id == "context"
    assert outcome.first_event_ms is not None
    assert client.subscribe_request == SubscribeToTaskRequest(id="task")
    assert client.context is not None
    assert client.context.service_parameters == {"Authorization": "Bearer secret"}


@pytest.mark.asyncio
async def test_session_rejects_subscribe_without_streaming_capability() -> None:
    client = _FakeClient([])
    session = A2ASession(cast(Client, client), AgentCard(name="Agent"), timeout=2)

    with pytest.raises(ProtocolError, match="does not advertise streaming"):
        await session.subscribe_task(task_id="task", context_id="context")

    assert client.subscribe_request is None


@pytest.mark.asyncio
async def test_session_reports_subscribe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([])
    session = A2ASession(
        cast(Client, client),
        AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True)),
        timeout=2,
    )
    monkeypatch.setattr(a2a_module.asyncio, "timeout", lambda seconds: _ForcedTimeout())

    with pytest.raises(ProtocolError, match="did not finish subscribed task"):
        await session.subscribe_task(task_id="task", context_id="context")


@pytest.mark.asyncio
async def test_session_closes_both_clients() -> None:
    streaming_client = _FakeClient([])
    lifecycle_client = _FakeClient([])
    session = A2ASession(
        cast(Client, streaming_client),
        AgentCard(name="Agent"),
        timeout=2,
        lifecycle_client=cast(Client, lifecycle_client),
    )

    await session.close()

    assert streaming_client.closed
    assert lifecycle_client.closed


@pytest.mark.asyncio
async def test_session_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([])
    session = A2ASession(cast(Client, client), AgentCard(name="Agent"), timeout=2)
    monkeypatch.setattr(a2a_module.asyncio, "timeout", lambda seconds: _ForcedTimeout())

    with pytest.raises(ProtocolError, match="did not finish within"):
        await session.send_turn("Hi", context_id="context", task_id=None)


@pytest.mark.asyncio
@respx.mock
async def test_connect_builds_client_for_discovered_card() -> None:
    respx.get("https://example.com/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json=_agent_card())
    )

    session = await A2ASession.connect(AgentConfig(url="https://example.com"))
    assert session.card.name == "Test agent"
    await session.close()


@pytest.mark.asyncio
@respx.mock
async def test_connect_builds_non_streaming_client_for_streaming_agent() -> None:
    card = _agent_card()
    card["capabilities"] = {"streaming": True}
    respx.get("https://example.com/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json=card)
    )

    session = await A2ASession.connect(AgentConfig(url="https://example.com"))

    assert session._lifecycle_client is not session._client
    await session.close()


@pytest.mark.asyncio
async def test_connect_closes_resources_when_lifecycle_client_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_http = AsyncMock()
    lifecycle_http = AsyncMock()
    clients = iter([primary_http, lifecycle_http])
    primary_client = _FakeClient([])
    factory = Mock()
    factory.return_value.create.side_effect = [primary_client, RuntimeError("factory failed")]
    card = AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True))

    monkeypatch.setattr(
        a2a_module,
        "_http_client",
        lambda config, *, trust_env: next(clients),
    )
    monkeypatch.setattr(a2a_module, "_resolve_card", AsyncMock(return_value=card))
    monkeypatch.setattr(a2a_module, "ClientFactory", factory)

    with pytest.raises(RuntimeError, match="factory failed"):
        await A2ASession.connect(AgentConfig(url="https://example.com"))

    assert primary_client.closed
    lifecycle_http.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_closes_both_clients_when_session_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_http = AsyncMock()
    lifecycle_http = AsyncMock()
    clients = iter([primary_http, lifecycle_http])
    primary_client = _FakeClient([])
    lifecycle_client = _FakeClient([])
    factory = Mock()
    factory.return_value.create.side_effect = [primary_client, lifecycle_client]
    card = AgentCard(name="Agent", capabilities=AgentCapabilities(streaming=True))

    class BrokenSession(A2ASession):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("construction failed")

    monkeypatch.setattr(
        a2a_module,
        "_http_client",
        lambda config, *, trust_env: next(clients),
    )
    monkeypatch.setattr(a2a_module, "_resolve_card", AsyncMock(return_value=card))
    monkeypatch.setattr(a2a_module, "ClientFactory", factory)

    with pytest.raises(RuntimeError, match="construction failed"):
        await BrokenSession.connect(AgentConfig(url="https://example.com"))

    assert primary_client.closed
    assert lifecycle_client.closed


@pytest.mark.asyncio
async def test_connect_closes_http_client_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    http_client = AsyncMock()

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(
        a2a_module,
        "_http_client",
        lambda config, *, trust_env: http_client,
    )
    monkeypatch.setattr(a2a_module, "_resolve_card", fail)

    with pytest.raises(RuntimeError, match="discovery failed"):
        await A2ASession.connect(AgentConfig(url="https://example.com"))
    http_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_closes_http_client_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client = AsyncMock()

    async def cancel(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        a2a_module,
        "_http_client",
        lambda config, *, trust_env: http_client,
    )
    monkeypatch.setattr(a2a_module, "_resolve_card", cancel)

    with pytest.raises(asyncio.CancelledError):
        await A2ASession.connect(AgentConfig(url="https://example.com"))
    http_client.aclose.assert_awaited_once()


def test_maps_supported_protocol_bindings() -> None:
    assert _protocol_bindings("JSONRPC") == ["JSONRPC"]
    assert _protocol_bindings("HTTP+JSON") == ["HTTP+JSON"]
    assert _protocol_bindings("GRPC") == ["GRPC"]
    assert _protocol_bindings("auto") == ["JSONRPC", "HTTP+JSON", "GRPC"]


def test_validates_requested_and_required_card_extensions() -> None:
    card = AgentCard(
        name="Agent",
        capabilities=AgentCapabilities(
            extensions=[
                AgentExtension(uri="https://example.com/optional"),
                AgentExtension(uri="https://example.com/required", required=True),
            ]
        ),
    )

    _validate_card_extensions(
        card,
        ["https://example.com/optional", "https://example.com/required"],
    )

    with pytest.raises(ProtocolError) as raised:
        _validate_card_extensions(card, ["https://example.com/unadvertised"])
    assert str(raised.value) == (
        "Agent Card does not advertise requested extension(s): "
        "https://example.com/unadvertised; Agent Card requires unconfigured extension(s): "
        "https://example.com/required"
    )


def test_normalizes_and_validates_grpc_targets() -> None:
    assert _grpc_target("https://agent.example.com:443") == "agent.example.com:443"
    assert _grpc_target("dns:///agent.example.com:443") == "dns:///agent.example.com:443"

    with pytest.raises(ProtocolError, match="must not contain a path"):
        _grpc_target("grpcs://agent.example.com/service")
    with pytest.raises(ProtocolError, match="invalid gRPC interface URL"):
        _grpc_target("https://user:password@agent.example.com")


def test_builds_secure_grpc_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = object()
    channel = object()
    secure_channel = Mock(return_value=channel)
    monkeypatch.setattr(a2a_module.grpc, "ssl_channel_credentials", lambda: credentials)
    monkeypatch.setattr(a2a_module.grpc.aio, "secure_channel", secure_channel)

    assert _grpc_channel("https://agent.example.com:443", tls=True) is channel
    secure_channel.assert_called_once_with("agent.example.com:443", credentials)


def test_rejects_cross_origin_agent_interfaces_by_default() -> None:
    card = AgentCard(
        name="Agent",
        supported_interfaces=[
            AgentInterface(
                url="https://api.example.net/a2a",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
    )

    with pytest.raises(ProtocolError, match="different origin"):
        _validate_card_interfaces(card, "https://agent.example.com", allow_cross_origin=False)

    _validate_card_interfaces(card, "https://agent.example.com", allow_cross_origin=True)


def test_accepts_equivalent_secure_interface_origin() -> None:
    card = AgentCard(
        name="Agent",
        supported_interfaces=[
            AgentInterface(
                url="grpcs://agent.example.com:443",
                protocol_binding="GRPC",
                protocol_version="1.0",
            )
        ],
    )

    _validate_card_interfaces(card, "https://agent.example.com", allow_cross_origin=False)


@pytest.mark.parametrize(
    "url",
    [
        "dns:///agent.example.com:443",
        "https://user:password@agent.example.com",
        "https://agent.example.com:invalid",
    ],
)
def test_rejects_invalid_interface_urls(url: str) -> None:
    card = AgentCard(
        name="Agent",
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="GRPC", protocol_version="1.0")
        ],
    )

    with pytest.raises(ProtocolError, match=r"unsupported URL|invalid URL"):
        _validate_card_interfaces(card, "https://agent.example.com", allow_cross_origin=False)


@pytest.mark.asyncio
@respx.mock
async def test_discovers_current_agent_card_and_sends_headers() -> None:
    route = respx.get("https://example.com/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json=_agent_card())
    )
    config = AgentConfig(
        url="https://example.com",
        headers={"Authorization": "Bearer secret"},
    )

    card = await discover_agent(config)

    assert card.name == "Test agent"
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
@respx.mock
async def test_falls_back_to_legacy_agent_card_path_on_404() -> None:
    respx.get("https://example.com/.well-known/agent-card.json").mock(
        return_value=httpx.Response(404)
    )
    legacy = respx.get("https://example.com/.well-known/agent.json").mock(
        return_value=httpx.Response(200, json=_agent_card())
    )

    card = await discover_agent(AgentConfig(url="https://example.com"))

    assert card.name == "Test agent"
    assert legacy.called


@pytest.mark.asyncio
@respx.mock
async def test_custom_card_path_does_not_fall_back() -> None:
    respx.get("https://example.com/card.json").mock(return_value=httpx.Response(404))

    with pytest.raises(AgentCardResolutionError):
        await discover_agent(AgentConfig(url="https://example.com", card_path="/card.json"))
