# Task lifecycle contracts

Lifecycle checks use ordinary multi-turn scenarios. An input turn can request the initial
non-terminal task with `return_immediately: true`; later turns can cancel or retrieve that task.

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

A lifecycle action cannot be the first turn. Execution fails if the preceding response did not
contain a task ID or if the selected transport does not expose the requested operation.

JSON-RPC, HTTP+JSON, and gRPC use their native A2A lifecycle methods. For a streaming Agent Card,
normal turns keep streaming collection while `return_immediately` uses a separate non-streaming
client as required by the SDK.
