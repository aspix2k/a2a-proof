import base64
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml
from click.testing import CliRunner

import a2a_proof.cli as cli_module
import a2a_proof.demo as demo_module
from a2a_proof.cli import main
from a2a_proof.demo import _MAX_REQUEST_BYTES, _RECEIPT, _DemoHandler, _DemoServer, run_demo
from a2a_proof.models import ScenarioResult, SuiteResult


@contextmanager
def demo_agent() -> Iterator[str]:
    server = _DemoServer(("127.0.0.1", 0), _DemoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_demo_runs_real_contract() -> None:
    result = CliRunner().invoke(main, ["demo"])

    assert result.exit_code == 0
    assert "PASS" in result.output
    assert "billing dispute routing" in result.output
    assert "1 scenario passed" in result.output


def test_demo_can_show_failure_diagnostics() -> None:
    result = CliRunner().invoke(main, ["demo", "--fail"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "billing dispute routing" in result.output
    assert "expected structured data at '/queue' to equal" in result.output
    assert '"general-support"' in result.output
    assert '"billing-disputes"' in result.output
    assert "1 scenario failed" in result.output


def test_demo_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    result = CliRunner().invoke(main, ["demo"])

    assert result.exit_code == 0
    assert "1 scenario passed" in result.output


def test_demo_reports_setup_errors_without_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*, intentional_failure: bool):
        assert intentional_failure is False
        raise RuntimeError("demo could not start")

    monkeypatch.setattr(cli_module, "run_demo", fail)

    result = CliRunner().invoke(main, ["demo"])

    assert result.exit_code == 2
    assert result.output == "Error: demo could not start\n"


@pytest.mark.parametrize(("passed", "exit_code"), [(True, 0), (False, 1)])
def test_demo_renders_runner_result(
    monkeypatch: pytest.MonkeyPatch,
    passed: bool,
    exit_code: int,
) -> None:
    async def result(*, intentional_failure: bool) -> SuiteResult:
        assert intentional_failure is False
        scenario = ScenarioResult(
            name="demo",
            passed=passed,
            passed_trials=int(passed),
            required_trials=1,
            trials=[],
        )
        return SuiteResult(passed=passed, duration_ms=1, scenarios=[scenario])

    monkeypatch.setattr(cli_module, "run_demo", result)

    invocation = CliRunner().invoke(main, ["demo"])

    assert invocation.exit_code == exit_code
    assert ("1 scenario passed" if passed else "1 scenario failed") in invocation.output


def test_demo_agent_rejects_unknown_routes() -> None:
    with demo_agent() as url:
        get_response = httpx.get(f"{url}/unknown")
        post_response = httpx.post(f"{url}/unknown", content=b"{}")

    assert get_response.status_code == 404
    assert get_response.json() == {"error": "not found"}
    assert post_response.status_code == 404
    assert post_response.json() == {"error": "not found"}


def test_demo_agent_returns_structured_and_file_artifacts() -> None:
    with demo_agent() as url:
        response = httpx.post(
            f"{url}/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "request-1",
                "method": "SendMessage",
                "params": {"message": {"contextId": "context-1"}},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "request-1"
    task = payload["result"]["task"]
    assert task["id"] == "demo-task"
    assert task["contextId"] == "context-1"
    assert task["status"] == {"state": "TASK_STATE_COMPLETED"}
    artifact = task["artifacts"][0]
    assert artifact["artifactId"] == "routing"
    assert artifact["name"] == "routing decision"
    assert artifact["parts"][0] == {
        "data": {"queue": "billing-disputes", "priority": "high"},
        "mediaType": "application/json",
    }
    assert artifact["parts"][1]["filename"] == "routing.txt"
    assert artifact["parts"][1]["mediaType"] == "text/plain"
    assert base64.b64decode(artifact["parts"][1]["raw"], validate=True) == _RECEIPT


@pytest.mark.parametrize(
    ("body", "content_length"),
    [
        (b"", "0"),
        (b"{", "1"),
        (b"{}", "2"),
        (b"[]", "2"),
    ],
)
def test_demo_agent_rejects_invalid_requests(body: bytes, content_length: str) -> None:
    with demo_agent() as url:
        response = httpx.post(
            f"{url}/a2a",
            content=body,
            headers={"Content-Length": content_length},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid request"}


@pytest.mark.parametrize(
    "payload",
    [
        {
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "UnknownMethod",
            "params": {"message": {"contextId": "context-1"}},
        },
        {
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "SendMessage",
            "params": {"message": {"contextId": ""}},
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {"message": {"contextId": "context-1"}},
        },
    ],
)
def test_demo_agent_rejects_wrong_jsonrpc_shape(payload: object) -> None:
    with demo_agent() as url:
        response = httpx.post(f"{url}/a2a", json=payload)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid request"}


def test_demo_agent_rejects_oversized_request_before_reading_body() -> None:
    with demo_agent() as url:
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        assert parsed.port is not None
        with socket.create_connection((parsed.hostname, parsed.port)) as connection:
            connection.sendall(
                (
                    "POST /a2a HTTP/1.1\r\n"
                    f"Host: {parsed.netloc}\r\n"
                    f"Content-Length: {_MAX_REQUEST_BYTES + 1}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            response = connection.recv(4_096)

    assert response.startswith(b"HTTP/1.0 400 Bad Request\r\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intentional_failure", "expected_queue"),
    [(False, "billing-disputes"), (True, "general-support")],
)
async def test_run_demo_builds_bounded_contract(
    monkeypatch: pytest.MonkeyPatch,
    intentional_failure: bool,
    expected_queue: str,
) -> None:
    expected_result = SuiteResult(passed=True, duration_ms=1, scenarios=[])

    async def inspect(config, *, max_parallel_trials=1, _trust_env=True):
        assert max_parallel_trials == 1
        assert _trust_env is False
        assert str(config.agent.url).startswith("http://127.0.0.1:")
        assert config.agent.timeout == 30
        scenario = config.scenarios[0]
        assert scenario.name == "billing dispute routing"
        assert scenario.expect.state == "completed"
        assert scenario.expect.data[0].path == "/queue"
        assert scenario.expect.data[0].equals == expected_queue
        assert scenario.expect.data[1].path == "/priority"
        assert scenario.expect.data[1].equals == "high"
        file = scenario.expect.files[0]
        assert file.source == "artifact"
        assert file.artifact_name == "routing decision"
        assert file.filename == "routing.txt"
        assert file.media_type == "text/plain"
        assert file.kind == "raw"
        assert file.size_bytes == len(_RECEIPT)
        assert file.sha256 == "e0a09923f9c026cd5c93c497740dad0e74e93aa750aa49dd14bb486baf5ba392"
        return expected_result

    monkeypatch.setattr(demo_module, "run", inspect)

    assert await run_demo(intentional_failure=intentional_failure) is expected_result


def test_action_metadata_keeps_inputs_and_execution_bounded() -> None:
    action = yaml.safe_load((Path(__file__).parents[1] / "action.yml").read_text())
    setup_step = action["runs"]["steps"][0]
    run_step = action["runs"]["steps"][1]

    assert action["runs"]["using"] == "composite"
    assert action["inputs"] == {
        "config": {
            "description": "Path to the a2a-proof contract",
            "required": False,
            "default": "a2a-proof.yaml",
        }
    }
    assert setup_step["uses"] == ("astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990")
    assert setup_step["with"] == {
        "enable-cache": False,
        "python-version": "3.11",
        "version": "0.11.29",
    }
    assert run_step["env"] == {"A2A_PROOF_CONFIG": "${{ inputs.config }}"}
    assert "${{ inputs.config }}" not in run_step["run"]
    assert '-- "$A2A_PROOF_CONFIG"' in run_step["run"]
