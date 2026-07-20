from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from hashlib import sha256
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

import h11
from a2a.types import StreamResponse, TaskState
from google.protobuf.json_format import ParseDict, ParseError

from a2a_proof.models import PushNotificationsConfig
from a2a_proof.protocol import INTERRUPTED_STATES, ProtocolError, ResponseCollector, TurnOutcome

MAX_PUSH_BODY_BYTES = 1_000_000
MAX_PUSH_TOTAL_BYTES = 20_000_000
MAX_PUSH_EVENTS = 1_000
MAX_PUSH_SUBSCRIPTIONS = 100
MAX_PUSH_PATH_BYTES = 2_048
MAX_PUSH_CONNECTIONS = 64
PUSH_CONNECTION_TIMEOUT_SECONDS = 10
_AUTHORIZATION_HEADER = b"authorization"
_LEGACY_TOKEN_HEADER = b"x-a2a-notification-token"
_CONTENT_TYPE_HEADER = b"content-type"
_CONTENT_LENGTH_HEADER = b"content-length"
_ACCEPTED_CONTENT_TYPES = {b"application/a2a+json", b"application/json"}


@dataclass(frozen=True, slots=True)
class PushTarget:
    url: str = field(repr=False)
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Delivery:
    response: StreamResponse
    received_at: float


@dataclass(slots=True)
class _SubscriptionState:
    token: str
    created_at: float
    queue: asyncio.Queue[_Delivery | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=MAX_PUSH_EVENTS)
    )
    task_id: str | None = None
    context_id: str | None = None
    first_received_at: float | None = None
    rejection: str | None = None
    received_bytes: int = 0
    fingerprints: set[bytes] = field(default_factory=set)


class PushSubscription:
    def __init__(
        self,
        receiver: PushReceiver,
        path: str,
        target: PushTarget,
        state: _SubscriptionState,
    ) -> None:
        self._receiver = receiver
        self._path = path
        self.target = target
        self._state = state
        self._closed = False

    def bind(self, *, task_id: str, context_id: str) -> None:
        if self._closed:
            raise RuntimeError("push subscription is closed")
        self._state.task_id = task_id
        self._state.context_id = context_id

    async def wait(self, timeout_seconds: float) -> TurnOutcome:
        if self._closed:
            raise RuntimeError("push subscription is closed")
        if self._state.task_id is None:
            raise RuntimeError("push subscription is not bound to a task")

        collector = ResponseCollector(self._state.context_id or "")
        deadline = perf_counter() + timeout_seconds
        while True:
            if self._state.rejection is not None:
                raise self._wait_error(timeout_seconds)
            try:
                delivery = self._state.queue.get_nowait()
            except asyncio.QueueEmpty:
                remaining = deadline - perf_counter()
                if remaining <= 0:
                    raise self._wait_error(timeout_seconds) from None
                try:
                    async with asyncio.timeout(remaining):
                        delivery = await self._state.queue.get()
                except TimeoutError as error:
                    raise self._wait_error(timeout_seconds) from error

            if delivery is None:
                raise self._wait_error(timeout_seconds)
            self._validate_identity(delivery.response)
            collector.add(delivery.response)
            if _is_terminal(delivery.response):
                return collector.finish(
                    duration_ms=round((delivery.received_at - self._state.created_at) * 1_000),
                    first_event_ms=(
                        round((self._state.first_received_at - self._state.created_at) * 1_000)
                        if self._state.first_received_at is not None
                        else None
                    ),
                )

    def close(self) -> None:
        if not self._closed:
            self._receiver._remove(self._path)
            self._closed = True

    def _validate_identity(self, response: StreamResponse) -> None:
        task_id, context_id = _response_identity(response)
        if not task_id:
            raise ProtocolError("push notification did not identify its task")
        if task_id != self._state.task_id:
            raise ProtocolError("push notification referenced an unexpected task")
        if context_id != self._state.context_id:
            raise ProtocolError("push notification referenced an unexpected context")

    def _wait_error(self, timeout_seconds: float) -> ProtocolError:
        if self._state.rejection is not None:
            return ProtocolError(f"agent push notification was rejected: {self._state.rejection}")
        return ProtocolError(
            f"agent did not deliver a terminal push notification within {timeout_seconds:g} seconds"
        )


