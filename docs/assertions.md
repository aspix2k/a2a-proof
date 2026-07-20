# Assertions

Every turn accepts an `expect` block. Failed, rejected, and canceled tasks fail unless their state
is explicitly expected.

## Text and timing

```yaml
expect:
  state: completed
  max_seconds: 10
  max_first_event_seconds: 2
  text:
    contains: Paris
    not_contains: error
    matches: "(?i)capital"
```

Text checks support `contains`, `not_contains`, `equals`, and bounded Python regular expressions in
`matches`. They are case-sensitive unless `case_sensitive: false` is set.

`max_seconds` bounds the complete turn. `max_first_event_seconds` bounds the time until the first
A2A response event. Scenario-level `latency.p50_seconds` and `latency.p95_seconds` apply across
completed trials using linear interpolation; execution errors are excluded.

## State trajectory

```yaml
expect:
  states:
    contains_in_order: [submitted, working, completed]
```

`states.equals` checks the complete observed trajectory. `states.contains_in_order` checks a
subsequence and permits intermediate states. Consecutive duplicates are collapsed.

## Structured data

Structured assertions inspect A2A data parts from messages or artifacts. `path` is an
[RFC 6901 JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901); an empty path selects the complete
JSON value. Each assertion must match at least one part after optional `source`, `artifact_name`,
and `media_type` filters.

```yaml
expect:
  data:
    - source: artifact
      artifact_name: forecast
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
```

Use exactly one predicate per entry: `equals`, `exists`, `matches`, numeric `gt`/`gte`/`lt`/`lte`,
or `json_schema`. JSON equality does not treat booleans as numbers. Embedded schemas use JSON
Schema Draft 2020-12 and may contain local references; external references are rejected.

Signed AP2 mandate chains and receipts use a dedicated `expect.ap2` assertion rather than a generic
string check. See [AP2 contracts](ap2.md).

## Files

Response file checks match metadata from raw and URL parts. Remote URLs remain passive and are
never included in reports.

```yaml
expect:
  files:
    source: artifact
    artifact_name: summary
    filename: summary.txt
    media_type: text/plain
    kind: url
    count: 1
```
