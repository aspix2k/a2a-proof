from __future__ import annotations

import asyncio
from collections.abc import Mapping
from time import perf_counter
from types import TracebackType
from urllib.parse import urlsplit

import grpc
import httpx
from a2a.client import A2ACardResolver, ClientCallContext, ClientConfig, ClientFactory
from a2a.client.client import Client
from a2a.client.errors import AgentCardResolutionError
from a2a.helpers import new_text_message
from a2a.types import (
    AgentCard,
    Role,
    SendMessageRequest,
)
from a2a.utils.constants import TransportProtocol

from a2a_proof.models import AgentConfig
from a2a_proof.protocol import ProtocolError, ResponseCollector, TurnOutcome

LEGACY_AGENT_CARD_PATH = "/.well-known/agent.json"


class A2ASession:
    def __init__(
        self,
        client: Client,
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._service_parameters = dict(headers or {})

    @classmethod
    async def connect(cls, config: AgentConfig) -> A2ASession:
        http_client = _http_client(config)
        try:
            card = await _resolve_card(
                http_client,
                str(config.url),
                config.card_path,
            )
            _validate_card_interfaces(
                card,
                str(config.url),
                allow_cross_origin=config.allow_cross_origin_interfaces,
            )
            bindings = _protocol_bindings(config.transport)
            client_config = ClientConfig(
                streaming=True,
                httpx_client=http_client,
                grpc_channel_factory=lambda url: _grpc_channel(url, tls=config.grpc_tls),
                supported_protocol_bindings=bindings,
                use_client_preference=config.transport != "auto",
            )
            client = ClientFactory(client_config).create(card)
            return cls(client, config.timeout, config.headers)
        except BaseException:
            await http_client.aclose()
            raise

    async def __aenter__(self) -> A2ASession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.close()

    async def send_turn(
        self,
        text: str,
        *,
        context_id: str,
        task_id: str | None,
    ) -> TurnOutcome:
        message = new_text_message(text, role=Role.ROLE_USER)
        message.context_id = context_id
        if task_id is not None:
            message.task_id = task_id
        request = SendMessageRequest(message=message)
        collector = ResponseCollector(context_id)
        started = perf_counter()

        try:
            async with asyncio.timeout(self._timeout):
                call_context = ClientCallContext(
                    timeout=self._timeout,
                    service_parameters=self._service_parameters.copy() or None,
                )
                async for response in self._client.send_message(request, context=call_context):
                    collector.add(response)
        except TimeoutError as error:
            raise ProtocolError(f"agent did not finish within {self._timeout:g} seconds") from error

        return collector.finish(duration_ms=round((perf_counter() - started) * 1_000))


async def discover_agent(
    config: AgentConfig,
) -> AgentCard:
    async with _http_client(config) as http_client:
        card = await _resolve_card(http_client, str(config.url), config.card_path)
        _validate_card_interfaces(
            card,
            str(config.url),
            allow_cross_origin=config.allow_cross_origin_interfaces,
        )
        return card


def _http_client(config: AgentConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=config.headers,
        timeout=httpx.Timeout(config.timeout),
        follow_redirects=False,
    )


async def _resolve_card(
    http_client: httpx.AsyncClient,
    base_url: str,
    card_path: str | None,
) -> AgentCard:
    resolver = A2ACardResolver(http_client, base_url)
    try:
        return await resolver.get_agent_card(relative_card_path=card_path)
    except AgentCardResolutionError as error:
        if card_path is not None or error.status_code != httpx.codes.NOT_FOUND:
            raise
        return await resolver.get_agent_card(relative_card_path=LEGACY_AGENT_CARD_PATH)


def _protocol_bindings(transport: str) -> list[str]:
    if transport == "JSONRPC":
        return [TransportProtocol.JSONRPC]
    if transport == "HTTP+JSON":
        return [TransportProtocol.HTTP_JSON]
    if transport == "GRPC":
        return [TransportProtocol.GRPC]
    return [TransportProtocol.JSONRPC, TransportProtocol.HTTP_JSON, TransportProtocol.GRPC]


def _grpc_channel(url: str, *, tls: bool) -> grpc.aio.Channel:
    target = _grpc_target(url)
    if tls:
        return grpc.aio.secure_channel(target, grpc.ssl_channel_credentials())
    return grpc.aio.insecure_channel(target)


def _grpc_target(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "grpc", "grpcs"}:
        return url
    if not parsed.hostname or parsed.username or parsed.password:
        raise ProtocolError(f"invalid gRPC interface URL: {url!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ProtocolError(
            f"gRPC interface URL must not contain a path, query, or fragment: {url!r}"
        )
    return parsed.netloc


def _validate_card_interfaces(
    card: AgentCard,
    base_url: str,
    *,
    allow_cross_origin: bool,
) -> None:
    if allow_cross_origin:
        return
    expected = _network_origin(base_url)
    for interface in card.supported_interfaces:
        if _network_origin(interface.url) != expected:
            raise ProtocolError(
                f"Agent Card interface {interface.url!r} has a different origin; "
                "set allow_cross_origin_interfaces: true only if you trust it"
            )


def _network_origin(url: str) -> tuple[bool, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "grpc", "grpcs"}:
        raise ProtocolError(f"Agent Card interface has an unsupported URL: {url!r}")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ProtocolError(f"Agent Card interface has an invalid URL: {url!r}")
    try:
        port = parsed.port
    except ValueError as error:
        raise ProtocolError(f"Agent Card interface has an invalid URL: {url!r}") from error
    secure = parsed.scheme in {"https", "grpcs"}
    return secure, parsed.hostname, port or (443 if secure else 80)
