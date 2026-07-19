from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from a2a.client.client import Client, ClientCallContext
from a2a.client.errors import AgentCardResolutionError
from a2a.helpers import get_message_text
from a2a.types import (
    AgentCard,
    AgentInterface,
    Artifact,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
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
    _grpc_target,
    _protocol_bindings,
    _validate_card_interfaces,
    discover_agent,
)
from a2a_proof.models import AgentConfig
from a2a_proof.protocol import ProtocolError, ResponseCollector


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
    def __init__(self, responses: list[StreamResponse], *, delay: float = 0) -> None:
        self.responses = responses
        self.delay = delay
        self.request: SendMessageRequest | None = None
        self.context: ClientCallContext | None = None
        self.closed = False

    async def send_message(
        self,
        request,
        *,
        context: ClientCallContext | None = None,
    ) -> AsyncIterator[StreamResponse]:
        self.request = request
        self.context = context
        if self.delay:
            await asyncio.sleep(self.delay)
        for response in self.responses:
            yield response

    async def close(self) -> None:
        self.closed = True


def test_collects_direct_message() -> None:
    collector = ResponseCollector("initial")
    collector.add(StreamResponse(message=_agent_message("Hello")))

    outcome = collector.finish(duration_ms=12)

    assert outcome.state == "message"
    assert outcome.text == "Hello"
    assert outcome.context_id == "context"
    assert outcome.task_id is None
    assert outcome.duration_ms == 12


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


@pytest.mark.asyncio
async def test_session_sends_context_and_closes_client() -> None:
    client = _FakeClient([StreamResponse(message=_agent_message("Hello"))])
    session = A2ASession(
        cast(Client, client),
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
    assert client.request is not None
    assert client.context is not None
    assert client.request.message.context_id == "context"
    assert client.request.message.task_id == "task"
    assert get_message_text(client.request.message) == "Hi"
    assert client.context.timeout == 2
    assert client.context.service_parameters == {"Authorization": "Bearer secret"}
    assert client.closed


@pytest.mark.asyncio
async def test_session_reports_timeout() -> None:
    client = _FakeClient([], delay=0.05)
    session = A2ASession(cast(Client, client), timeout=0.001)

    with pytest.raises(ProtocolError, match="did not finish within"):
        await session.send_turn("Hi", context_id="context", task_id=None)


@pytest.mark.asyncio
@respx.mock
async def test_connect_builds_client_for_discovered_card() -> None:
    respx.get("https://example.com/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json=_agent_card())
    )

    session = await A2ASession.connect(AgentConfig(url="https://example.com"))
    await session.close()


@pytest.mark.asyncio
async def test_connect_closes_http_client_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    http_client = AsyncMock()

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(a2a_module, "_http_client", lambda config: http_client)
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

    monkeypatch.setattr(a2a_module, "_http_client", lambda config: http_client)
    monkeypatch.setattr(a2a_module, "_resolve_card", cancel)

    with pytest.raises(asyncio.CancelledError):
        await A2ASession.connect(AgentConfig(url="https://example.com"))
    http_client.aclose.assert_awaited_once()


def test_maps_supported_protocol_bindings() -> None:
    assert _protocol_bindings("JSONRPC") == ["JSONRPC"]
    assert _protocol_bindings("HTTP+JSON") == ["HTTP+JSON"]
    assert _protocol_bindings("GRPC") == ["GRPC"]
    assert _protocol_bindings("auto") == ["JSONRPC", "HTTP+JSON", "GRPC"]


def test_normalizes_and_validates_grpc_targets() -> None:
    assert _grpc_target("https://agent.example.com:443") == "agent.example.com:443"
    assert _grpc_target("dns:///agent.example.com:443") == "dns:///agent.example.com:443"

    with pytest.raises(ProtocolError, match="must not contain a path"):
        _grpc_target("grpcs://agent.example.com/service")
    with pytest.raises(ProtocolError, match="invalid gRPC interface URL"):
        _grpc_target("https://user:password@agent.example.com")


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
