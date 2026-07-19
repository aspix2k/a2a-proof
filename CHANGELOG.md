# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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
