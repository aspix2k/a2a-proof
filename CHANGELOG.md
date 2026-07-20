# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## 0.10.0 - 2026-07-20

### Security

- Added signed AP2 payment and checkout receipt verification with an exact ES256 protected-header
  requirement, official AP2 success/error payload validation, and explicit local public P-256
  issuer keys. Remote key discovery, private JWK material, and token-supplied trust remain rejected.
- Bound every response receipt to a mandate assertion that passed full chain verification in the
  same turn. The receipt reference is recomputed from that exact closed-mandate JWT instead of
  trusting a caller-provided response field.
- Redacted selected receipt JWTs before reports and evidence are produced. Offline inspection reads
  bounded files or standard input, never accepts token values as arguments, and does not expose
  mismatched signed fields in diagnostics.

### Features

- Added `kind: receipt` AP2 contract assertions for payment and checkout receipts. Contracts can
  check issuer, success or error status, payment or order identifiers, and error codes while using
  `binds_to` to reference a named mandate assertion.
- Added `a2a-proof ap2 inspect-receipt` with concise terminal and stable JSON output. It can derive
  the expected reference from a mandate file or accept an already known reference hash.
- Extended the generated JSON Schema with receipt assertions, binding identifiers, path and
  response-part filters, and type-specific validation.

### Bug fixes

- Made official receipt tampering tests flip a decoded signature bit instead of a trailing
  base64url character whose unused padding bits could leave the signature unchanged.

### Documentation

- Expanded the focused AP2 guide with a complete mandate-to-receipt contract, trust boundary, CLI
  walkthrough, exact output, exit semantics, and copyable success-path example.
- Updated the README feature summary and documentation links without expanding the onboarding
  path.

### Maintenance

- Added synthetic payment-receipt fixtures signed by discarded demo keys and official AP2 v0.2.0
  integration tests for payment and checkout receipts, success and error payloads, signature
  tampering, and mandate-reference binding.
- Extended unit, schema, CLI, reporting, configuration, redaction, and negative-path coverage while
  retaining complete statement and branch coverage.
- Established a clean mutation baseline after removing the generated `mutants/` tree: 3,617 of
  3,697 mutants are killed (97.84%), with no uncovered, suspicious, skipped, timed-out, or crashed
  mutations. Surviving receipt mutants were triaged for observable contract and security impact.

## 0.9.0 - 2026-07-20

### Security

- Added offline AP2 mandate inspection without accepting tokens as command-line values. Tokens are
  read from a bounded file or standard input, never echoed, and omitted from success and failure
  output.
- Rendered verified mandate fields as literal, bounded text so terminal control characters and
  Rich markup cannot alter inspector output. Trust roots remain local public P-256 JWKs; remote key
  discovery and private JWK material remain rejected.
- Split invalid mandates from setup failures: cryptographic, delegation, audience, nonce, validity,
  schema, constraint, and checkout-binding failures return exit code `1`; input, key, and missing
  SDK errors return `2`.

### Features

- Added `a2a-proof ap2 inspect` with automatic payment or checkout type detection, optional
  type-specific expected bindings, concise terminal output, and stable JSON output.
- Added verified summaries for payment payee, amount, transaction, and instrument type, plus
  checkout merchant, status, currency, item count, and signed checkout hash. Raw mandate tokens and
  payment instrument identifiers are not reported.

### Documentation

- Added a copyable inspector walkthrough with the exact terminal result, stdin usage, JSON mode,
  and exit-code contract while keeping AP2 reference material outside the README.

### Maintenance

- Added a synthetic signed AP2 payment fixture whose private demo keys were discarded. CI verifies
  the documented command against the pinned official AP2 v0.2.0 SDK.
- Extended official integration coverage to inspect real payment and checkout chains and reject a
  tampered signature through the same verifier used by response contracts.
- Strengthened AP2 and cross-module contract tests. The mutation result originally recorded for
  this release was incremental rather than a comparable full baseline; v0.10.0 establishes the
  clean-run procedure.

## 0.8.0 - 2026-07-19

### Security

- Added cryptographic verification for AP2 v0.2.0 dSD-JWT mandate chains, including every
  delegation signature and binding, terminal audience and nonce, validity windows, typed payloads,
  and open-to-closed constraints.
- Required an explicit local public P-256 trust root. Key paths are confined to the contract
  directory, private JWK material is rejected, reads are bounded, and remote key discovery is never
  attempted.
- Redacted mandate tokens selected by AP2 assertions before JSON, JUnit, terminal, or evidence
  output while preserving unrelated response data. Verification failures do not echo untrusted
  token payloads.

### Features

- Added `expect.ap2` assertions for signed payment and checkout mandate chains. Official AP2 data
  keys are inferred from the mandate type, with optional response-part filters and custom JSON
  Pointer paths.
