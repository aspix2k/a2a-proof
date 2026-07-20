from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import grpc
import httpx
import pytest
import respx
from a2a.helpers import get_data_parts
from a2a.server.context import ServerCallContext
from a2a.server.tasks.base_push_notification_sender import BasePushNotificationSender
from a2a.server.tasks.inmemory_push_notification_config_store import (
    InMemoryPushNotificationConfigStore,
)
from a2a.types import (
    Artifact,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    SendMessageResponse,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    a2a_pb2_grpc,
)
from click.testing import CliRunner

from a2a_proof.cli import main
from a2a_proof.models import ProofConfig, PushNotificationsConfig
from a2a_proof.push import PushReceiver
from a2a_proof.runner import run

TEST_EXTENSION = "https://example.com/extensions/structured-input/v1"


class _AgentServer(ThreadingHTTPServer):
    task_state = "TASK_STATE_WORKING"
    supports_streaming = False


class _AgentHandler(BaseHTTPRequestHandler):
    server: _AgentServer

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/tasks/task":
            self._send_json(200, self._task())
            return
        if self.path != "/.well-known/agent-card.json":
            self._send_json(404, {"error": "not found"})
            return
        host = str(self.server.server_address[0])
        port = int(self.server.server_address[1])
        self._send_json(
            200,
            {
                "name": "Echo agent",
                "description": "Echoes text",
                "version": "1.0.0",
                "supportedInterfaces": [
                    {
                        "url": f"http://{host}:{port}/a2a",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    },
                    {
                        "url": f"http://{host}:{port}",
                        "protocolBinding": "HTTP+JSON",
                        "protocolVersion": "1.0",
                    },
                ],
                "capabilities": {
                    "streaming": getattr(self.server, "supports_streaming", False),
                    "pushNotifications": True,
                    "extensions": [{"uri": TEST_EXTENSION}],
                },
                "defaultInputModes": ["text/plain", "application/json", "application/pdf"],
                "defaultOutputModes": ["text/plain"],
                "skills": [
                    {
                        "id": "echo",
                        "name": "Echo",
                        "description": "Echo text",
                        "examples": ["Hello"],
                    }
                ],
            },
        )

    def do_POST(self) -> None:
        if self._handle_rest_task_operation():
            return
        if self.path not in {"/a2a", "/message:send"}:
            self._send_json(404, {"error": "not found"})
            return
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        if self._handle_jsonrpc_task_operation(request):
            return
        payload = request["params"] if self.path == "/a2a" else request
        push_config = payload.get("configuration", {}).get("taskPushNotificationConfig")
        message = payload["message"]
        text = "".join(part.get("text", "") for part in message["parts"])
        data = [part["data"] for part in message["parts"] if "data" in part]
        files = [part for part in message["parts"] if "raw" in part]
        if data and self.headers.get("A2A-Extensions") != TEST_EXTENSION:
            self._send_json(400, {"error": "missing extension activation"})
            return
        if payload.get("configuration", {}).get("returnImmediately"):
            self.server.task_state = "TASK_STATE_WORKING"
            result = {"task": self._task()}
        elif files:
            result = {
                "task": {
                    "id": "task",
                    "contextId": message["contextId"],
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {
                            "artifactId": "result",
                            "name": "processed file",
                            "parts": [
                                {
                                    "url": "https://files.example/result?token=secret",
                                    "filename": files[0]["filename"],
                                    "mediaType": files[0]["mediaType"],
                                }
                            ],
                        }
                    ],
                }
            }
        elif data and data[0].get("action") == "forecast":
            result = {
                "task": {
                    "id": "task",
                    "contextId": message["contextId"],
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {
                            "artifactId": "result",
                            "name": "forecast",
                            "parts": [
                                {
                                    "data": {"city": "Paris", "temperature": 21},
                                    "mediaType": "application/json",
                                }
                            ],
                        }
                    ],
                }
            }
        elif data:
            result = {
                "message": {
                    "messageId": "response",
                    "contextId": message["contextId"],
                    "role": "ROLE_AGENT",
                    "parts": [{"text": f"data: {data[0]['value']}"}],
                }
            }
        else:
            result = {
                "message": {
                    "messageId": "response",
                    "contextId": message["contextId"],
                    "role": "ROLE_AGENT",
                    "parts": [{"text": f"echo: {text}"}],
                }
            }
        if self.path == "/a2a":
            result = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        self._send_json(200, result)
        if push_config is not None:
            authentication = push_config["authentication"]
            response = httpx.post(
                push_config["url"],
                headers={
                    "Authorization": (f"{authentication['scheme']} {authentication['credentials']}")
                },
                json={
                    "statusUpdate": {
                        "taskId": "task",
                        "contextId": "context",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                    }
                },
            )
            response.raise_for_status()

    def _handle_rest_task_operation(self) -> bool:
        if self.path == "/tasks/task:cancel":
            self.server.task_state = "TASK_STATE_CANCELED"
            self._send_json(200, self._task())
            return True
        if self.path == "/tasks/task:subscribe":
            self._send_sse(self._subscription_events())
            return True
        return False

    def _handle_jsonrpc_task_operation(self, request: dict[str, object]) -> bool:
        if self.path != "/a2a":
            return False
        if request["method"] == "SubscribeToTask":
            self._send_sse(
                [
                    {"jsonrpc": "2.0", "id": request["id"], "result": event}
                    for event in self._subscription_events()
                ]
            )
            return True
        if request["method"] not in {"CancelTask", "GetTask"}:
            return False
        if request["method"] == "CancelTask":
            self.server.task_state = "TASK_STATE_CANCELED"
        self._send_json(
            200,
            {"jsonrpc": "2.0", "id": request["id"], "result": self._task()},
        )
        return True

    def _task(self) -> dict[str, object]:
        state = getattr(self.server, "task_state", "TASK_STATE_WORKING")
        return {
            "id": "task",
            "contextId": "context",
            "status": {"state": state},
            "history": [
                {
                    "messageId": "stored",
                    "contextId": "context",
                    "taskId": "task",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "stored"}],
                }
            ],
        }

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _subscription_events(self) -> list[dict[str, object]]:
        return [
            {"task": self._task()},
            {
                "artifactUpdate": {
                    "taskId": "task",
                    "contextId": "context",
                    "artifact": {
                        "artifactId": "result",
                        "name": "report",
                        "parts": [
                            {
                                "raw": "cmVwb3J0",
                                "filename": "report.txt",
                                "mediaType": "text/plain",
                            }
                        ],
                    },
                }
            },
            {
                "statusUpdate": {
                    "taskId": "task",
                    "contextId": "context",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {
                            "messageId": "finished",
                            "contextId": "context",
                            "taskId": "task",
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "finished"}],
                        },
                    },
                }
            },
        ]

    def _send_sse(self, events: Sequence[object]) -> None:
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _GrpcAgent(a2a_pb2_grpc.A2AServiceServicer):
    def __init__(self) -> None:
        self.metadata: dict[str, str] = {}
        self.task_state = TaskState.TASK_STATE_WORKING

    async def SendMessage(
        self,
        request: SendMessageRequest,
        context: grpc.aio.ServicerContext,
    ) -> SendMessageResponse:
        metadata = cast(
            Sequence[tuple[str, str | bytes]],
            context.invocation_metadata() or (),
        )
        self.metadata = {key: value for key, value in metadata if isinstance(value, str)}
        if request.configuration.return_immediately:
            self.task_state = TaskState.TASK_STATE_WORKING
            return SendMessageResponse(task=self._task())
        text = "".join(part.text for part in request.message.parts if part.HasField("text"))
        data = get_data_parts(request.message.parts)
        response = f"data: {data[0]['value']}" if data else f"echo: {text}"
        return SendMessageResponse(
            message=Message(
                message_id="response",
                context_id=request.message.context_id,
                role=Role.ROLE_AGENT,
                parts=[Part(text=response)],
            )
        )

    async def CancelTask(
        self,
        request: CancelTaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> Task:
        self.task_state = TaskState.TASK_STATE_CANCELED
        return self._task()

    async def GetTask(
        self,
        request: GetTaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> Task:
        return self._task()

    async def SubscribeToTask(
        self,
        request: SubscribeToTaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[StreamResponse]:
        yield StreamResponse(task=self._task())
        yield StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                task_id=request.id,
                context_id="context",
                artifact=Artifact(
                    artifact_id="result",
                    name="report",
                    parts=[
                        Part(
                            raw=b"report",
                            filename="report.txt",
                            media_type="text/plain",
                        )
                    ],
                ),
            )
        )
        self.task_state = TaskState.TASK_STATE_COMPLETED
        yield StreamResponse(
            status_update=TaskStatusUpdateEvent(
                task_id=request.id,
                context_id="context",
                status=TaskStatus(
                    state=self.task_state,
                    message=Message(
                        message_id="finished",
                        context_id="context",
                        task_id=request.id,
                        role=Role.ROLE_AGENT,
                        parts=[Part(text="finished")],
                    ),
                ),
            )
        )

    def _task(self) -> Task:
        return Task(
            id="task",
            context_id="context",
            status=TaskStatus(state=self.task_state),
            history=[
                Message(
                    message_id="stored",
                    context_id="context",
                    task_id="task",
                    role=Role.ROLE_AGENT,
                    parts=[Part(text="stored")],
                )
            ],
        )


def _grpc_agent_card(port: int, *, streaming: bool) -> dict[str, object]:
    return {
        "name": "gRPC echo",
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": f"127.0.0.1:{port}",
                "protocolBinding": "GRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": streaming,
            "extensions": [{"uri": TEST_EXTENSION}],
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }


def _subscription_config(
    url: str,
    transport: str,
    *,
    grpc_tls: bool = True,
    allow_cross_origin_interfaces: bool = False,
) -> ProofConfig:
    return ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {
                "url": url,
                "transport": transport,
                "grpc_tls": grpc_tls,
                "allow_cross_origin_interfaces": allow_cross_origin_interfaces,
            },
            "scenarios": [
                {
                    "name": "resume report",
                    "turns": [
                        {
                            "message": "Start a report",
                            "return_immediately": True,
                            "expect": {"state": "working"},
                        },
                        {
                            "action": "subscribe",
                            "expect": {
                                "state": "completed",
                                "states": {"contains_in_order": ["working", "completed"]},
                                "text": {"contains": "finished"},
                                "files": {
                                    "source": "artifact",
                                    "artifact_name": "report",
                                    "filename": "report.txt",
                                    "media_type": "text/plain",
                                    "kind": "raw",
                                    "size_bytes": 6,
                                    "sha256": (
                                        "845e91831319e89c4d656bdb80c278ac09a7230d61e5dfd2e1b1fbb436ac8917"
                                    ),
                                },
                            },
                        },
                    ],
                }
            ],
        }
    )


