from __future__ import annotations

import asyncio
import json
from typing import cast
from urllib.parse import urlsplit, urlunsplit

import pytest
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict

import a2a_proof.push as push_module
from a2a_proof.models import PushNotificationsConfig
from a2a_proof.protocol import ProtocolError
from a2a_proof.push import PushReceiver, PushSubscription


class _Writer:
    def __init__(
        self,
        *,
        closing: bool = False,
        drain_error: Exception | None = None,
        wait_closed_error: Exception | None = None,
    ) -> None:
        self.closing = closing
        self.drain_error = drain_error
        self.wait_closed_error = wait_closed_error
        self.output = bytearray()
        self.closed = False

    def is_closing(self) -> bool:
        return self.closing

    def write(self, data: bytes) -> None:
        self.output.extend(data)

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self.wait_closed_error is not None:
            raise self.wait_closed_error


def _status(
    state: TaskState.ValueType,
    *,
    task_id: str = "task",
    context_id: str = "context",
) -> StreamResponse:
    return StreamResponse(
        status_update=TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            status=TaskStatus(state=state),
        )
    )


def _artifact(*, task_id: str = "task", context_id: str = "context") -> StreamResponse:
    return StreamResponse(
        artifact_update=TaskArtifactUpdateEvent(
            task_id=task_id,
            context_id=context_id,
            artifact=Artifact(artifact_id="result", parts=[Part(text="ready")]),
        )
    )


async def _deliver(
    subscription: PushSubscription,
    response: StreamResponse,
    *,
    token: str | None = None,
    content_type: str = "application/json",
    use_authorization: bool = False,
) -> int:
    credential = token or subscription.target.token
    authentication = (
        {"Authorization": f"Bearer {credential}"}
        if use_authorization
        else {"X-A2A-Notification-Token": credential}
    )
    return await _request(
        subscription.target.url,
        headers={
            "Content-Type": content_type,
            **authentication,
        },
        body=json.dumps(MessageToDict(response)).encode(),
    )


async def _request(
    url: str,
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> int:
    parsed = urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    request_headers = {
        "Host": parsed.netloc,
        "Content-Length": str(len(body)),
        "Connection": "close",
        **(headers or {}),
    }
    head = "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
    request = f"{method} {target} HTTP/1.1\r\n{head}\r\n".encode() + body
    response = await _raw_exchange(url, request)
    return int(response.split(b" ", 2)[1])


async def _raw_exchange(url: str, *chunks: bytes) -> bytes:
    parsed = urlsplit(url)
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    for chunk in chunks:
        writer.write(chunk)
        await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.asyncio
async def test_collects_deduplicated_push_sequence() -> None:
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        subscription.bind(task_id="task", context_id="context")
        assert "token" not in repr(subscription.target).lower()

        working = _status(TaskState.TASK_STATE_WORKING)
        assert await _deliver(subscription, working, use_authorization=True) == 204
        assert await _deliver(subscription, working) == 204
        assert await _deliver(subscription, _artifact()) == 204
        assert await _deliver(subscription, _status(TaskState.TASK_STATE_COMPLETED)) == 204

        outcome = await subscription.wait(1)

    assert outcome.state == "completed"
    assert outcome.states == ("working", "completed")
    assert outcome.text == "ready"
    assert outcome.task_id == "task"
    assert outcome.context_id == "context"
    assert outcome.first_event_ms is not None
    assert outcome.duration_ms >= outcome.first_event_ms


@pytest.mark.asyncio
async def test_rejects_unauthenticated_unknown_and_unsupported_requests() -> None:
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        payload = MessageToDict(_status(TaskState.TASK_STATE_COMPLETED))
        parsed = urlsplit(subscription.target.url)
        unknown = urlunsplit((parsed.scheme, parsed.netloc, f"{parsed.path}x", "", ""))

        body = json.dumps(payload).encode()
        assert await _request(unknown, body=body) == 404
        assert (
            await _request(
                subscription.target.url,
                headers={"X-A2A-Notification-Token": "wrong", "Content-Type": "application/json"},
                body=body,
            )
            == 401
        )
        assert (
            await _request(
                subscription.target.url,
                method="GET",
                headers={"X-A2A-Notification-Token": subscription.target.token},
            )
            == 405
        )
        assert (
            await _request(
                subscription.target.url,
                headers={
                    "X-A2A-Notification-Token": subscription.target.token,
                    "Content-Type": "text/plain",
                },
                body=b"{}",
            )
            == 415
        )


@pytest.mark.asyncio
async def test_surfaces_authenticated_invalid_payload() -> None:
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        subscription.bind(task_id="task", context_id="context")
        waiting = asyncio.create_task(subscription.wait(1))
        await asyncio.sleep(0)
        status = await _request(
            subscription.target.url,
            headers={
                "X-A2A-Notification-Token": subscription.target.token,
                "Content-Type": "application/a2a+json; charset=utf-8",
            },
            body=b"not-json",
        )
        assert status == 400
        with pytest.raises(ProtocolError, match="rejected: invalid StreamResponse"):
            await waiting


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_id", "context_id", "message"),
    [
        ("other", "context", "unexpected task"),
        ("task", "other", "unexpected context"),
        ("task", "", "unexpected context"),
        ("", "context", "did not identify its task"),
    ],
)
async def test_rejects_delivery_for_another_task_or_context(
    task_id: str,
    context_id: str,
    message: str,
) -> None:
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        subscription.bind(task_id="task", context_id="context")
        assert (
            await _deliver(
                subscription,
                _status(
                    TaskState.TASK_STATE_COMPLETED,
                    task_id=task_id,
                    context_id=context_id,
                ),
            )
            == 204
        )
        with pytest.raises(ProtocolError, match=message):
            await subscription.wait(1)


