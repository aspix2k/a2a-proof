from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import grpc
import httpx
import pytest
import respx
from a2a.helpers import get_data_parts
from a2a.types import Message, Part, Role, SendMessageRequest, SendMessageResponse, a2a_pb2_grpc
from click.testing import CliRunner

from a2a_proof.cli import main
from a2a_proof.models import ProofConfig
from a2a_proof.runner import run

TEST_EXTENSION = "https://example.com/extensions/structured-input/v1"


class _AgentHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def do_GET(self) -> None:
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
                    "streaming": False,
                    "extensions": [{"uri": TEST_EXTENSION}],
                },
                "defaultInputModes": ["text/plain", "application/json"],
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
        if self.path not in {"/a2a", "/message:send"}:
            self._send_json(404, {"error": "not found"})
            return
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        payload = request["params"] if self.path == "/a2a" else request
        message = payload["message"]
        text = "".join(part.get("text", "") for part in message["parts"])
        data = [part["data"] for part in message["parts"] if "data" in part]
        if data and self.headers.get("A2A-Extensions") != TEST_EXTENSION:
            self._send_json(400, {"error": "missing extension activation"})
            return
        if data and data[0].get("action") == "forecast":
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

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _GrpcAgent(a2a_pb2_grpc.A2AServiceServicer):
    def __init__(self) -> None:
        self.metadata: dict[str, str] = {}

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


@pytest.fixture
def agent_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentHandler)
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
                        "data": [
                            {
                                "source": "artifact",
                                "artifact_name": "forecast",
                                "media_type": "application/json",
                                "path": "/city",
                                "equals": "Paris",
                            },
                            {"path": "/temperature", "equals": 21},
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


@pytest.mark.asyncio
@respx.mock
async def test_runs_real_grpc_exchange() -> None:
    server = grpc.aio.server()
    service = _GrpcAgent()
    a2a_pb2_grpc.add_A2AServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    respx.get("https://grpc.example/.well-known/agent-card.json").mock(
        return_value=httpx.Response(
            200,
            json={
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
                    "streaming": False,
                    "extensions": [{"uri": TEST_EXTENSION}],
                },
                "defaultInputModes": ["text/plain", "application/json"],
                "defaultOutputModes": ["text/plain"],
                "skills": [],
            },
        )
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
                }
            ],
        }
    )

    try:
        assert (await run(config)).passed
        assert service.metadata["authorization"] == "Bearer secret"
        assert service.metadata["a2a-extensions"] == TEST_EXTENSION
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
