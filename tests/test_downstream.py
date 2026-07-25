from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import a2a_proof.downstream as downstream_module
from a2a_proof.assertions import evaluate_delegation
from a2a_proof.downstream import (
    AGENT_CARD_PATH,
    MESSAGE_PATH,
    DownstreamAgent,
    DownstreamCall,
    resolve_downstream_url,
)
from a2a_proof.models import DelegationExpectation, DownstreamConfig, ProofConfig
from a2a_proof.protocol import TurnOutcome
from a2a_proof.runner import run_with_sender

SECRET = "Bearer super-secret"


def _config(scenarios: list[dict[str, object]], **downstream: object) -> ProofConfig:
    return ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "downstream": {"reply": {"text": "12 units", "data": {"units": 12}}, **downstream},
            "scenarios": scenarios,
        }
    )


def _downstream(config: ProofConfig) -> DownstreamAgent:
    assert config.downstream is not None
    return DownstreamAgent(config.downstream)


def _delegating_sender(*, forward_secret: bool = False, calls: int = 1):
    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        url = str(message).split("at ")[1]
        headers = {"Authorization": SECRET} if forward_secret else {}
        async with httpx.AsyncClient(trust_env=False) as client:
            for index in range(calls):
                await client.post(
                    f"{url}{MESSAGE_PATH}",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": f"downstream-{index}",
                        "method": "SendMessage",
                        "params": {
                            "message": {
                                "messageId": "1",
                                "contextId": "downstream-context",
                                "parts": [
                                    {"text": "How many units of SKU-42 are left?"},
                                    {"data": {"sku": "SKU-42"}},
                                ],
                            }
                        },
                    },
                )
        return TurnOutcome(
            state="completed",
            text="12 units are left",
            task_id=None,
            context_id=str(context["context_id"]),
            duration_ms=1,
        )

    return send_turn


@pytest.mark.asyncio
async def test_serves_a_card_and_records_a_delegated_call() -> None:
    async with DownstreamAgent(
        DownstreamConfig.model_validate({"skills": ["lookup"], "reply": {"text": "12 units"}})
    ) as agent:
        async with httpx.AsyncClient(trust_env=False) as client:
            card = (await client.get(f"{agent.url}{AGENT_CARD_PATH}")).json()
            response = await client.post(
                f"{agent.url}{MESSAGE_PATH}",
                headers={"Authorization": SECRET},
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "contextId": "context-1",
                            "parts": [{"text": "stock?"}, {"data": {"sku": "SKU-42"}}],
                        }
                    },
                },
            )
            missing = await client.get(f"{agent.url}/nope")

        call = agent.calls_since(0)[0]

    assert card["supportedInterfaces"][0]["url"] == f"{agent.url}{MESSAGE_PATH}"
    assert [skill["id"] for skill in card["skills"]] == ["lookup"]
    assert missing.status_code == 404
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["contextId"] == "context-1"
    assert task["artifacts"][0]["parts"] == [{"text": "12 units"}]
    assert (call.method, call.text, call.data) == ("SendMessage", "stock?", ({"sku": "SKU-42"},))
    assert ("authorization", SECRET) in call.headers


@pytest.mark.asyncio
async def test_answers_unsupported_methods_and_rejects_malformed_requests() -> None:
    async with DownstreamAgent(DownstreamConfig()) as agent:
        async with httpx.AsyncClient(trust_env=False) as client:
            unsupported = await client.post(
                f"{agent.url}{MESSAGE_PATH}",
                json={"jsonrpc": "2.0", "id": "1", "method": "tasks/get", "params": {}},
            )
            malformed = await client.post(f"{agent.url}{MESSAGE_PATH}", content=b"{")
            elsewhere = await client.post(f"{agent.url}/other", json={})

        calls = agent.calls_since(0)

    assert unsupported.json()["error"]["code"] == -32601
    assert malformed.status_code == 400
    assert elsewhere.status_code == 404
    assert [call.method for call in calls] == ["tasks/get"]
    assert evaluate_delegation(DelegationExpectation(count=1), calls, {}) == [
        "downstream agent received unsupported method(s): tasks/get"
    ]


@pytest.mark.asyncio
async def test_proves_delegated_content_through_a_contract() -> None:
    config = _config(
        [
            {
                "name": "stock lookup",
                "message": "How many units of SKU-42 are left? Ask the agent at {{downstream_url}}",
                "expect": {
                    "state": "completed",
                    "delegation": {
                        "count": 1,
                        "text": {"contains": "SKU-42"},
                        "data": [{"path": "/sku", "equals": "SKU-42"}],
                        "not_contains_env": "A2A_AUTHORIZATION",
                    },
                },
            }
        ]
    )

    async with _downstream(config) as agent:
        result = await run_with_sender(
            config,
            _delegating_sender(),
            invariant_secrets={"A2A_AUTHORIZATION": SECRET},
            downstream=agent,
        )

    assert result.passed
    assert result.scenarios[0].trials[0].turns[0].failures == []


@pytest.mark.asyncio
async def test_reports_a_forwarded_secret_and_an_unexpected_call_count() -> None:
    config = _config(
        [
            {
                "name": "stock lookup",
                "message": "Ask the agent at {{downstream_url}}",
                "expect": {
                    "delegation": {
                        "count": 1,
                        "text": {"contains": "SKU-99"},
                        "not_contains_env": "A2A_AUTHORIZATION",
                    }
                },
            }
        ]
    )

    async with _downstream(config) as agent:
        result = await run_with_sender(
            config,
            _delegating_sender(forward_secret=True, calls=2),
            invariant_secrets={"A2A_AUTHORIZATION": SECRET},
            downstream=agent,
        )

    assert not result.passed
    assert result.scenarios[0].trials[0].turns[0].failures == [
        "expected 1 downstream call(s), got 2",
        "downstream call contains value from environment variable 'A2A_AUTHORIZATION'",
        "downstream call: text does not contain 'SKU-99'",
    ]