@pytest.mark.asyncio
async def test_times_out_without_terminal_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter([10.0, 11.0, 12.0])
    monkeypatch.setattr(push_module, "perf_counter", lambda: next(timestamps))
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        subscription.bind(task_id="task", context_id="context")
        with pytest.raises(ProtocolError, match=r"within 0\.01 seconds"):
            await subscription.wait(0.01)


@pytest.mark.asyncio
async def test_wait_timeout_bounds_the_queue_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_timeouts: list[float | None] = []
    timestamps = iter([10.0, 20.0, 20.0])

    class ImmediateTimeout:
        async def __aenter__(self) -> None:
            raise TimeoutError

        async def __aexit__(self, *args: object) -> None:
            return None

    def timeout(delay: float | None) -> ImmediateTimeout:
        observed_timeouts.append(delay)
        return ImmediateTimeout()

    monkeypatch.setattr(push_module, "perf_counter", lambda: next(timestamps))
    monkeypatch.setattr(push_module.asyncio, "timeout", timeout)
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        subscription.bind(task_id="task", context_id="context")
        with pytest.raises(ProtocolError, match="within 2 seconds"):
            await subscription.wait(2)

    assert observed_timeouts == [2]


@pytest.mark.asyncio
async def test_reports_exact_push_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter([10.0, 10.125, 10.25, 10.3])
    monkeypatch.setattr(push_module, "perf_counter", lambda: next(timestamps))
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        subscription.bind(task_id="task", context_id="context")
        working = json.dumps(MessageToDict(_status(TaskState.TASK_STATE_WORKING))).encode()
        completed = json.dumps(MessageToDict(_status(TaskState.TASK_STATE_COMPLETED))).encode()
        assert PushReceiver._record_delivery(subscription._state, working) == 204
        assert PushReceiver._record_delivery(subscription._state, completed) == 204

        outcome = await subscription.wait(1)

    assert outcome.first_event_ms == 125
    assert outcome.duration_ms == 250