class PushReceiver:
    def __init__(self, config: PushNotificationsConfig) -> None:
        self._config = config
        self._server: asyncio.AbstractServer | None = None
        self._base_url: str | None = None
        self._subscriptions: dict[str, _SubscriptionState] = {}
        self._connections = asyncio.Semaphore(MAX_PUSH_CONNECTIONS)
        self._handler_tasks: set[asyncio.Task[object]] = set()

    async def __aenter__(self) -> PushReceiver:
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._config.listen_host,
            port=self._config.listen_port,
            backlog=MAX_PUSH_CONNECTIONS,
        )
        socket = self._server.sockets[0] if self._server.sockets else None
        if socket is None:
            await self.close()
            raise RuntimeError("push receiver did not open a listening socket")
        port = int(socket.getsockname()[1])
        self._base_url = (
            str(self._config.public_url).rstrip("/")
            if self._config.public_url is not None
            else f"http://{_url_host(self._config.listen_host)}:{port}"
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()

    def register(self) -> PushSubscription:
        if self._base_url is None:
            raise RuntimeError("push receiver is not running")
        if len(self._subscriptions) >= MAX_PUSH_SUBSCRIPTIONS:
            raise ProtocolError(
                f"push receiver exceeded {MAX_PUSH_SUBSCRIPTIONS} active subscriptions"
            )
        route = secrets.token_urlsafe(24)
        token = secrets.token_urlsafe(32)
        path, url = _callback_url(self._base_url, route)
        state = _SubscriptionState(token=token, created_at=perf_counter())
        self._subscriptions[path] = state
        return PushSubscription(self, path, PushTarget(url=url, token=token), state)

    async def close(self) -> None:
        self._subscriptions.clear()
        self._base_url = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        current_task = asyncio.current_task()
        handlers = tuple(task for task in self._handler_tasks if task is not current_task)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)

    def _remove(self, path: str) -> None:
        self._subscriptions.pop(path, None)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        handler_task = asyncio.current_task()
        if handler_task is not None:
            self._handler_tasks.add(handler_task)
        try:
            if self._connections.locked():
                await _send_response(writer, 503)
                return
            async with self._connections:
                async with asyncio.timeout(PUSH_CONNECTION_TIMEOUT_SECONDS):
                    await self._read_request(reader, writer)
        except TimeoutError:
            await _send_response(writer, 408)
        except (ConnectionError, h11.RemoteProtocolError):
            await _send_response(writer, 400)
        finally:
            if handler_task is not None:
                self._handler_tasks.discard(handler_task)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def _read_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=16_384)
        state: _SubscriptionState | None = None
        body = bytearray()
        while True:
            data = await reader.read(65_536)
            if not data:
                return
            connection.receive_data(data)
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    break
                if isinstance(event, h11.Request):
                    status, state = self._accept_request(event)
                    if status is not None:
                        await _send_response(writer, status)
                        return
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                    if len(body) > MAX_PUSH_BODY_BYTES:
                        if state is not None:
                            _reject(state, "body exceeded the size limit", 413)
                        await _send_response(writer, 413)
                        return
                elif isinstance(event, h11.EndOfMessage):
                    if state is None:
                        await _send_response(writer, 400)
                        return
                    status = self._record_delivery(state, bytes(body))
                    await _send_response(writer, status)
                    return

    def _accept_request(
        self,
        request: h11.Request,
    ) -> tuple[int | None, _SubscriptionState | None]:
        if request.method != b"POST":
            return 405, None
        status, state = self._resolve_subscription(request.target)
        if status is not None or state is None:
            return status, state
        if not _valid_authentication(request.headers, state.token):
            return 401, None
        return _validate_content_headers(request.headers, state), state

    def _resolve_subscription(
        self,
        target_bytes: bytes,
    ) -> tuple[int | None, _SubscriptionState | None]:
        if len(target_bytes) > MAX_PUSH_PATH_BYTES:
            return 414, None
        try:
            target = target_bytes.decode("ascii")
        except UnicodeDecodeError:
            return 400, None
        parsed = urlsplit(target)
        if parsed.query or parsed.fragment:
            return 404, None
        state = self._subscriptions.get(parsed.path)
        return (None, state) if state is not None else (404, None)

    @staticmethod
    def _record_delivery(state: _SubscriptionState, body: bytes) -> int:
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError
            response = StreamResponse()
            ParseDict(value, response, ignore_unknown_fields=False, max_recursion_depth=50)
            if response.WhichOneof("payload") is None:
                raise ValueError
        except (UnicodeDecodeError, ValueError, TypeError, ParseError):
            return _reject(state, "invalid StreamResponse", 400)
        fingerprint = sha256(response.SerializeToString(deterministic=True)).digest()
        if state.received_bytes + len(body) > MAX_PUSH_TOTAL_BYTES:
            return _reject(state, "total body size exceeded the limit", 413)
        state.received_bytes += len(body)
        if fingerprint in state.fingerprints:
            return 204
        if len(state.fingerprints) >= MAX_PUSH_EVENTS:
            return _reject(state, "event limit exceeded", 429)
        received_at = perf_counter()
        if state.first_received_at is None:
            state.first_received_at = received_at
        state.fingerprints.add(fingerprint)
        state.queue.put_nowait(
            _Delivery(
                response=response,
                received_at=received_at,
            )
        )
        return 204