@pytest.fixture
def agent_url() -> Iterator[str]:
    server = _AgentServer(("127.0.0.1", 0), _AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def streaming_agent_url() -> Iterator[str]:
    server = _AgentServer(("127.0.0.1", 0), _AgentHandler)
    server.supports_streaming = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_runs_real_jsonrpc_exchange(agent_url: str) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": agent_url},
            "scenarios": [
                {
                    "name": "echo",
                    "message": "Hello",
                    "expect": {"state": "message", "text": {"equals": "echo: Hello"}},
                }
            ],
        }
    )

    result = await run(config)

    assert result.passed
    assert result.scenarios[0].trials[0].turns[0].text == "echo: Hello"


@pytest.mark.asyncio
async def test_runs_real_http_json_exchange(agent_url: str) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {
                "url": agent_url,
                "transport": "HTTP+JSON",
                "extensions": [TEST_EXTENSION],
            },
            "scenarios": [
                {
                    "name": "structured echo",
                    "data": {"value": "Hello"},
                    "expect": {"text": {"equals": "data: Hello"}},
                }
            ],
        }
    )

    assert (await run(config)).passed


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["JSONRPC", "HTTP+JSON"])
async def test_runs_task_lifecycle_contract_over_real_http_transports(
    agent_url: str,
    transport: str,
) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": agent_url, "transport": transport},
            "scenarios": [
                {
                    "name": "cancel and retrieve",
                    "turns": [
                        {
                            "message": "Start a long task",
                            "return_immediately": True,
                            "expect": {"state": "working"},
                        },
                        {"action": "cancel", "expect": {"state": "canceled"}},
                        {
                            "action": "get_task",
                            "history_length": 5,
                            "expect": {
                                "state": "canceled",
                                "text": {"contains": "stored"},
                            },
                        },
                    ],
                }
            ],
        }
    )

    assert (await run(config)).passed


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["JSONRPC", "HTTP+JSON"])
async def test_runs_task_subscription_over_real_http_transports(
    streaming_agent_url: str,
    transport: str,
) -> None:
    assert (await run(_subscription_config(streaming_agent_url, transport))).passed


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["JSONRPC", "HTTP+JSON"])
async def test_runs_push_contract_over_real_http_transports(
    agent_url: str,
    transport: str,
) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": agent_url, "transport": transport},
            "push_notifications": {},
            "scenarios": [
                {
                    "name": "async completion",
                    "turns": [
                        {
                            "message": "Start a long task",
                            "return_immediately": True,
                            "push_notification": True,
                            "expect": {"state": "working"},
                        },
                        {
                            "action": "await_push",
                            "expect": {"state": "completed"},
                        },
                    ],
                }
            ],
        }
    )

    result = await run(config)

    assert result.passed
    assert result.scenarios[0].trials[0].turns[1].state == "completed"


