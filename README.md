# a2a-proof

[![CI](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/aspix2k/a2a-proof/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/a2a-proof)](https://pypi.org/project/a2a-proof/)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.14-blue)](https://www.python.org/)

Black-box behavior contracts for deployed [A2A](https://a2a-protocol.org/) agents.

`a2a-proof` discovers an Agent Card, sends real requests, and checks only observable behavior. It
does not need the agent's source code, framework, prompts, or model provider.

## Status

The project is alpha software. It targets A2A 1.0 over JSON-RPC, HTTP+JSON, and gRPC on Python
3.11–3.14. An optional compatibility path covers AP2 v0.2.0 agents that expose A2A 0.3 JSON-RPC.

Contracts can cover:

- text, structured data, files, task states, and latency;
- repeated trials with pass-rate bounds and parallel execution;
- Agent Card preflight, deployment diffs, and offline cassette replay;
- push delivery, downstream delegation, and signed AP2 mandates and receipts.

The official [A2A TCK](https://github.com/a2aproject/a2a-tck) remains the protocol-conformance
suite. `a2a-proof` focuses on application behavior.

## Quick start

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, run a deterministic
loopback contract without credentials:

```console
uvx a2a-proof demo
uvx a2a-proof demo --fail
```

The second command intentionally fails and exits `1`. To create and run a contract against a
deployed agent:

```console
uvx a2a-proof init https://agent.example.com
uvx a2a-proof check
uvx a2a-proof run
```

`init` writes `a2a-proof.yaml` from the Agent Card. Replace its smoke scenario with behavior your
users depend on before treating the result as a useful contract.

## Validation

`a2a-proof check [CONFIG]` validates a contract without contacting the agent. A run exits `0` when
all selected scenarios pass, `1` when a contract fails, and `2` for configuration or execution
errors.

Repository checks and release instructions are in [CONTRIBUTING.md](CONTRIBUTING.md). CI enforces
formatting, linting, type checks, schema freshness, workflow security, tests with at least 99%
coverage, package validation, supported Python versions, and macOS/Windows compatibility.

## GitHub Actions

After checkout, the composite action runs the repository's `a2a-proof.yaml` contract:

```yaml
- uses: aspix2k/a2a-proof@v0.16.0
```

See [Running in development and CI](docs/operations.md) for configuration and evidence handling.

## Safety

Agent responses and file metadata are treated as untrusted input. Requests, evidence, local file
access, redirects, schemas, and regular expressions are bounded or validated. See
[SECURITY.md](SECURITY.md) for the security model and private vulnerability reports.

## Documentation

- [Write a contract](docs/contracts.md) and [choose assertions](docs/assertions.md)
- [Run locally or in CI](docs/operations.md)
- [Test task lifecycles](docs/lifecycle.md), [push delivery](docs/push-notifications.md), or
  [delegation](docs/delegation.md)
- [Record and replay agent responses](docs/cassettes.md)
- [Verify AP2 mandates and receipts](docs/ap2.md)
- [Run external-agent examples](docs/showcases.md)
- [Browse the configuration schema](schema/a2a-proof.schema.json)
- [Read the changelog](CHANGELOG.md), [security policy](SECURITY.md), or
  [contribution guide](CONTRIBUTING.md)

Licensed under the [MIT License](LICENSE).
