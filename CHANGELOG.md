# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## 0.1.1 - 2026-07-19

### Added

- Issue forms and a pull request template for focused contributions.
- SHA-256 checksums for release artifacts.
- CI runs on Linux, macOS, and Windows.

### Changed

- Clarify the boundary between behavioral contract testing, the A2A TCK, and the A2A Inspector.
- Show successful and failed command output in the README.

### Security

- Enable GitHub dependency alerts and automated security updates.

## 0.1.0 - 2026-07-19

### Added

- Agent Card discovery and generated smoke scenarios.
- JSON-RPC, HTTP+JSON, and gRPC contract execution.
- Single-turn, multi-turn, and repeated probabilistic scenarios.
- Terminal, JSON, and JUnit reports.

### Security

- Bound streamed events, response text, and configuration size.
- Disable redirects and reject cross-origin Agent Card interfaces unless explicitly allowed.
