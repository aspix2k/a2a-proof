# Writing contracts

An `a2a-proof.yaml` file names one agent and one or more scenarios. The published
[JSON Schema](../schema/a2a-proof.schema.json) provides the complete machine-readable reference.

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/aspix2k/a2a-proof/main/schema/a2a-proof.schema.json

version: 1

agent:
  url: https://agent.example.com
  timeout: 30
  transport: auto
  headers:
    Authorization: ${A2A_AUTHORIZATION}

card:
  skills:
    contains: [summarize]
  capabilities:
    streaming: true

invariants:
  text:
    not_contains: ["system prompt", "BEGIN PRIVATE KEY"]
    not_contains_env: A2A_AUTHORIZATION

defaults:
  trials: 3
  pass_rate: 0.66

scenarios:
  - name: summarize
    message: Summarize this document
    data:
      language: en
    files:
      - path: fixtures/report.pdf
        media_type: application/pdf
    expect:
      state: completed
      text:
        not_contains: error
```

## Scenarios and turns

A scenario uses either the single-turn `message`, `data`, and `files` fields or a `turns` list.
Multi-turn scenarios preserve the A2A context and continue tasks after `input_required` and
`auth_required` responses.

```yaml
scenarios:
  - name: clarification
    turns:
      - message: Book a table
        expect:
          state: input_required
          text: {contains: city}
      - message: Paris
        expect:
          state: completed
```

`data` accepts one JSON value or a list of values. Each value becomes an A2A data part. File paths
are resolved relative to the contract file and sent as inline raw parts. A file may be a path
string or an object with `path` and optional `media_type`.

`defaults` supplies `trials` and `pass_rate` only when a scenario omits the field. A scenario can
also define aggregate `latency` limits:

```yaml
defaults:
  trials: 5
  pass_rate: 0.8

scenarios:
  - name: nondeterministic answer
    message: Name a primary color
    latency:
      p50_seconds: 5
      p95_seconds: 10
    expect:
      text:
        matches: "(?i)red|blue|yellow"
```

Long-running scenarios can attach a per-trial callback and assert the delivered event with
`push_notification: true` followed by `action: await_push`. See
[Push notification contracts](push-notifications.md).

## Agent Card preflight

`card` assertions run once before scenario messages. Skill checks use stable skill IDs; input and
output mode comparisons are case-insensitive. A failed check stops the run with exit code `1`.

```yaml
card:
  skills: {contains: [summarize, translate]}
  capabilities: {streaming: true}
  input_modes: {contains: application/pdf}
  output_modes: {contains: text/plain}
```

## Global invariants

`invariants.text` applies to every response turn. `not_contains` accepts public forbidden strings.
`not_contains_env` accepts environment variable names and resolves their values only in memory.
Both accept one string or a list; set `case_sensitive: false` for case-insensitive matching.

A missing or empty environment variable stops `check` and `run` before an agent request. If an
invariant fails, that turn's text, data, and file metadata are removed before any report or
evidence is rendered.