- Added payment checks for expected transaction IDs and open-checkout references, plus checkout
  checks for expected hashes and automatic binding of `checkout_hash` to the signed
  `checkout_jwt`.

### Documentation

- Added a focused AP2 guide covering the pinned official SDK install, trust-root setup, payment and
  checkout examples, verification semantics, redaction, and current protocol boundary.

### Maintenance

- Added a pinned CI integration test against the official AP2 v0.2.0 source commit. It creates
  valid payment and checkout chains, verifies both, and rejects a tampered signature.
- Confirmed real interoperability between the current A2A client and an AP2-style A2A 0.3.24
  JSON-RPC server, including legacy extension-header translation and signed mandate transport.
- Extended the generated configuration schema and deterministic mutation target for AP2 while
  retaining complete statement and branch coverage.

## 0.7.0 - 2026-07-19

### Features

- Added black-box cancellation contracts. A multi-turn scenario can request an initial
  non-terminal task with `return_immediately: true`, invoke the protocol-level `cancel` operation,
  and assert the returned task state, content, timing, files, data, and invariants.
- Added task persistence contracts through `action: get_task`, with optional bounded
  `history_length`. Task IDs and contexts flow between message, cancel, and retrieval turns without
  exposing transport-specific methods in the configuration.
- Added `a2a-proof diff --against URL` for running one selected contract against baseline and
  candidate deployments. Terminal and JSON output classify Agent Card and scenario outcomes as
  regressions, improvements, changed, or unchanged; the candidate contract result controls the
  exit status.
- Kept lifecycle behavior portable across JSON-RPC, HTTP+JSON, and gRPC. Streaming agents use a
  dedicated non-streaming client for the A2A `return_immediately` request while normal scenarios
  retain streaming state trajectories and first-event timing.

### Security

- Applied the existing URL credential rejection, Agent Card origin checks, redirect policy,
  request headers, extensions, response bounds, and timeouts to lifecycle and candidate diff runs.
- Kept differential comparison at the contract-result layer instead of fetching or comparing
  untrusted remote resources; response file URLs remain passive and omitted from reports.

### Documentation

- Added a complete cancellation-and-persistence contract and documented lifecycle ordering,
  missing-task behavior, history limits, diff classifications, and diff exit codes.
- Kept the README focused on first use and moved contract, assertion, lifecycle, and operational
  reference material into short topic guides.

### Maintenance

- Added real lifecycle exchanges for every supported transport and a real CLI deployment diff;
  272 tests retain complete statement and branch coverage across the expanded surface.
- Added lifecycle configuration constraints to the generated JSON Schema and differential result
  classification to mutation testing, with 98.8% of 2,362 mutants killed.

## 0.6.0 - 2026-07-19

### Security

- Kept invariant secrets out of YAML and reports by accepting environment variable names,
  resolving their values only in memory, and redacting values case-insensitively before evidence
  truncation or serialization.
- Removed response text, structured data, and file metadata from every report when a global
  invariant detects a leak in that turn.
- Redacted resolved request-header values and their environment substitutions throughout evidence
  records, and continued to omit remote file URLs. Evidence output is limited to 100 failed trials
  and bounded response previews.
- Wrote evidence through a private temporary directory with flushed files and an atomic final
  rename; existing destinations are never overwritten.

### Bug fixes

- Made JUnit agree with `pass_rate`: failed trials within the accepted budget are now reported as
  skipped instead of failing an otherwise successful CI report.
- Made an explicitly empty environment mapping remain empty instead of silently falling back to
  the process environment during configuration loading.

### Features

- Added suite-wide response invariants for forbidden text and environment-backed secret values.
  They run on every response turn and identify the violated rule without printing the secret.
- Added `--evidence DIR` for atomic, machine-readable run bundles. Each bundle records the contract
  and Agent Card hashes, run metadata, failed-trial summaries, and a normalized response trace in
  JSONL. Agent Card and aggregate latency failures are included even when no trial fails.
- Added scenario-level `p50_seconds` and `p95_seconds` latency contracts across completed trials,
  with aggregate results in terminal, JSON, and JUnit reports.
- Added opt-in `--jobs` concurrency for repeated trials. Concurrency is bounded at 32, remains
  sequential by default, and preserves deterministic result ordering.

### Documentation

- Documented global invariants, evidence contents and limits, percentile semantics, and safe
  parallel trial execution.

### Maintenance

- Added evidence handling to mutation testing and extended the generated configuration schema for
  invariants and aggregate latency contracts.

## 0.5.0 - 2026-07-19

### Features

- Added end-to-end file contracts for A2A 1.0 `raw` and `url` parts. Scenarios can send local
  files, assert response file metadata and counts, and preserve safe metadata in JSON reports.
