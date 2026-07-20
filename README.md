# a2a-proof

[![CI](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-%E2%89%A599%25-brightgreen)](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/aspix2k/a2a-proof)](https://github.com/aspix2k/a2a-proof/releases)
[![PyPI](https://img.shields.io/pypi/v/a2a-proof)](https://pypi.org/project/a2a-proof/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

Black-box contract tests for A2A agents.

`a2a-proof` discovers a deployed agent, sends real A2A requests, and checks its observable
behavior. It needs no access to the agent's source code, framework, prompts, or model provider.

## Why

The official [A2A TCK](https://github.com/a2aproject/a2a-tck) checks protocol conformance, and the
[A2A Inspector](https://github.com/a2aproject/a2a-inspector) supports interactive debugging.
`a2a-proof` adds repeatable behavior and lifecycle contracts for local development and CI.

It targets A2A 1.0 over JSON-RPC, HTTP+JSON, and gRPC. The SDK compatibility layer also supports
AP2 v0.2.0 agents that expose A2A 0.3 JSON-RPC.

## Quick start

```console
uvx a2a-proof init https://agent.example.com
uvx a2a-proof run
```

`init` reads the Agent Card and creates `a2a-proof.yaml`. Add the behavior you depend on:

```yaml
version: 1

agent:
  url: https://agent.example.com

scenarios:
  - name: capital of France
    message: What is the capital of France?
    expect:
      state: completed
      max_seconds: 10
      text:
        contains: Paris
```

```console
$ a2a-proof run
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Result ┃ Scenario          ┃ Trials ┃ Time ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ PASS   │ capital of France │    1/1 │ 1.2s │
└────────┴───────────────────┴────────┴──────┘

1 scenario passed in 1.2s
```

## What it covers

- Text, structured data, and file parts.
- Single-turn, multi-turn, repeated, and parallel trials.
- Agent Card, state trajectory, latency, and global leak invariants.
- Task cancellation and persistence through real A2A lifecycle operations.
- Signed AP2 payment and checkout mandate chains, including offline inspection.
- Baseline-to-candidate deployment comparison.
- Terminal, JSON, JUnit, and bounded evidence output.

Compare the same contract against a candidate deployment:

```console
a2a-proof diff --against https://candidate-agent.example.com
```

The diff reports regressions, improvements, changed results, and unchanged results. It compares
contract outcomes rather than raw model text; the candidate result controls the exit status.

## Documentation

- [Writing contracts](docs/contracts.md)
- [Assertions](docs/assertions.md)
- [Task lifecycle](docs/lifecycle.md)
- [AP2 mandate contracts](docs/ap2.md)
- [Running in development and CI](docs/operations.md)
- [Configuration schema](schema/a2a-proof.schema.json)

The schema provides completion and inline validation in YAML-aware editors. `init` links it
automatically.

## Safety

Agent responses and file metadata are treated as untrusted input. Requests, response parts,
regular expressions, embedded schemas, evidence, and local file access are bounded. Redirects,
credential-bearing URLs, external schema references, and cross-origin interfaces are rejected by
default. Remote file URLs are never fetched or written to reports.

See [SECURITY.md](SECURITY.md) for private vulnerability reports.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```console
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run python scripts/generate_schema.py --check
uv run pytest --cov=a2a_proof
uv run mutmut run --max-children 1
```

The test suite includes real JSON-RPC, HTTP+JSON, and gRPC exchanges. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## License

MIT
