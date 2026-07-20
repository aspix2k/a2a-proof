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
from a2a.client.service_parameters import ServiceParametersFactory, with_a2a_extensions
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.helpers import new_data_part, new_message, new_text_part
from a2a.types import (
    AgentCard,
    AuthenticationInfo,
    CancelTaskRequest,
    GetTaskRequest,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
)
from a2a.utils.constants import TransportProtocol
from pydantic import JsonValue

from a2a_proof.files import PreparedFile
from a2a_proof.models import AgentConfig
from a2a_proof.protocol import ProtocolError, ResponseCollector, TurnOutcome
from a2a_proof.push import PushTarget

LEGACY_AGENT_CARD_PATH = "/.well-known/agent.json"


class A2ASession:
    def __init__(
        self,
        client: Client,
        card: AgentCard,
        timeout: float,
        headers: Mapping[str, str] | None = None,
        extensions: list[str] | None = None,
        lifecycle_client: Client | None = None,
    ) -> None:
        self._client = client
        self._lifecycle_client = lifecycle_client or client
        self.card = card
        self._timeout = timeout
        self._service_parameters = _service_parameters(headers or {}, extensions or [])

    @classmethod
    async def connect(cls, config: AgentConfig, *, trust_env: bool = True) -> A2ASession:
        http_client = _http_client(config, trust_env=trust_env)
        lifecycle_http_client: httpx.AsyncClient | None = None
        client: Client | None = None
        lifecycle_client: Client | None = None
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
            extensions = config.requested_extensions()
            _validate_card_extensions(card, extensions)
            bindings = _protocol_bindings(config.transport)
            client_config = ClientConfig(
                streaming=True,
                httpx_client=http_client,
                grpc_channel_factory=lambda url: _grpc_channel(url, tls=config.grpc_tls),
                supported_protocol_bindings=bindings,
                use_client_preference=config.transport != "auto",
            )
            client = ClientFactory(client_config).create(card)
            lifecycle_client = client
            if card.capabilities.streaming:
                lifecycle_http_client = _http_client(config, trust_env=trust_env)
                lifecycle_config = ClientConfig(
                    streaming=False,
                    httpx_client=lifecycle_http_client,
                    grpc_channel_factory=lambda url: _grpc_channel(url, tls=config.grpc_tls),
                    supported_protocol_bindings=bindings,
                    use_client_preference=config.transport != "auto",
                )
                lifecycle_client = ClientFactory(lifecycle_config).create(card)
            return cls(
                client,
                card,
                config.timeout,
                config.headers,
                extensions,
                lifecycle_client,
            )
        except BaseException:
            try:
                if client is None:
                    await http_client.aclose()
                else:
                    await client.close()
            finally:
                if lifecycle_client is not None and lifecycle_client is not client:
                    await lifecycle_client.close()
                elif lifecycle_http_client is not None:
                    await lifecycle_http_client.aclose()
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
        try:
            await self._client.close()
        finally:
            if self._lifecycle_client is not self._client:
                await self._lifecycle_client.close()

    async def send_turn(
        self,
        text: str | None,
        *,
        data: list[JsonValue] | None = None,
        files: list[PreparedFile] | None = None,
        context_id: str,
        task_id: str | None,
        return_immediately: bool = False,
        push_notification: PushTarget | None = None,
    ) -> TurnOutcome:
        if push_notification is not None and not self.card.capabilities.push_notifications:
            raise ProtocolError("Agent Card does not advertise push notifications")
        parts = [new_text_part(text)] if text is not None else []
        parts.extend(new_data_part(value) for value in data or [])
        parts.extend(
            Part(raw=file.content, filename=file.filename, media_type=file.media_type)
            for file in files or []
        )
        message = new_message(
            parts,
            context_id=context_id,
            task_id=task_id,
            role=Role.ROLE_USER,
        )
        configuration: SendMessageConfiguration | None = None
        if return_immediately or push_notification is not None:
            configuration = SendMessageConfiguration(return_immediately=return_immediately)
            if push_notification is not None:
                configuration.task_push_notification_config.CopyFrom(
                    TaskPushNotificationConfig(
                        url=push_notification.url,
                        token=push_notification.token,
                        authentication=AuthenticationInfo(
                            scheme="Bearer",
                            credentials=push_notification.token,
                        ),
                    )
                )
        request = SendMessageRequest(message=message, configuration=configuration)
        collector = ResponseCollector(context_id)
        started = perf_counter()
        first_event_ms: int | None = None

        try:
            async with asyncio.timeout(self._timeout):
                call_context = ClientCallContext(
                    timeout=self._timeout,
                    service_parameters=self._service_parameters.copy() or None,
                )
                client = self._lifecycle_client if return_immediately else self._client
                async for response in client.send_message(request, context=call_context):
                    if first_event_ms is None:
                        first_event_ms = round((perf_counter() - started) * 1_000)
                    collector.add(response)
        except TimeoutError as error:
            raise ProtocolError(f"agent did not finish within {self._timeout:g} seconds") from error

        return collector.finish(
            duration_ms=round((perf_counter() - started) * 1_000),
            first_event_ms=first_event_ms,
            require_terminal=not return_immediately,
        )

    async def cancel_task(self, *, task_id: str, context_id: str) -> TurnOutcome:
        started = perf_counter()
        try:
            async with asyncio.timeout(self._timeout):
                task = await self._lifecycle_client.cancel_task(
                    CancelTaskRequest(id=task_id),
                    context=self._call_context(),
                )
        except TimeoutError as error:
            raise ProtocolError(
                f"agent did not cancel task within {self._timeout:g} seconds"
            ) from error
        return _task_outcome(task, context_id, started)

    async def get_task(
        self,
        *,
        task_id: str,
        context_id: str,
        history_length: int | None = None,
    ) -> TurnOutcome:
        started = perf_counter()
        request = GetTaskRequest(id=task_id)
        if history_length is not None:
            request.history_length = history_length
        try:
            async with asyncio.timeout(self._timeout):
                task = await self._lifecycle_client.get_task(
                    request,
                    context=self._call_context(),
                )
        except TimeoutError as error:
            raise ProtocolError(
                f"agent did not return task within {self._timeout:g} seconds"
            ) from error
        return _task_outcome(task, context_id, started)

    async def subscribe_task(self, *, task_id: str, context_id: str) -> TurnOutcome:
        if not self.card.capabilities.streaming:
            raise ProtocolError("Agent Card does not advertise streaming")
        collector = ResponseCollector(
            context_id,
            expected_identity=(task_id, context_id),
        )
        started = perf_counter()
        first_event_ms: int | None = None
        try:
            async with asyncio.timeout(self._timeout):
                async for response in self._client.subscribe(
                    SubscribeToTaskRequest(id=task_id),
                    context=self._call_context(),
                ):
                    if response.HasField("message"):
                        raise ProtocolError("task subscription returned a standalone message")
                    if first_event_ms is None:
                        if not response.HasField("task"):
                            raise ProtocolError(
                                "task subscription did not start with a task snapshot"
                            )
                        first_event_ms = round((perf_counter() - started) * 1_000)
                    collector.add(response)
        except TimeoutError as error:
            raise ProtocolError(
                f"agent did not finish subscribed task within {self._timeout:g} seconds"
            ) from error
        return collector.finish(
            duration_ms=round((perf_counter() - started) * 1_000),
            first_event_ms=first_event_ms,
        )

    def _call_context(self) -> ClientCallContext:
        return ClientCallContext(
            timeout=self._timeout,
            service_parameters=self._service_parameters.copy() or None,
        )


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


