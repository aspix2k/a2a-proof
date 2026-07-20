# Task lifecycle contracts

Lifecycle checks use ordinary multi-turn scenarios. An input turn can request the initial
non-terminal task with `return_immediately: true`; later turns can subscribe, cancel, or retrieve
that task.

```yaml
scenarios:
  - name: cancellation persists
    turns:
      - message: Start a long-running export
        return_immediately: true
        expect:
          state: working
      - action: cancel
        expect:
          state: canceled
      - action: get_task
        history_length: 10
        expect:
          state: canceled
```

Actions reuse the preceding task ID and context and accept the same `expect` block as message
turns. `history_length` is optional and valid only for `get_task`.

Use `subscribe` to resume observation of active work and assert the complete subscription stream:

```yaml
scenarios:
  - name: export survives client reconnect
    turns:
      - message: Start a long-running export
        return_immediately: true
        expect: {state: working}
      - action: subscribe
        expect:
          state: completed
          states:
            contains_in_order: [working, completed]
          files:
            source: artifact
            filename: export.csv
            kind: raw
            min_size_bytes: 1
```

The Agent Card must advertise streaming. The first subscription event must be the current task
snapshot, every event must retain the preceding task ID and context, and the stream must end in a
terminal or interrupted state. All ordinary text, data, file, AP2, timing, and invariant checks
apply to the collected result.

A lifecycle action cannot be the first turn. Execution fails if the preceding response did not
contain a task ID or if the selected transport does not expose the requested operation.

JSON-RPC, HTTP+JSON, and gRPC use their native A2A lifecycle methods. For a streaming Agent Card,
normal turns and `subscribe` use streaming collection while `return_immediately` uses a separate
non-streaming client as required by the SDK.

Long-running work can also wait for the agent's actual callback instead of polling with
`get_task`. See [Push notification contracts](push-notifications.md).