def _header_values(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for key, value in headers if key.lower() == name]


def _valid_authentication(headers: Sequence[tuple[bytes, bytes]], expected: str) -> bool:
    authorization = _header_values(headers, _AUTHORIZATION_HEADER)
    legacy = _header_values(headers, _LEGACY_TOKEN_HEADER)
    if len(authorization) > 1 or len(legacy) > 1 or (not authorization and not legacy):
        return False
    if authorization:
        scheme, separator, credentials = authorization[0].partition(b" ")
        if (
            not separator
            or scheme.lower() != b"bearer"
            or not secrets.compare_digest(credentials.decode("latin-1"), expected)
        ):
            return False
    return not legacy or secrets.compare_digest(legacy[0].decode("latin-1"), expected)


def _validate_content_headers(
    headers: Sequence[tuple[bytes, bytes]],
    state: _SubscriptionState,
) -> int | None:
    content_types = _header_values(headers, _CONTENT_TYPE_HEADER)
    if len(content_types) != 1 or content_types[0].split(b";", 1)[0].strip().lower() not in (
        _ACCEPTED_CONTENT_TYPES
    ):
        return _reject(state, "unsupported content type", 415)
    lengths = _header_values(headers, _CONTENT_LENGTH_HEADER)
    if not lengths:
        return None
    return _validate_content_length(lengths, state)


def _validate_content_length(lengths: list[bytes], state: _SubscriptionState) -> int | None:
    if len(lengths) != 1:
        return _reject(state, "ambiguous content length", 400)
    try:
        length = int(lengths[0])
    except ValueError:
        return _reject(state, "invalid content length", 400)
    if length < 0:
        return _reject(state, "invalid content length", 400)
    if length > MAX_PUSH_BODY_BYTES:
        return _reject(state, "body exceeded the size limit", 413)
    return None


def _reject(state: _SubscriptionState, reason: str, status: int) -> int:
    if state.rejection is None:
        state.rejection = reason
        with suppress(asyncio.QueueFull):
            state.queue.put_nowait(None)
    return status


async def _send_response(writer: asyncio.StreamWriter, status: int) -> None:
    if writer.is_closing():
        return
    connection = h11.Connection(h11.SERVER)
    try:
        writer.write(
            connection.send(
                h11.Response(
                    status_code=status,
                    headers=[(b"Content-Length", b"0"), (b"Connection", b"close")],
                )
            )
        )
        writer.write(connection.send(h11.EndOfMessage()))
        await writer.drain()
    except (ConnectionError, h11.LocalProtocolError):
        return


def _callback_url(base_url: str, route: str) -> tuple[str, str]:
    parsed = urlsplit(base_url)
    path = f"{parsed.path.rstrip('/')}/.a2a-proof/push/{route}"
    return path, urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _response_identity(response: StreamResponse) -> tuple[str, str]:
    payload = response.WhichOneof("payload")
    if payload == "task":
        return response.task.id, response.task.context_id
    if payload == "message":
        return response.message.task_id, response.message.context_id
    if payload == "status_update":
        return response.status_update.task_id, response.status_update.context_id
    if payload == "artifact_update":
        return response.artifact_update.task_id, response.artifact_update.context_id
    return "", ""


def _is_terminal(response: StreamResponse) -> bool:
    payload = response.WhichOneof("payload")
    if payload == "message":
        return True
    if payload == "task":
        state = response.task.status.state
    elif payload == "status_update":
        state = response.status_update.status.state
    else:
        return False
    name = TaskState.Name(state).removeprefix("TASK_STATE_").lower()
    return name in INTERRUPTED_STATES