- Added Agent Card preflight assertions for skill IDs, streaming and notification capabilities,
  extended cards, and default input and output modes. A failed preflight stops before the first
  scenario message.
- Added exact and ordered-subsequence assertions for observed task-state trajectories, with
  consecutive duplicate states collapsed.
- Added top-level `defaults` for `trials` and `pass_rate`, while preserving explicit scenario
  values.

### Security

- Confined input files to the contract directory after symlink resolution and rejected missing,
  non-regular, oversized, and cross-directory inputs during configuration checks.
- Bounded file input to 20 files, 10 MB per file, and 20 MB per turn; bounded response file counts,
  inline bytes, metadata, and URL length.
- Kept remote file parts passive: URLs are neither fetched nor included in reports, which avoids
  following untrusted locations or persisting signed query parameters.

### Maintenance

- Extended real JSON-RPC coverage to Agent Card, file upload, file response, and state-trajectory
  contracts while retaining complete statement and branch coverage.
- Added file handling to the mutation target and raised the full deterministic-core mutation score
  above 99%.
- Updated the generated JSON Schema for file shorthand, card assertions, state-sequence
  exclusivity, and scenario defaults.

### Documentation

- Added complete configuration examples and precise semantics for Agent Card checks, file parts,
  state trajectories, defaults, path confinement, and file limits.

## 0.4.0 - 2026-07-19

### Features

- Made releases installable from PyPI with `uvx a2a-proof` through tokenless Trusted Publishing.
- Published a machine-readable configuration schema and made `init` attach it for editor
  completion and inline validation.
- Added structured-data assertions for presence, bounded regular expressions, numeric ranges, and
  inline JSON Schema Draft 2020-12 documents.
- Added `--scenario` filtering for focused local and CI runs. Multiple exact names can be selected
  without changing configuration order.
- Added `max_first_event_seconds` for bounding the first A2A response event independently from
  total turn duration.

### Security

- Rejected external references in embedded JSON Schemas and bounded each schema to 100 KB and 50
  levels.
- Kept structured-data regular expressions under the same 100 ms evaluation limit as text
  assertions.

### Maintenance

- Made CI reject a committed configuration schema that has drifted from the Pydantic models.

### Documentation

- Documented editor integration, every structured-data assertion, focused scenario execution, and
  first-event latency semantics.

## 0.3.0 - 2026-07-19

### Features

- Added outgoing A2A data parts to single-turn and multi-turn scenarios, including data-only
  messages and multiple JSON values per turn.
- Added validated A2A extension activation for JSON-RPC, HTTP+JSON, and gRPC. Runs fail before the
  first agent request when configured and advertised capabilities do not match.
- Made `init` enable required Agent Card extensions and validate the complete generated file before
  writing it.

### Security

- Limited outgoing structured input to 100 parts and 1 MB per turn after environment expansion.
- Rejected malformed, duplicate, oversized, and excessive extension configuration.

### Maintenance

- Preserved header-based `A2A-Extensions` configuration while normalizing it with the dedicated
  `agent.extensions` field.

### Documentation

- Documented structured request data, extension negotiation, and the current A2A version boundary
  with the official AP2 samples.

## 0.2.0 - 2026-07-19

### Features

- Added exact JSON assertions for A2A data parts, with message/artifact filters and RFC 6901 JSON
  Pointer paths.
- Preserved structured data values and their source, media type, and artifact metadata in JSON
  reports.
- Included a reproducible contract for the official A2A Hello World sample agent.

### Bug fixes

- Aligned every line of multiline agent responses in verbose terminal output.

### Security

- Rejected non-finite structured values and enforced per-turn limits for structured and inline raw
  data.

### Maintenance

- Made versioned changelog sections the source for GitHub release notes. Release jobs fail when
  notes are missing.

## 0.1.1 - 2026-07-19

### Security

- Added SHA-256 checksums for release artifacts.
- Enabled GitHub dependency alerts and automated security updates.

### Maintenance

- Added CI on Linux, macOS, and Windows.
- Added issue forms and a pull request template for focused contributions.

### Documentation

- Clarified the boundary between behavioral contract testing, the A2A TCK, and the A2A Inspector.
- Added successful and failed command output to the README.

## 0.1.0 - 2026-07-19

### Features

- Added JSON-RPC, HTTP+JSON, and gRPC contract execution.
- Added Agent Card discovery and generated smoke scenarios.
- Added single-turn, multi-turn, and repeated probabilistic scenarios.
- Added terminal, JSON, and JUnit reports.

### Security

- Disabled redirects and rejected cross-origin Agent Card interfaces unless explicitly allowed.
- Bounded streamed events, response text, and configuration size.