@pytest.mark.asyncio
async def test_closed_subscription_route_is_removed() -> None:
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        target = subscription.target
        subscription.close()
        subscription.close()
        status = await _request(
            target.url,
            headers={
                "X-A2A-Notification-Token": target.token,
                "Content-Type": "application/json",
            },
            body=json.dumps(MessageToDict(_status(TaskState.TASK_STATE_COMPLETED))).encode(),
        )
    assert status == 404


@pytest.mark.asyncio
async def test_receiver_close_is_idempotent_and_disables_registration() -> None:
    receiver = PushReceiver(PushNotificationsConfig())
    async with receiver:
        pass

    with pytest.raises(RuntimeError, match="not running"):
        receiver.register()
    await receiver.close()


@pytest.mark.asyncio
async def test_uses_public_base_url_without_exposing_listener_port() -> None:
    config = PushNotificationsConfig(public_url="https://hooks.example/base")
    async with PushReceiver(config) as receiver:
        subscription = receiver.register()
    assert subscription.target.url.startswith("https://hooks.example/base/.a2a-proof/push/")


@pytest.mark.asyncio
async def test_bounds_active_subscriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_module, "MAX_PUSH_SUBSCRIPTIONS", 1)
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        receiver.register()
        with pytest.raises(ProtocolError, match="exceeded 1 active subscriptions"):
            receiver.register()


@pytest.mark.asyncio
async def test_subscription_and_receiver_lifecycle_guards() -> None:
    receiver = PushReceiver(PushNotificationsConfig())
    with pytest.raises(RuntimeError, match="not running"):
        receiver.register()
    await receiver.close()

    async with receiver:
        subscription = receiver.register()
        with pytest.raises(RuntimeError, match="not bound"):
            await subscription.wait(0.01)
        subscription.close()
        with pytest.raises(RuntimeError, match="closed"):
            subscription.bind(task_id="task", context_id="context")
        with pytest.raises(RuntimeError, match="closed"):
            await subscription.wait(0.01)


@pytest.mark.asyncio
async def test_receiver_requires_a_listening_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class Server:
        def __init__(self) -> None:
            self.closed = False
            self.sockets: list[object] = []

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    server = Server()

    async def start_server(*args: object, **kwargs: object) -> Server:
        return server

    monkeypatch.setattr(asyncio, "start_server", start_server)
    with pytest.raises(RuntimeError, match="did not open a listening socket"):
        await PushReceiver(PushNotificationsConfig()).__aenter__()
    assert server.closed


@pytest.mark.asyncio
async def test_handles_fragmented_and_malformed_connections() -> None:
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        parsed = urlsplit(subscription.target.url)
        request = (
            f"POST {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            f"X-A2A-Notification-Token: {subscription.target.token}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\n\r\n{}"
        ).encode()
        assert (
            await _raw_exchange(subscription.target.url, request[:10], request[10:])
        ).startswith(b"HTTP/1.1 400")
        assert (await _raw_exchange(subscription.target.url, b"invalid\r\n\r\n")).startswith(
            b"HTTP/1.1 400"
        )


@pytest.mark.asyncio
async def test_bounds_chunked_body_and_request_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_module, "MAX_PUSH_BODY_BYTES", 3)
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        subscription = receiver.register()
        parsed = urlsplit(subscription.target.url)
        request = (
            f"POST {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            f"X-A2A-Notification-Token: {subscription.target.token}\r\n"
            "Content-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n\r\n"
            "4\r\nnull\r\n0\r\n\r\n"
        ).encode()
        assert (await _raw_exchange(subscription.target.url, request)).startswith(b"HTTP/1.1 413")
        status, state = receiver._resolve_subscription(b"/" + b"x" * 2_048)
        assert (status, state) == (414, None)
        status, state = receiver._resolve_subscription(b"/" + b"x" * 2_047)
        assert (status, state) == (404, None)
        status, state = receiver._resolve_subscription(b"/\xff")
        assert (status, state) == (400, None)
        status, state = receiver._resolve_subscription(f"{parsed.path}?token=x".encode())
        assert (status, state) == (404, None)


