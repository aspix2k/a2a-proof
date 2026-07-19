# a2a-proof

[![CI](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/aspix2k/a2a-proof/graph/badge.svg)](https://codecov.io/gh/aspix2k/a2a-proof)
[![Release](https://img.shields.io/github/v/release/aspix2k/a2a-proof)](https://github.com/aspix2k/a2a-proof/releases)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

Black-box contract tests for A2A agents.

`a2a-proof` discovers an agent, sends real A2A requests, and checks the observable result.
It does not need the agent's source code, framework, prompts, or model provider.

## Quick start

```console
uv tool install git+https://github.com/aspix2k/a2a-proof@v0.1.0
a2a-proof init https://agent.example.com
a2a-proof run
```

`init` reads the Agent Card and creates `a2a-proof.yaml`. If a skill contains examples,
the first example becomes a scenario; otherwise the file contains one smoke test. The generated
scenarios verify protocol success. Add the assertions you care about before using them in CI.

## Configuration

```yaml
version: 1

agent:
  url: https://agent.example.com
  timeout: 30
  transport: auto
  headers:
    Authorization: ${A2A_AUTHORIZATION}

scenarios:
  - name: capital of France
    message: What is the capital of France?
    expect:
      state: completed
      max_seconds: 10
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
```

Each scenario uses either `message` or `turns`. Multi-turn scenarios preserve the A2A context
and continue the task after `input_required` and `auth_required` responses.

Text assertions support `contains`, `not_contains`, `equals`, and Python regular expressions in
`matches`. Strings are case-sensitive unless `case_sensitive: false` is set. Failed, rejected,
and canceled tasks fail by default unless that state is explicitly expected.

`trials` repeats a scenario. `pass_rate` is the minimum successful fraction and defaults to `1`.

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

## Commands and output

```console
a2a-proof check [CONFIG]
a2a-proof run [CONFIG]
a2a-proof run --format json
a2a-proof run --format junit --output a2a-proof.xml
a2a-proof run --verbose
```

Exit code `0` means all scenarios passed, `1` means a contract failed, and `2` means the command
or configuration could not be executed. JUnit output is suitable for CI test reports.

## Safety limits

Per turn, responses are limited to 1,000 stream events and 1,000,000 text characters. Requests
have a configurable timeout, regular-expression checks have a 100 ms evaluation limit, HTTP
redirects are disabled, and artifact URLs are never fetched. Treat the tested agent and all
returned text as untrusted input.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```console
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run ty check
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