@pytest.mark.asyncio
async def test_accepts_notifications_from_official_sdk_sender() -> None:
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        subscription.bind(task_id="task", context_id="context")
        store = InMemoryPushNotificationConfigStore(owner_resolver=lambda _context: "owner")
        await store.set_info(
            "task",
            TaskPushNotificationConfig(
                task_id="task",
                url=subscription.target.url,
                token=subscription.target.token,
            ),
            cast(ServerCallContext, object()),
        )
        async with httpx.AsyncClient() as client:
            sender = BasePushNotificationSender(client, store)
            await sender.send_notification(
                "task",
                TaskStatusUpdateEvent(
                    task_id="task",
                    context_id="context",
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                ),
            )
        assert (await subscription.wait(1)).state == "completed"


@pytest.mark.asyncio
async def test_runs_structured_artifact_contract_over_real_jsonrpc(agent_url: str) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": agent_url, "extensions": [TEST_EXTENSION]},
            "scenarios": [
                {
                    "name": "forecast",
                    "data": {"action": "forecast", "city": "Paris"},
                    "expect": {
                        "state": "completed",
                        "max_first_event_seconds": 5,
                        "data": [
                            {
                                "source": "artifact",
                                "artifact_name": "forecast",
                                "media_type": "application/json",
                                "path": "/city",
                                "matches": "^Par",
                            },
                            {"path": "/temperature", "gte": 20, "lt": 22},
                            {"path": "/alerts", "exists": False},
                            {
                                "json_schema": {
                                    "type": "object",
                                    "required": ["city", "temperature"],
                                    "properties": {
                                        "city": {"type": "string"},
                                        "temperature": {"type": "number"},
                                    },
                                    "additionalProperties": False,
                                }
                            },
                        ],
                    },
                }
            ],
        }
    )

    result = await run(config)

    assert result.passed
    data = result.scenarios[0].trials[0].turns[0].data
    assert data[0].value == {"city": "Paris", "temperature": 21.0}
    assert data[0].artifact_id == "result"
    assert result.scenarios[0].trials[0].turns[0].first_event_ms is not None


