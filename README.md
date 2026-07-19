# a2a-proof

[![CI](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-%E2%89%A599%25-brightgreen)](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/aspix2k/a2a-proof)](https://github.com/aspix2k/a2a-proof/releases)
[![PyPI](https://img.shields.io/pypi/v/a2a-proof)](https://pypi.org/project/a2a-proof/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

Black-box contract tests for A2A agents.

`a2a-proof` discovers an agent, sends real A2A requests, and checks the observable result.
It does not need the agent's source code, framework, prompts, or model provider.

## Scope

`a2a-proof` complements the official A2A testing tools:

- [A2A TCK](https://github.com/a2aproject/a2a-tck) checks protocol conformance.
- [A2A Inspector](https://github.com/a2aproject/a2a-inspector) supports interactive inspection
  and debugging.
- `a2a-proof` runs repeatable, user-defined behavior checks locally or in CI.

Use the TCK to verify that an implementation follows the A2A specification. Use `a2a-proof` to
verify that a deployed agent still behaves as your application expects. The current release targets
A2A 1.0 and supports JSON-RPC, HTTP+JSON, and gRPC.

## Quick start

```console
uvx a2a-proof init https://agent.example.com
uvx a2a-proof run
```

`init` reads the Agent Card and creates `a2a-proof.yaml`. If a skill contains examples,
the first example becomes a scenario; otherwise the file contains one smoke test. The generated
scenarios verify protocol success. Add the assertions you care about before using them in CI.

## Configuration

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/aspix2k/a2a-proof/main/schema/a2a-proof.schema.json

version: 1

agent:
  url: https://agent.example.com
  timeout: 30
  transport: auto
  extensions:
    - https://agent.example.com/extensions/structured-input/v1
  headers:
    Authorization: ${A2A_AUTHORIZATION}

card:
  skills:
    contains: [summarize]
  capabilities:
    streaming: true
  input_modes:
    contains: application/pdf

defaults:
  trials: 3
  pass_rate: 0.66

scenarios:
  - name: capital of France
    message: What is the capital of France?
    expect:
      state: completed
      max_seconds: 10
      max_first_event_seconds: 2
      text:
        contains: Paris
        not_contains: error
        matches: "(?i)capital"

  - name: clarification
    turns:
      - message: Book a table
        expect:
          state: input_required
          text:
            contains: city
      - message: Paris
        expect:
          state: completed

  - name: nondeterministic answer
    message: Name a primary color
    trials: 5
    pass_rate: 0.8
    expect:
      text:
        matches: "(?i)red|blue|yellow"

  - name: structured forecast
    message: Return a structured forecast
    data:
      action: forecast
      city: Paris
    expect:
      data:
        - source: artifact
          artifact_name: forecast
          media_type: application/json
          path: /city
          equals: Paris
        - path: /temperature
          gte: 18
          lt: 30
        - path: /summary
          matches: "(?i)sunny|cloudy"
        - path: /alerts
          exists: true
        - path: /forecast
          json_schema:
            type: object
            required: [date, conditions]
            properties:
              date: {type: string}
              conditions: {type: string}

  - name: summarize a document
    message: Summarize this file
    files:
      - path: fixtures/report.pdf
        media_type: application/pdf
    expect:
      state: completed
      states:
        contains_in_order: [working, completed]
      files:
        source: artifact
        artifact_name: summary
        media_type: text/plain
        count: 1
```

Each scenario uses a single turn or `turns`. A turn may contain `message`, `data`, `files`, or any
combination of them. A mapping under `data` creates one A2A data part; use a list to send several
parts. Multi-turn scenarios preserve the A2A context and continue the task after `input_required`
and `auth_required` responses.

`card` runs once, before scenario messages. Skill checks use stable skill IDs, while input and
output mode comparisons are case-insensitive. A failed card check stops the run with exit code `1`.
`defaults` supplies `trials` and `pass_rate` only when a scenario omits that field.

Text assertions support `contains`, `not_contains`, `equals`, and Python regular expressions in
`matches`. Strings are case-sensitive unless `case_sensitive: false` is set. Failed, rejected,
and canceled tasks fail by default unless that state is explicitly expected.

`max_seconds` bounds the complete turn. `max_first_event_seconds` bounds the time until the first
A2A response event observed by the client, which is useful for streaming responsiveness checks.

`trials` repeats a scenario. `pass_rate` is the minimum successful fraction and defaults to `1`.

`states.equals` checks the complete observed state trajectory. `states.contains_in_order` checks a
subsequence and allows intermediate states. Consecutive duplicate states are collapsed before
evaluation.

Structured assertions inspect A2A `data` parts from messages or artifacts. `path` is an
[RFC 6901 JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901); an empty path checks the complete
JSON value. Each assertion must match at least one data part after the optional source, artifact
name, and media type filters are applied. Use exactly one assertion type per entry:

- `equals` compares JSON values without treating booleans as numbers.
- `exists` checks whether a non-root pointer is present or absent.
- `matches` applies a bounded regular expression to string values.
- `gt`, `gte`, `lt`, and `lte` define one or more numeric bounds.
- `json_schema` validates a value against an inline JSON Schema Draft 2020-12 document.

Embedded schemas may use local references such as `#/$defs/item`; external references are rejected
and never fetched.

File paths are resolved relative to the contract file and sent as inline `raw` parts with a
filename and media type. A file entry may be a path string or an object with `path` and an optional
`media_type`. Response file checks match metadata from `raw` and `url` parts by source, artifact
name, filename, media type, kind, and exact count. Remote file URLs are never fetched or copied into
reports; reports retain only the part kind and non-URL metadata.

## Editor support

The published [configuration schema](schema/a2a-proof.schema.json) provides completion and inline
validation in editors that support YAML language-server schema comments. `init` writes the comment
automatically. For an existing file, add the first line shown in the configuration example above.

## Output

A passing scenario produces a compact summary:

```console
$ a2a-proof run
┏━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Result ┃ Scenario ┃ Trials ┃ Time ┃
┡━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ PASS   │ Echo     │    1/1 │  2ms │
└────────┴──────────┴────────┴──────┘

1 scenario passed in 2ms
```

A failed assertion identifies the scenario and returns exit code `1`:

```console
$ a2a-proof run --verbose
┏━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Result ┃ Scenario ┃ Trials ┃ Time ┃
┡━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ FAIL   │ Echo     │    0/1 │  1ms │
└────────┴──────────┴────────┴──────┘

Echo
  trial 1, turn 1: response text is not equal to the expected value
  response: echo: Hello

1 scenario failed in 1ms
```

## Official sample

The repository includes a contract for the A2A project's
[Hello World agent](https://github.com/a2aproject/a2a-samples/tree/main/samples/python/agents/helloworld).
Start that agent on its default port, then run from an `a2a-proof` checkout:

```console
uv run a2a-proof run examples/official-helloworld.yaml
```

## Authentication

Keep secrets in environment variables. Configuration values in the form `${NAME}` are expanded
after YAML parsing.

```console
export A2A_AUTHORIZATION='Bearer ...'
a2a-proof init https://agent.example.com \
  --header-env Authorization=A2A_AUTHORIZATION
```

The generated file stores the reference, not the secret.

## Transports

The default `auto` mode lets the Agent Card select JSON-RPC, HTTP+JSON, or gRPC. Set `transport`
to `JSONRPC`, `HTTP+JSON`, or `GRPC` to require one binding. gRPC uses TLS by default; set
`grpc_tls: false` only for a trusted plaintext endpoint such as a local test server.

Agent interfaces must share the discovery URL's origin by default. If a trusted deployment
intentionally separates them, set `allow_cross_origin_interfaces: true` or pass
`--allow-cross-origin` to `init`. Request headers will then be sent to that interface.

Discovery uses `/.well-known/agent-card.json` and falls back to the legacy
`/.well-known/agent.json` path after a 404. Use `card_path` to require a custom path.

## Protocol extensions

List extension URIs under `agent.extensions`. `a2a-proof` checks them against the Agent Card and
activates them on every JSON-RPC, HTTP+JSON, or gRPC request. Execution stops before the first agent
request if a configured extension is not advertised or a required extension is not configured.
`init` adds required extensions automatically.

Existing configurations that set `A2A-Extensions` under `headers` remain valid. The dedicated
`extensions` field is preferred because it is explicit and lets `init` populate required
capabilities.

Extension activation does not implement the extension's semantics. In particular, this release
does not add AP2 mandate assertions. The current official AP2 Python samples pin
[`a2a-sdk==0.3.24`](https://github.com/google-agentic-commerce/AP2/blob/main/code/samples/python/pyproject.toml),
while `a2a-proof` targets A2A 1.0, so those sample agents are not compatible wire-level test targets
yet.

## Commands and output

```console
a2a-proof check [CONFIG]
a2a-proof run [CONFIG]
a2a-proof run --format json
a2a-proof run --format junit --output a2a-proof.xml
a2a-proof run --verbose
a2a-proof run --scenario "capital of France"
a2a-proof run --scenario smoke --scenario regression
```

`--scenario` is repeatable and runs exact, case-sensitive scenario names in configuration order.
Unknown names fail before connecting to the agent.

Exit code `0` means all scenarios passed, `1` means a contract failed, and `2` means the command
or configuration could not be executed. JUnit output is suitable for CI test reports.

## Safety limits

Per turn, outgoing structured input is limited to 100 parts and 1 MB. File input is limited to 20
files, 10 MB per file, and 20 MB in total; paths cannot escape the contract directory, including
through symlinks. Responses are limited to 1,000 stream events, 1,000 structured data parts, 1,000
file parts, 1 MB each of text and structured data, and 20 MB of inline raw data. At most 20 extension
URIs and 8,000 extension-header characters may be configured. Embedded JSON Schemas are limited to
100 KB and 50 levels. Requests have a configurable timeout, text and data `matches` checks have a
100 ms evaluation limit, HTTP redirects are disabled, external schema references are rejected, and
file URLs are never fetched. Treat the tested agent and all returned content as untrusted input.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```console
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run python scripts/generate_schema.py --check
uv run zizmor --persona=pedantic --offline --strict-collection .
uv run pytest --cov=a2a_proof
uv run mutmut run --max-children 1
uv build
```

Mutation testing targets the deterministic contract core. Network transports remain covered by
real JSON-RPC, HTTP+JSON, and gRPC end-to-end tests.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow and
[SECURITY.md](SECURITY.md) for private vulnerability reports.

## License

MIT
