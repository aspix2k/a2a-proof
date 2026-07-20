from __future__ import annotations

import asyncio
import base64
import json
import threading
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from a2a_proof.models import ProofConfig, SuiteResult
from a2a_proof.runner import run

_MAX_REQUEST_BYTES = 1_000_000
_RECEIPT = b"queue=billing-disputes\npriority=high\n"


class _DemoServer(ThreadingHTTPServer):
    daemon_threads = True


class _DemoHandler(BaseHTTPRequestHandler):
    server: _DemoServer

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/.well-known/agent-card.json":
            self._send_json(404, {"error": "not found"})
            return
        host, port = self.server.server_address[:2]
        self._send_json(
            200,
            {
                "name": "Support router demo",
                "description": "Routes a deterministic support ticket",
                "version": "1.0.0",
                "supportedInterfaces": [
                    {
                        "url": f"http://{host}:{port}/a2a",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
                "capabilities": {},
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["application/json", "text/plain"],
                "skills": [
                    {
                        "id": "route-support-ticket",
                        "name": "Route support ticket",
                        "description": "Chooses a queue and priority",
                        "examples": ["My card was charged twice"],
                    }
                ],
            },
        )

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/a2a":
            self._send_json(404, {"error": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
            if not 0 < size <= _MAX_REQUEST_BYTES:
                raise ValueError
            request: dict[str, Any] = json.loads(self.rfile.read(size))
            if request["jsonrpc"] != "2.0" or request["method"] != "SendMessage":
                raise ValueError
            message = request["params"]["message"]
            context_id = message["contextId"]
            request_id = request["id"]
            if not isinstance(context_id, str) or not context_id or not isinstance(request_id, str):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            self._send_json(400, {"error": "invalid request"})
            return

        self._send_json(
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "task": {
                        "id": "demo-task",
                        "contextId": context_id,
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [
                            {
                                "artifactId": "routing",
                                "name": "routing decision",
                                "parts": [
                                    {
                                        "data": {
                                            "queue": "billing-disputes",
                                            "priority": "high",
                                        },
                                        "mediaType": "application/json",
                                    },
                                    {
                                        "raw": base64.b64encode(_RECEIPT).decode("ascii"),
                                        "filename": "routing.txt",
                                        "mediaType": "text/plain",
                                    },
                                ],
                            }
                        ],
                    }
                },
            },
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def run_demo(*, intentional_failure: bool = False) -> SuiteResult:
    server = _DemoServer(("127.0.0.1", 0), _DemoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        expected_queue = "general-support" if intentional_failure else "billing-disputes"
        config = ProofConfig.model_validate(
            {
                "version": 1,
                "agent": {"url": f"http://{host}:{port}", "timeout": 30},
                "scenarios": [
                    {
                        "name": "billing dispute routing",
                        "message": "A customer says their card was charged twice for order 4815.",
                        "expect": {
                            "state": "completed",
                            "data": [
                                {"path": "/queue", "equals": expected_queue},
                                {"path": "/priority", "equals": "high"},
                            ],
                            "files": {
                                "source": "artifact",
                                "artifact_name": "routing decision",
                                "filename": "routing.txt",
                                "media_type": "text/plain",
                                "kind": "raw",
                                "size_bytes": len(_RECEIPT),
                                "sha256": sha256(_RECEIPT).hexdigest(),
                            },
                        },
                    }
                ],
            }
        )
        return await run(config, _trust_env=False)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()