def _http_client(config: AgentConfig, *, trust_env: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=config.headers,
        timeout=httpx.Timeout(config.timeout),
        follow_redirects=False,
        trust_env=trust_env,
    )


def _task_outcome(task: Task, context_id: str, started: float) -> TurnOutcome:
    elapsed_ms = round((perf_counter() - started) * 1_000)
    collector = ResponseCollector(context_id)
    collector.add(StreamResponse(task=task))
    return collector.finish(
        duration_ms=elapsed_ms,
        first_event_ms=elapsed_ms,
        require_terminal=False,
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


def _service_parameters(headers: Mapping[str, str], extensions: list[str]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    configured = list(extensions)
    for name, value in headers.items():
        if name.lower() == HTTP_EXTENSION_HEADER.lower():
            configured.extend(item.strip() for item in value.split(","))
        else:
            parameters[name] = value
    return ServiceParametersFactory.create_from(
        parameters,
        [with_a2a_extensions(list(dict.fromkeys(configured)))],
    )


def _validate_card_extensions(card: AgentCard, requested: list[str]) -> None:
    advertised = {extension.uri for extension in card.capabilities.extensions}
    required = {extension.uri for extension in card.capabilities.extensions if extension.required}
    requested_set = set(requested)
    missing_advertised = sorted(requested_set - advertised)
    missing_required = sorted(required - requested_set)
    failures: list[str] = []
    if missing_advertised:
        failures.append(
            "Agent Card does not advertise requested extension(s): " + ", ".join(missing_advertised)
        )
    if missing_required:
        failures.append(
            "Agent Card requires unconfigured extension(s): " + ", ".join(missing_required)
        )
    if failures:
        raise ProtocolError("; ".join(failures))


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