@pytest.mark.asyncio
async def test_reports_a_missing_downstream_call() -> None:
    config = _config(
        [
            {
                "name": "no delegation",
                "message": "Answer without help from {{downstream_url}}",
                "expect": {"delegation": {"text": {"contains": "SKU-42"}}},
            }
        ]
    )

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome(
            state="completed",
            text="answered alone",
            task_id=None,
            context_id=str(context["context_id"]),
            duration_ms=1,
        )

    async with _downstream(config) as agent:
        result = await run_with_sender(config, send_turn, downstream=agent)

    assert not result.passed
    assert result.scenarios[0].trials[0].turns[0].failures == [
        "agent made no downstream call to check"
    ]


@pytest.mark.asyncio
async def test_rejects_concurrent_trials_for_delegation_contracts() -> None:
    config = _config(
        [
            {
                "name": "stock lookup",
                "message": "Ask {{downstream_url}}",
                "expect": {"delegation": {"count": 1}},
            }
        ]
    )

    with pytest.raises(ValueError, match="delegation checks require sequential trials"):
        await run_with_sender(config, _delegating_sender(), max_parallel_trials=2)


@pytest.mark.asyncio
async def test_requires_a_running_downstream_agent() -> None:
    config = _config(
        [
            {
                "name": "stock lookup",
                "message": "Ask {{downstream_url}}",
                "expect": {"delegation": {"count": 1}},
            }
        ]
    )

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return TurnOutcome(
            state="completed",
            text="answered alone",
            task_id=None,
            context_id=str(context["context_id"]),
            duration_ms=1,
        )

    result = await run_with_sender(config, send_turn)

    assert not result.passed
    assert result.scenarios[0].trials[0].error == (
        "ValueError: delegation checks require a running downstream agent"
    )


def test_requires_downstream_settings_for_delegation_contracts() -> None:
    with pytest.raises(ValueError, match="downstream settings are required"):
        ProofConfig.model_validate(
            {
                "version": 1,
                "agent": {"url": "https://example.com"},
                "scenarios": [
                    {
                        "name": "stock lookup",
                        "message": "Ask a friend",
                        "expect": {"delegation": {"count": 1}},
                    }
                ],
            }
        )


def test_requires_at_least_one_delegation_check() -> None:
    with pytest.raises(ValueError, match="delegation must define count, text, data"):
        DelegationExpectation()


def test_resolves_the_downstream_url_inside_structured_input() -> None:
    resolved = resolve_downstream_url(
        {"target": "{{downstream_url}}/a2a", "targets": ["{{downstream_url}}"], "retries": 2},
        "http://127.0.0.1:9",
    )

    assert resolved == {
        "target": "http://127.0.0.1:9/a2a",
        "targets": ["http://127.0.0.1:9"],
        "retries": 2,
    }


def test_reports_a_public_url_for_a_remote_agent() -> None:
    config = DownstreamConfig.model_validate(
        {"listen_host": "0.0.0.0", "public_url": "https://proof.example.net"}
    )

    assert str(config.public_url) == "https://proof.example.net/"

    with pytest.raises(ValueError, match="public_url is required"):
        DownstreamConfig.model_validate({"listen_host": "0.0.0.0"})
    with pytest.raises(ValueError, match="must not contain credentials"):
        DownstreamConfig.model_validate({"public_url": "https://user:pass@proof.example.net"})


@pytest.mark.asyncio
async def test_uses_a_configured_public_url_and_bounds_recorded_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(downstream_module, "MAX_DOWNSTREAM_CALLS", 1)
    config = DownstreamConfig.model_validate({"public_url": "http://127.0.0.1:8899"})

    async with DownstreamAgent(config) as agent:
        assert agent.url == "http://127.0.0.1:8899"
        card = json.loads(_request(agent, AGENT_CARD_PATH))
        for _ in range(2):
            _request(
                agent,
                MESSAGE_PATH,
                body={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "SendMessage",
                    "params": {"message": {"parts": "not a list"}},
                },
            )
        calls = agent.calls_since(0)

    assert card["supportedInterfaces"][0]["url"] == "http://127.0.0.1:8899/a2a"
    assert len(calls) == 1
    assert (calls[0].text, calls[0].data) == ("", ())


@pytest.mark.asyncio
async def test_rejects_an_oversized_downstream_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downstream_module, "MAX_DOWNSTREAM_BODY_BYTES", 1)

    async with DownstreamAgent(DownstreamConfig()) as agent:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                f"{agent.url}{MESSAGE_PATH}",
                json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage"},
            )
        calls = agent.calls_since(0)

    assert response.status_code == 400
    assert calls == ()


@pytest.mark.asyncio
async def test_reports_a_stopped_agent() -> None:
    agent = DownstreamAgent(DownstreamConfig())

    await agent.__aexit__()

    with pytest.raises(RuntimeError, match="downstream agent is not running"):
        _ = agent.url
    with pytest.raises(RuntimeError, match="downstream agent is not running"):
        agent.recorded()


def _request(agent: DownstreamAgent, path: str, body: dict[str, Any] | None = None) -> str:
    host, port = agent._require_server().server_address[:2]
    url = f"http://{host}:{port}{path}"
    with httpx.Client(trust_env=False) as client:
        if body is None:
            return client.get(url).text
        return client.post(url, json=body).text


def _payload(call: DownstreamCall) -> dict[str, Any]:
    return json.loads(call.body)