@pytest.mark.asyncio
async def test_runs_file_and_agent_card_contract_over_real_jsonrpc(
    agent_url: str,
    tmp_path: Path,
) -> None:
    (tmp_path / "report.pdf").write_bytes(b"report")
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": agent_url},
            "card": {
                "skills": {"contains": "echo"},
                "capabilities": {"streaming": False},
                "input_modes": {"contains": "application/pdf"},
                "output_modes": {"contains": "text/plain"},
            },
            "scenarios": [
                {
                    "name": "process file",
                    "files": ["report.pdf"],
                    "expect": {
                        "state": "completed",
                        "states": {"equals": ["completed"]},
                        "files": {
                            "source": "artifact",
                            "artifact_name": "processed file",
                            "filename": "report.pdf",
                            "media_type": "application/pdf",
                            "kind": "url",
                            "count": 1,
                        },
                    },
                }
            ],
        }
    )
    config.bind_contract_dir(tmp_path)

    result = await run(config)

    assert result.passed
    turn = result.scenarios[0].trials[0].turns[0]
    assert turn.states == ["completed"]
    assert turn.files[0].filename == "report.pdf"
    assert "secret" not in result.model_dump_json()


@pytest.mark.asyncio
@respx.mock
async def test_runs_real_grpc_exchange() -> None:
    server = grpc.aio.server()
    service = _GrpcAgent()
    a2a_pb2_grpc.add_A2AServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    respx.get("https://grpc.example/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json=_grpc_agent_card(port, streaming=False))
    )
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {
                "url": "https://grpc.example",
                "transport": "GRPC",
                "grpc_tls": False,
                "allow_cross_origin_interfaces": True,
                "headers": {"Authorization": "Bearer secret"},
                "extensions": [TEST_EXTENSION],
            },
            "scenarios": [
                {
                    "name": "structured echo",
                    "data": {"value": "Hello"},
                    "expect": {"text": {"equals": "data: Hello"}},
                },
                {
                    "name": "cancel and retrieve",
                    "turns": [
                        {
                            "message": "Start a long task",
                            "return_immediately": True,
                            "expect": {"state": "working"},
                        },
                        {"action": "cancel", "expect": {"state": "canceled"}},
                        {
                            "action": "get_task",
                            "history_length": 5,
                            "expect": {
                                "state": "canceled",
                                "text": {"contains": "stored"},
                            },
                        },
                    ],
                },
            ],
        }
    )

    try:
        assert (await run(config)).passed
        assert service.metadata["authorization"] == "Bearer secret"
        assert service.metadata["a2a-extensions"] == TEST_EXTENSION
    finally:
        await server.stop(None)


