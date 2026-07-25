# Recording and replaying a run

`a2a-proof record` runs a contract against the live agent and writes every response it observed to
a cassette. `a2a-proof run --replay` evaluates a contract against that cassette instead of the
agent, so the checks run with no network, no credentials, and no agent bill.

```console
a2a-proof record --output cassettes/today.json
a2a-proof run --replay cassettes/today.json
```

## What this is for

- **Contract work without the agent.** Rewriting assertions, adding data checks, or reviewing a
  contract change in a pull request needs recorded responses, not a live deployment.
- **A behavior baseline.** A cassette from a known-good day is a record of what the agent actually
  answered, kept next to the contract that accepted it.
- **Reproducible failure analysis.** A cassette recorded while the agent misbehaved replays that
  exact run as often as needed.

Comparing two live deployments remains [`a2a-proof diff`](operations.md#commands); a cassette
compares a contract against the past, not one agent against another.

## What a cassette holds

One JSON document: the contract's SHA-256, the agent URL, the Agent Card as recorded, and every
turn in the order the run produced it. A turn keeps the state, the state trajectory, the response
text, structured data parts, file-part metadata with the computed SHA-256, and both timings, which
is exactly the surface every assertion reads. Requests are not recorded, so nothing the contract
sends is duplicated in the file.

The recorded Agent Card lets `card` preflight assertions run during replay too.

## Reading a cassette

Replay serves recorded turns in order, including the responses to `cancel`, `get_task`, and
`subscribe` actions. A contract that asks for more turns than the cassette holds fails that trial
with a clear diagnostic, which is what a contract that grew new turns since the recording does.
Re-record it.

Assertions themselves are evaluated fresh, so a changed expectation produces a real pass or fail
against the recorded responses. When the cassette's contract digest no longer matches, `run` notes
that on standard error and continues.

Replay needs `--jobs 1`, because a cassette is an ordered list rather than a keyed store. Contracts
using [push notifications](push-notifications.md) or [delegation](delegation.md) cannot be
replayed: both assert on live callbacks the cassette does not contain.

## Handling a cassette

A cassette contains real agent responses, so treat it like evidence: keep it out of a public
repository unless the responses are safe to publish, and record with a contract whose credentials
come from the environment rather than the file. Reading one is bounded to 50 MB.
