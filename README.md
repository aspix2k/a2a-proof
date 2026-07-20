# a2a-proof

[![CI](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-%E2%89%A599%25-brightgreen)](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/aspix2k/a2a-proof)](https://github.com/aspix2k/a2a-proof/releases)
[![PyPI](https://img.shields.io/pypi/v/a2a-proof)](https://pypi.org/project/a2a-proof/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

Black-box contract tests for A2A agents.

`a2a-proof` discovers a deployed agent, sends real A2A requests, and checks its observable
behavior. It needs no access to the agent's source code, framework, prompts, or model provider.

The official [A2A TCK](https://github.com/a2aproject/a2a-tck) checks protocol conformance, and the
[A2A Inspector](https://github.com/a2aproject/a2a-inspector) supports interactive debugging.
`a2a-proof` adds repeatable behavior and lifecycle contracts for local development and CI.

It targets A2A 1.0 over JSON-RPC, HTTP+JSON, and gRPC. The SDK compatibility layer also supports
AP2 v0.2.0 agents that expose A2A 0.3 JSON-RPC, including signed mandate-to-receipt payment flows.

## What you can prove

| Risk | Contract |
| --- | --- |
| A prompt, model, or backend change alters an answer | Text, structured data, JSON Schema, and file assertions |
| An LLM succeeds only some of the time or gets slower | Repeated trials, pass rates, parallel runs, and p50/p95 latency |
| A long-running task breaks after acceptance | Multi-turn state trajectories, cancellation, persistence, and push delivery |
| Staging no longer behaves like production | Agent Card preflight and deployment diff |
| Agent text leaks a secret or system prompt | Global invariants and bounded failure evidence |
| An agent produces an invalid payment proof | Signed AP2 mandate-chain and receipt verification |

## Quick start

```console
uvx a2a-proof init https://agent.example.com
```

`init` reads the Agent Card and creates `a2a-proof.yaml`. Replace its smoke scenario with behavior
your users depend on:

```yaml
version: 1

agent:
  url: https://agent.example.com

defaults:
  trials: 5
  pass_rate: 0.8

scenarios:
  - name: billing dispute routing
    message: A customer says their card was charged twice for order 4815.
    latency:
      p95_seconds: 15
    expect:
      state: completed
      data:
        - path: /queue
          equals: billing-disputes
        - path: /priority
          matches: "(?i)^high$"
```

```console
$ uvx a2a-proof run
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━┓
┃ Result ┃ Scenario                ┃ Trials ┃ Time ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━┩
│ PASS   │ billing dispute routing │    4/5 │ 3.2s │
└────────┴─────────────────────────┴────────┴──────┘

1 scenario passed in 3.2s
```

Run one scenario, save failure evidence, or compare the same contract against another deployment:

```console
uvx a2a-proof run --scenario "billing dispute routing"
uvx a2a-proof run --format junit --output a2a-proof.xml --evidence evidence
uvx a2a-proof diff --against https://candidate-agent.example.com
```

## Documentation

- [Writing contracts](docs/contracts.md)
- [Assertions](docs/assertions.md)
- [Task lifecycle](docs/lifecycle.md)
- [Push notifications](docs/push-notifications.md)
- [AP2 contracts](docs/ap2.md)
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

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).
