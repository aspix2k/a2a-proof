# Delegation contracts

The other contracts in `a2a-proof` observe what an agent answers. A delegation contract observes
what it *sends*: `a2a-proof` runs a recording A2A agent, the tested agent delegates work to it, and
the contract asserts on the request that arrived there.

That covers two risks no response check can reach: an orchestrator that silently stopped calling
its specialist, and an orchestrator that forwards your credentials to it.

## Running the recording agent

```yaml
downstream:
  name: Inventory agent
  skills: [lookup]
  reply:
    text: 12 units are in stock
    data: {units: 12}

scenarios:
  - name: stock lookup
    message: How many units of SKU-42 are left? Ask the inventory agent at {{downstream_url}}
    expect:
      state: completed
      delegation:
        count: 1
        text:
          contains: SKU-42
        data:
          - path: /sku
            equals: SKU-42
        not_contains_env: A2A_AUTHORIZATION
```

The recording agent listens on loopback, serves an Agent Card at
`/.well-known/agent-card.json`, accepts A2A JSON-RPC `SendMessage` at `/a2a`, and answers every
call with the configured `reply`. It never advertises streaming, so a conformant client uses
`SendMessage`; any other method is recorded and answered with JSON-RPC error `-32601`, which the
contract reports.

`{{downstream_url}}` is replaced with the running agent's base URL in the turn's `message` and in
every string inside its `data`. Substitution happens after configuration loads, so the contract
never contains a port that changes between runs. This is how the tested agent learns the address —
through the same input path a user would use — which keeps the contract black-box.

## Checks

| Field | Meaning |
| --- | --- |
| `count` | Exact number of downstream calls the turn must produce |
| `text` | `contains`, `not_contains`, `equals`, or `matches` for the text of one call |
| `data` | JSON Pointer assertions for the structured parts of one call |
| `not_contains_env` | Environment variables whose values must appear in no call |

`text` and `data` are satisfied when at least one recorded call matches all of them; the
diagnostics describe the first call when none does. `not_contains_env` applies to every call and
inspects the complete request body and its headers, which is what catches a forwarded
`Authorization` header. Values are read from the environment in memory, and no downstream request
body is written to reports or to an evidence bundle.

## Limits

Delegation checks require sequential trials, because calls from concurrent trials cannot be
attributed to one turn; `--jobs` above `1` fails with exit code `2`. A run records at most 100
calls and 1 MB per call.

A remote agent needs a public HTTPS route to the recording agent, configured exactly as the
[push receiver](push-notifications.md#remote-agents):

```yaml
downstream:
  listen_host: 0.0.0.0
  listen_port: 8788
  public_url: https://downstream.example.net
```
