from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from pydantic import JsonValue

from a2a_proof.models import DownstreamConfig

MAX_DOWNSTREAM_BODY_BYTES = 1_000_000
MAX_DOWNSTREAM_CALLS = 100
AGENT_CARD_PATH = "/.well-known/agent-card.json"
MESSAGE_PATH = "/a2a"
SEND_METHODS = {"SendMessage", "message/send"}
DOWNSTREAM_URL_PLACEHOLDER = "{{downstream_url}}"


@dataclass(frozen=True, slots=True)
class DownstreamCall:
    method: str
    text: str
    data: tuple[JsonValue, ...]
    headers: tuple[tuple[str, str], ...]
    body: str


class DownstreamAgent:
    def __init__(self, config: DownstreamConfig) -> None:
        self._config = config
        self._server: _DownstreamServer | None = None
        self._thread: threading.Thread | None = None
        self._url = str(config.public_url).rstrip("/") if config.public_url is not None else None

    async def __aenter__(self) -> DownstreamAgent:
        server = _DownstreamServer(
            (self._config.listen_host, self._config.listen_port),
            _DownstreamHandler,
        )
        server.agent_config = self._config
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        if self._url is None:
            host, port = server.server_address[:2]
            self._url = f"http://{host}:{port}"
        server.base_url = self._url
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._server is not None:
            await asyncio.to_thread(self._server.shutdown)
            self._server.server_close()
        if self._thread is not None:
            self._thread.join()
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("downstream agent is not running")
        return self._url

    def recorded(self) -> int:
        return len(self._require_server().calls)

    def calls_since(self, index: int) -> tuple[DownstreamCall, ...]:
        return tuple(self._require_server().calls[index:])

    def _require_server(self) -> _DownstreamServer:
        if self._server is None:
            raise RuntimeError("downstream agent is not running")
        return self._server


class _DownstreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    agent_config: DownstreamConfig
    base_url: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[DownstreamCall] = []
        self._lock = threading.Lock()

    def record(self, call: DownstreamCall) -> None:
        with self._lock:
            if len(self.calls) < MAX_DOWNSTREAM_CALLS:
                self.calls.append(call)


class _DownstreamHandler(BaseHTTPRequestHandler):
    server: _DownstreamServer

    def do_GET(self) -> None:
        if urlsplit(self.path).path != AGENT_CARD_PATH:
            self._send_json(404, {"error": "not found"})
            return
        config = self.server.agent_config
        self._send_json(
            200,
            {
                "name": config.name,
                "description": "Recording downstream agent operated by a2a-proof",
                "version": "1.0.0",
                "supportedInterfaces": [
                    {
                        "url": f"{self.server.base_url}{MESSAGE_PATH}",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
                "capabilities": {},
                "defaultInputModes": ["text/plain", "application/json"],
                "defaultOutputModes": ["text/plain", "application/json"],
                "skills": [
                    {
                        "id": skill,
                        "name": skill,
                        "description": f"Recording skill {skill}",
                        "examples": [],
                    }
                    for skill in config.skills
                ],
            },
        )

    def do_POST(self) -> None:
        if urlsplit(self.path).path != MESSAGE_PATH:
            self._send_json(404, {"error": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
            if not 0 < size <= MAX_DOWNSTREAM_BODY_BYTES:
                raise ValueError
            body = self.rfile.read(size).decode("utf-8")
            request: dict[str, Any] = json.loads(body)
            method = str(request["method"])
            request_id = request["id"]
        except (KeyError, TypeError, UnicodeError, ValueError):
            self._send_json(400, {"error": "invalid request"})
            return

        text, data = _message_content(request) if method in SEND_METHODS else ("", ())
        self.server.record(
            DownstreamCall(
                method=method,
                text=text,
                data=data,
                headers=tuple((name.lower(), value) for name, value in self.headers.items()),
                body=body,
            )
        )
        if method not in SEND_METHODS:
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "method not supported"},
                },
            )
            return
        self._send_json(200, _reply(self.server.agent_config, request, request_id))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _message_content(request: dict[str, Any]) -> tuple[str, tuple[JsonValue, ...]]:
    parts = request.get("params", {}).get("message", {}).get("parts", [])
    if not isinstance(parts, list):
        return "", ()
    texts = [part["text"] for part in parts if isinstance(part, dict) and "text" in part]
    data = [part["data"] for part in parts if isinstance(part, dict) and "data" in part]
    return "\n".join(str(text) for text in texts), tuple(data)


def _reply(config: DownstreamConfig, request: dict[str, Any], request_id: object) -> dict[str, Any]:
    context_id = request.get("params", {}).get("message", {}).get("contextId")
    parts: list[dict[str, Any]] = []
    if config.reply.text is not None:
        parts.append({"text": config.reply.text})
    if config.reply.data is not None:
        parts.append({"data": config.reply.data, "mediaType": "application/json"})
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "task": {
                "id": "a2a-proof-downstream-task",
                "contextId": context_id if isinstance(context_id, str) else "a2a-proof-downstream",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {
                        "artifactId": "downstream-reply",
                        "name": "downstream reply",
                        "parts": parts,
                    }
                ],
            }
        },
    }


def resolve_downstream_url(value: JsonValue, url: str) -> JsonValue:
    if isinstance(value, str):
        return value.replace(DOWNSTREAM_URL_PLACEHOLDER, url)
    if isinstance(value, dict):
        return {key: resolve_downstream_url(item, url) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_downstream_url(item, url) for item in value]
    return value