def test_validates_content_lengths_and_delivery_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    state = push_module._SubscriptionState(token="token", created_at=0)
    assert (
        push_module._validate_content_headers([(b"content-type", b"application/json")], state)
        is None
    )

    cases = [
        ([b"1", b"1"], 400, "ambiguous"),
        ([b"invalid"], 400, "invalid"),
        ([b"-1"], 400, "invalid"),
        ([str(push_module.MAX_PUSH_BODY_BYTES + 1).encode()], 413, "size limit"),
    ]
    for lengths, expected_status, rejection in cases:
        current = push_module._SubscriptionState(token="token", created_at=0)
        assert push_module._validate_content_length(lengths, current) == expected_status
        assert current.rejection is not None
        assert rejection in current.rejection

    assert push_module._validate_content_length([b"0"], state) is None
    assert (
        push_module._validate_content_length([str(push_module.MAX_PUSH_BODY_BYTES).encode()], state)
        is None
    )

    invalid = push_module._SubscriptionState(token="token", created_at=0)
    assert PushReceiver._record_delivery(invalid, b"[]") == 400
    assert PushReceiver._record_delivery(invalid, b"{}") == 400
    unknown = MessageToDict(_status(TaskState.TASK_STATE_COMPLETED))
    unknown["unknownField"] = True
    assert PushReceiver._record_delivery(invalid, json.dumps(unknown).encode()) == 400

    monkeypatch.setattr(push_module, "MAX_PUSH_EVENTS", 1)
    limited = push_module._SubscriptionState(token="token", created_at=0)
    working = MessageToDict(_status(TaskState.TASK_STATE_WORKING))
    completed = MessageToDict(_status(TaskState.TASK_STATE_COMPLETED))
    assert PushReceiver._record_delivery(limited, push_module.json.dumps(working).encode()) == 204
    assert PushReceiver._record_delivery(limited, push_module.json.dumps(working).encode()) == 204
    assert PushReceiver._record_delivery(limited, push_module.json.dumps(completed).encode()) == 429
    assert limited.rejection == "event limit exceeded"

    monkeypatch.setattr(push_module, "MAX_PUSH_TOTAL_BYTES", 1)
    oversized = push_module._SubscriptionState(token="token", created_at=0)
    assert PushReceiver._record_delivery(oversized, push_module.json.dumps(working).encode()) == 413

    repeated = push_module._SubscriptionState(token="token", created_at=0)
    body = push_module.json.dumps(working).encode()
    monkeypatch.setattr(push_module, "MAX_PUSH_TOTAL_BYTES", len(body) + 1)
    assert PushReceiver._record_delivery(repeated, body) == 204
    assert PushReceiver._record_delivery(repeated, body) == 413

    monkeypatch.setattr(push_module, "MAX_PUSH_EVENTS", 1_000)
    distinct = push_module._SubscriptionState(token="token", created_at=0)
    bodies = [
        json.dumps(MessageToDict(_status(state))).encode()
        for state in (
            TaskState.TASK_STATE_WORKING,
            TaskState.TASK_STATE_INPUT_REQUIRED,
            TaskState.TASK_STATE_COMPLETED,
        )
    ]
    monkeypatch.setattr(push_module, "MAX_PUSH_TOTAL_BYTES", sum(map(len, bodies)) - 1)
    assert PushReceiver._record_delivery(distinct, bodies[0]) == 204
    assert PushReceiver._record_delivery(distinct, bodies[1]) == 204
    assert PushReceiver._record_delivery(distinct, bodies[2]) == 413
    assert distinct.rejection == "total body size exceeded the limit"


