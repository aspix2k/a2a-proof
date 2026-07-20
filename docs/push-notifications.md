# Push notification contracts

Push contracts verify the event an agent actually delivers after accepting long-running work. They
do not duplicate the A2A TCK's configuration CRUD checks.

For an agent running on the same machine, enable the built-in callback receiver with its safe
loopback defaults:

```yaml
push_notifications: {}

scenarios:
  - name: asynchronous report export
    turns:
      - message: Export the July account activity as CSV
        return_immediately: true
        push_notification: true
        expect:
          state: working
      - action: await_push
        timeout_seconds: 60
        expect:
          state: completed
          states:
            contains_in_order: [working, completed]
          files:
            source: artifact
            filename: account-activity.csv
            media_type: text/csv
            count: 1
```

`push_notification: true` asks the agent to return its initial task immediately and sends a unique
callback URL with Bearer authentication. `action: await_push` collects delivered A2A events until a
terminal event arrives, then evaluates the ordinary text, data, file, state, timing, AP2, and global
invariant checks.

The two turns must be adjacent. The initial response must contain a task ID, and the Agent Card
must advertise push notification support.

## Remote agents

A remote agent needs a public HTTPS route to the local receiver. Use a fixed listener port behind a
reverse proxy or tunnel:

```yaml
push_notifications:
  listen_host: 0.0.0.0
  listen_port: 8787
  public_url: https://proof.example.net
```

Route `https://proof.example.net/.a2a-proof/push/*` to local port `8787`. The public endpoint
terminates TLS; the built-in receiver itself speaks HTTP. `public_url` may include a fixed path
prefix, but not credentials, a query, or a fragment. Plain HTTP is accepted only for loopback and
private-network callback hosts.

Each trial gets an unpredictable route and notification token. The receiver accepts standard
`Authorization: Bearer` authentication and the legacy token header emitted by A2A Python SDK 1.1.1.
Authenticated deliveries are bound to the task and context returned by the initial request, and
exact retries are deduplicated. Both the standard `application/a2a+json` media type and the SDK's
`application/json` output are accepted.

## Limits and failures

The listener accepts at most 100 concurrent subscriptions and 64 connections. A subscription is
bounded to 1,000 distinct events, 1 MB per request, and 20 MB total. Connections time out after 10
seconds. The `await_push` waiting window starts when that action runs and lasts for at most
`timeout_seconds` or the agent timeout; reported push latency still starts when the callback is
registered so it includes the agent's initial processing time.

A missing callback, invalid payload, wrong task or context, exceeded limit, authenticated rejection,
or delivery failure fails the trial with exit code `1`. Listener setup errors return exit code `2`.
Failed push turns use the same JSON, JUnit, and `--evidence` output as request/response turns.