@pytest.mark.asyncio
@respx.mock
async def test_runs_task_subscription_over_real_grpc() -> None:
    server = grpc.aio.server()
    a2a_pb2_grpc.add_A2AServiceServicer_to_server(_GrpcAgent(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    respx.get("https://grpc-stream.example/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json=_grpc_agent_card(port, streaming=True))
    )
    config = _subscription_config(
        "https://grpc-stream.example",
        "GRPC",
        grpc_tls=False,
        allow_cross_origin_interfaces=True,
    )

    try:
        assert (await run(config)).passed
    finally:
        await server.stop(None)


def test_cli_init_then_run_against_real_agent(agent_url: str, tmp_path: Path) -> None:
    config = tmp_path / "a2a-proof.yaml"
    runner = CliRunner()

    initialized = runner.invoke(main, ["init", agent_url, "--output", str(config)])
    executed = runner.invoke(main, ["run", str(config)])

    assert initialized.exit_code == 0
    assert "message: Hello" in config.read_text(encoding="utf-8")
    assert executed.exit_code == 0
    assert "PASS" in executed.output


def test_cli_diff_against_real_agent(agent_url: str, tmp_path: Path) -> None:
    config = tmp_path / "a2a-proof.yaml"
    config.write_text(
        "\n".join(
            [
                "version: 1",
                "agent:",
                f"  url: {agent_url}",
                "scenarios:",
                "  - name: echo",
                "    message: Hello",
                "    expect:",
                "      text:",
                "        equals: 'echo: Hello'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        ["diff", str(config), "--against", agent_url],
    )

    assert result.exit_code == 0
    assert "unchanged" in result.output.lower()
    assert "echo" in result.output