@pytest.mark.parametrize(
    ("headers", "valid"),
    [
        ([(b"Authorization", b"Bearer token")], True),
        ([(b"authorization", b"bearer token")], True),
        ([(b"X-A2A-Notification-Token", b"token")], True),
        (
            [
                (b"Authorization", b"Bearer token"),
                (b"X-A2A-Notification-Token", b"token"),
            ],
            True,
        ),
        ([], False),
        ([(b"Authorization", b"Basic token")], False),
        ([(b"Authorization", b"Bearer wrong")], False),
        (
            [
                (b"Authorization", b"Bearer token"),
                (b"X-A2A-Notification-Token", b"wrong"),
            ],
            False,
        ),
        (
            [
                (b"Authorization", b"Bearer token"),
                (b"Authorization", b"Bearer token"),
            ],
            False,
        ),
        (
            [
                (b"X-A2A-Notification-Token", b"token"),
                (b"X-A2A-Notification-Token", b"token"),
            ],
            False,
        ),
    ],
)
def test_validates_standard_and_legacy_authentication(
    headers: list[tuple[bytes, bytes]],
    valid: bool,
) -> None:
    assert push_module._valid_authentication(headers, "token") is valid


@pytest.mark.asyncio
async def test_collects_terminal_task_and_message_payloads() -> None:
    async with PushReceiver(PushNotificationsConfig()) as receiver:
        task_subscription = receiver.register()
        task_subscription.bind(task_id="task", context_id="context")
        task = StreamResponse(
            task=Task(
                id="task",
                context_id="context",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        )
        assert await _deliver(task_subscription, task) == 204
        assert (await task_subscription.wait(1)).state == "completed"

        message_subscription = receiver.register()
        message_subscription.bind(task_id="task", context_id="context")
        message = StreamResponse(
            message=Message(
                message_id="message",
                task_id="task",
                context_id="context",
                role=Role.ROLE_AGENT,
                parts=[Part(text="done")],
            )
        )
        assert await _deliver(message_subscription, message) == 204
        assert (await message_subscription.wait(1)).text == "done"


@pytest.mark.asyncio
async def test_send_response_tolerates_closed_connections() -> None:
    await push_module._send_response(cast(asyncio.StreamWriter, _Writer(closing=True)), 204)
    await push_module._send_response(
        cast(asyncio.StreamWriter, _Writer(drain_error=ConnectionError())),
        204,
    )
    assert push_module._url_host("::1") == "[::1]"


@pytest.mark.asyncio
async def test_rejects_connections_beyond_concurrency_limit() -> None:
    receiver = PushReceiver(PushNotificationsConfig())
    receiver._connections = asyncio.Semaphore(0)
    writer = _Writer(wait_closed_error=ConnectionError())

    await receiver._handle_connection(
        cast(asyncio.StreamReader, object()),
        cast(asyncio.StreamWriter, writer),
    )

    assert writer.output.startswith(b"HTTP/1.1 503")
    assert writer.closed


@pytest.mark.asyncio
async def test_reports_connection_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_timeouts: list[float | None] = []

    class Timeout:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> None:
            return None

    def timeout(delay: float | None) -> Timeout:
        observed_timeouts.append(delay)
        return Timeout()

    class Reader:
        async def read(self, size: int) -> bytes:
            raise TimeoutError

    monkeypatch.setattr(push_module.asyncio, "timeout", timeout)
    receiver = PushReceiver(PushNotificationsConfig())
    writer = _Writer()

    await receiver._handle_connection(
        cast(asyncio.StreamReader, Reader()),
        cast(asyncio.StreamWriter, writer),
    )

    assert writer.output.startswith(b"HTTP/1.1 408")
    assert writer.closed
    assert observed_timeouts == [push_module.PUSH_CONNECTION_TIMEOUT_SECONDS]


@pytest.mark.asyncio
async def test_receiver_close_cancels_active_connection_handlers() -> None:
    started = asyncio.Event()

    class Reader:
        async def read(self, size: int) -> bytes:
            started.set()
            await asyncio.Event().wait()
            return b""

    receiver = PushReceiver(PushNotificationsConfig())
    writer = _Writer()
    handler = asyncio.create_task(
        receiver._handle_connection(
            cast(asyncio.StreamReader, Reader()),
            cast(asyncio.StreamWriter, writer),
        )
    )
    await started.wait()

    await receiver.close()

    assert handler.cancelled()
    assert not receiver._handler_tasks
    assert writer.closed
