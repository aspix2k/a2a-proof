# External agent showcases

These contracts run against unmodified agents maintained outside this repository. CI validates the
YAML offline; the live commands below are opt-in so upstream builds and networks cannot break a
normal `a2a-proof` build.

| Contract | Upstream implementation | Behavior checked |
| --- | --- | --- |
| `official-helloworld.yaml` | A2A Python SDK 1.1.0 or Go SDK 2.3.1 | Streaming task and exact artifact text |
| `official-multitenancy-reverse.yaml` | A2A Python SDK 1.1.0 | Agent Card discovery and behavior on a routed sub-path |
| `official-rust-lifecycle.yaml` | A2A Rust SDK | JSON-RPC, HTTP+JSON, and gRPC echo and cancellation |
| `turul-interrupting.yaml` | Turul A2A 0.1.32 | Same-task continuation from `input_required` to `completed` |

The matrix was exercised on 2026-07-20 against A2A samples revision
[`ffd5d82`](https://github.com/a2aproject/a2a-samples/tree/ffd5d8292f81589ae3970e610e6bac950005b934),
A2A Rust revision
[`ed8cb2b`](https://github.com/a2aproject/a2a-rs/tree/ed8cb2b5fbeaf2286afb8a1877d49285cce05dc0),
and Turul revision
[`07a0848`](https://github.com/aussierobots/turul-a2a/tree/07a0848a27a557958d4f0fb5dba8a0971f66774d).

## Official Python and Go samples

Clone `a2aproject/a2a-samples` at the revision above. Start either implementation on port `9999`:

```console
cd samples/python/agents/helloworld
uv run --no-project --with-requirements requirements.txt python __main__.py
```

```console
cd samples/go/agents/helloworld
go run .
```

Then, from this repository:

```console
uvx a2a-proof run examples/official-helloworld.yaml
```

## Routed Python agent

From the same `a2a-samples` checkout:

```console
cd samples/python/agents/multitenancy
A2A_PORT=18133 A2A_PUBLIC_URL=http://127.0.0.1:18133 \
  uv run --no-project --with a2a-sdk==1.1.0 --with uvicorn --with starlette \
  --with sse-starlette python a2a_server.py
```

```console
uvx a2a-proof run examples/official-multitenancy-reverse.yaml
```

## Official Rust transports

From the pinned `a2aproject/a2a-rs` checkout, start its Hello World server:

```console
cargo run --locked --bin helloworld-server --package examples
```

Run the same lifecycle contract over each advertised binding:

```console
uvx a2a-proof run examples/official-rust-lifecycle.yaml --transport JSONRPC
uvx a2a-proof run examples/official-rust-lifecycle.yaml --transport HTTP+JSON
uvx a2a-proof run examples/official-rust-lifecycle.yaml --transport GRPC
```

The example explicitly trusts the server's second loopback port for gRPC and disables TLS only for
that local endpoint.

## Independent multi-turn agent

Install Rust 1.94 and Protocol Buffers development headers, then start the pinned Turul example:

```console
cargo run --locked -p interrupting-agent
```

```console
uvx a2a-proof run examples/turul-interrupting.yaml
```
