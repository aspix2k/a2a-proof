# Running a2a-proof

## Commands

```console
a2a-proof check [CONFIG]
a2a-proof run [CONFIG]
a2a-proof run --transport GRPC
a2a-proof run --scenario smoke --jobs 4
a2a-proof run --format json
a2a-proof run --format junit --output a2a-proof.xml
a2a-proof run --evidence a2a-proof-evidence
a2a-proof diff [CONFIG] --against https://candidate-agent.example.com
a2a-proof diff --against https://candidate-agent.example.com --format json
```

`--scenario` is repeatable and matches exact, case-sensitive names in configuration order.
`--jobs` runs trials within a scenario concurrently, defaults to `1`, and is capped at `32`.

Exit code `0` means the contract passed, `1` means it failed, and `2` means execution or
configuration failed. In `diff`, the candidate contract result controls exit code `0` or `1`; a
failure to execute either side returns `2`.

## GitHub Actions

The first-party composite Action runs the contract from the source selected by the Action
reference. It installs the pinned runtime from that source's lock file and needs no write
permissions:

```yaml
name: A2A contracts

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  proof:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          persist-credentials: false
      - uses: aspix2k/a2a-proof@v0.14.0
        with:
          config: contracts/a2a-proof.yaml
```

The only Action input is `config`, which defaults to `a2a-proof.yaml`. Environment-backed headers
and invariants use ordinary GitHub Actions secrets. Use the CLI directly for scenario selection,
transport overrides, JUnit, or evidence; the Action does not upload response evidence
automatically because it may contain application data. Configure the workflow job as a required
check through the repository's rules when needed.

Use a full Action commit SHA when repository policy requires immutable dependencies. Do not expose
production credentials to a contract that a pull request can modify: the contract controls the
agent URL and header references. In particular, do not combine an untrusted checkout with
`pull_request_target` and production secrets.

## Local demo

`a2a-proof demo` starts a deterministic agent on an ephemeral loopback port and runs a real
JSON-RPC contract against it. The contract checks task state, structured routing data, inline file
size, and SHA-256. `a2a-proof demo --fail` changes one expectation, renders the normal failure
diagnostic, and exits `1`.

## Authentication

Configuration strings in the form `${NAME}` are expanded from the environment after YAML parsing.
Keep credentials out of the contract:

```console
export A2A_AUTHORIZATION='Bearer ...'
a2a-proof init https://agent.example.com \
  --header-env Authorization=A2A_AUTHORIZATION
```

The generated file stores `${A2A_AUTHORIZATION}`, not its value.

## Transports and discovery

The default `auto` mode lets the Agent Card select JSON-RPC, HTTP+JSON, or gRPC. Set `transport` to
`JSONRPC`, `HTTP+JSON`, or `GRPC` to require one binding. gRPC uses TLS by default; set
`grpc_tls: false` only for a trusted plaintext endpoint such as a local test server.

`run --transport` and `diff --transport` override that setting for one invocation without changing
the contract. Evidence manifests record the requested transport mode, including `auto`.

Interfaces must share the discovery URL's origin unless `allow_cross_origin_interfaces: true` is
set. Request headers are sent to an allowed cross-origin interface, so enable it only for a trusted
deployment.

Discovery reads `/.well-known/agent-card.json` and falls back to the legacy
`/.well-known/agent.json` path after a 404. `card_path` requires a custom location.

## Protocol extensions

List extension URIs under `agent.extensions`. `a2a-proof` validates them against the Agent Card and
activates them on every transport. `init` adds required extensions automatically. The legacy
`A2A-Extensions` header remains supported, but the dedicated field is preferred.

Extension activation is transport-level. AP2 mandate semantics require the optional setup in
[AP2 contracts](ap2.md).

## Push callback networking

The built-in push receiver listens on loopback by default. Remote agents need a public HTTPS route
to a fixed local port; configuration, authentication, limits, and failure behavior are covered in
[Push notification contracts](push-notifications.md).

## Evidence

`--evidence DIR` writes `manifest.json` and `failures.jsonl` through an atomic directory rename and
refuses to overwrite an existing path. The manifest binds the run to SHA-256 hashes of the
contract and Agent Card. JSONL contains bounded traces for failed trials and failed preflight or
aggregate latency checks.

Resolved headers, environment substitutions, and values named by `not_contains_env` are redacted
before truncation. Remote file URLs are omitted.

## Resource limits

Per turn, structured input is limited to 100 parts and 1 MB. File input is limited to 20 files,
10 MB each, and 20 MB total; resolved paths cannot escape the contract directory. Responses are
limited to 1,000 stream events, 1,000 data parts, 1,000 file parts, 1 MB each of text and structured
data, and 20 MB of inline raw data.

Embedded schemas are limited to 100 KB and 50 levels. Request timeouts are configurable, regular
expressions have a 100 ms evaluation limit, redirects are disabled, and evidence records at most
100 failed trials with bounded previews.
