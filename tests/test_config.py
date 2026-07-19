from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest

import a2a_proof.config as config_module
import a2a_proof.models as models_module
from a2a_proof.config import ConfigError, _header_environment_values, load_config, write_config
from a2a_proof.models import (
    _split_extension_parameter,
    _validate_extension_uris,
    _validate_json_schema_limits,
)


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class _UnreadablePath:
    def open(self, mode: str) -> None:
        raise OSError("denied")

    def __str__(self) -> str:
        return "proof.yaml"


class _BoundedStream(BytesIO):
    def read(self, size: int | None = -1, /) -> bytes:
        assert size == config_module.MAX_CONFIG_BYTES + 1
        return super().read(size)


class _BoundedPath:
    parent = Path(".")

    def open(self, mode: str) -> _BoundedStream:
        assert mode == "rb"
        return _BoundedStream(
            b"version: 1\nagent: {url: https://example.com}\n"
            b"scenarios: [{name: smoke, message: Hello}]\n"
        )

    def __str__(self) -> str:
        return "proof.yaml"


def test_loads_and_expands_environment_after_yaml_parsing(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent:
  url: https://example.com
  headers:
    Authorization: Bearer ${TOKEN}
scenarios:
  - name: smoke
    message: Hello
""",
    )

    config = load_config(path, {"TOKEN": "value: still a string"})

    assert config.agent.headers == {"Authorization": "Bearer value: still a string"}
    assert config.redaction_values == ("value: still a string",)


def test_collects_only_available_string_header_environment_values() -> None:
    assert _header_environment_values({"agent": "invalid"}, {}) == []
    assert _header_environment_values({"agent": {}}, {}) == []
    assert _header_environment_values(
        {
            "agent": {
                "headers": {
                    "Authorization": "Bearer ${TOKEN} ${MISSING}",
                    "Retries": 3,
                }
            }
        },
        {"TOKEN": "secret"},
    ) == ["secret"]


def test_reports_missing_environment_variable(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent:
  url: https://example.com
  headers:
    Authorization: ${TOKEN}
scenarios:
  - name: smoke
    message: Hello
""",
    )

    with pytest.raises(ConfigError) as raised:
        load_config(path, {})
    assert str(raised.value) == "missing environment variable(s): TOKEN"


def test_reports_multiple_missing_environment_variables_in_name_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent:
  url: https://example.com
  headers:
    Authorization: ${Z_TOKEN} ${A_TOKEN}
scenarios:
  - name: smoke
    message: Hello
""",
    )

    with pytest.raises(ConfigError) as raised:
        load_config(path, {})
    assert str(raised.value) == "missing environment variable(s): A_TOKEN, Z_TOKEN"


def test_loads_global_invariants_and_binds_contract_digest(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
invariants:
  text:
    not_contains: system prompt
    not_contains_env: API_TOKEN
    case_sensitive: false
scenarios:
  - name: smoke
    message: Hello
""",
    )

    config = load_config(path, {"API_TOKEN": "secret"})

    assert config.invariants is not None
    assert config.invariants.text.not_contains == ["system prompt"]
    assert config.invariants.text.not_contains_env == ["API_TOKEN"]
    assert not config.invariants.text.case_sensitive
    assert config.contract_sha256 == sha256(path.read_bytes()).hexdigest()


def test_loads_ap2_assertion_and_validates_trusted_root(tmp_path: Path) -> None:
    _write(
        tmp_path / "root.jwk",
        '{"kty":"EC","crv":"P-256","x":"eA","y":"eQ"}',
    )
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: payment
    message: Pay
    expect:
      ap2:
        type: payment
        trusted_root_jwk: root.jwk
        audience: merchant
        nonce: ${PAYMENT_NONCE}
        transaction_id: tx-1
""",
    )

    config = load_config(path, {"PAYMENT_NONCE": "nonce-1"})
    expectation = config.scenarios[0].expect.ap2[0]

    assert expectation.nonce == "nonce-1"
    assert expectation.resolved_path == "/ap2.mandates.PaymentMandateSdJwt"


def test_reports_invalid_ap2_trusted_root_as_configuration_error(tmp_path: Path) -> None:
    _write(
        tmp_path / "root.jwk",
        '{"kty":"EC","crv":"P-256","x":"eA","y":"eQ","d":"private"}',
    )
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: payment
    message: Pay
    expect:
      ap2:
        type: payment
        trusted_root_jwk: root.jwk
        audience: merchant
        nonce: nonce-1
""",
    )

    with pytest.raises(ConfigError, match="must contain a public key only"):
        load_config(path)


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "missing environment variable(s): A_TOKEN, Z_TOKEN"),
        (
            {"A_TOKEN": "", "Z_TOKEN": ""},
            "environment variable(s) must not be empty: A_TOKEN, Z_TOKEN",
        ),
    ],
)
def test_rejects_unavailable_invariant_environment_values(
    tmp_path: Path,
    environment: dict[str, str],
    message: str,
) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
invariants:
  text:
    not_contains_env: [Z_TOKEN, A_TOKEN]
scenarios:
  - name: smoke
    message: Hello
""",
    )

    with pytest.raises(ConfigError) as raised:
        load_config(path, environment)
    assert str(raised.value) == message


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("text: {}", "text invariant must define not_contains or not_contains_env"),
        ("text: {not_contains_env: invalid-name}", "String should match pattern"),
    ],
)
def test_rejects_invalid_global_invariants(tmp_path: Path, text: str, message: str) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        f"""
version: 1
agent: {{url: https://example.com}}
invariants:
  {text}
scenarios:
  - name: smoke
    message: Hello
""",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path, {})


@pytest.mark.parametrize(
    ("latency", "message"),
    [
        ("{}", "latency must define p50_seconds or p95_seconds"),
        ("{p95_seconds: null}", "latency percentile cannot be null"),
        ("{p50_seconds: 0}", "Input should be greater than 0"),
    ],
)
def test_rejects_invalid_latency_contract(
    tmp_path: Path,
    latency: str,
    message: str,
) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        f"""
version: 1
agent: {{url: https://example.com}}
scenarios:
  - name: smoke
    message: Hello
    latency: {latency}
""",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path, {})


def test_expands_environment_inside_sequences(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: smoke
    message: ${MESSAGE}
""",
    )

    config = load_config(path, {"MESSAGE": "Hello"})

    assert config.scenarios[0].message == "Hello"


def test_loads_structured_input_and_extensions(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent:
  url: https://example.com
  extensions:
    - https://example.com/extensions/orders/v1
scenarios:
  - name: create order
    message: Create this order
    data:
      order_id: ${ORDER_ID}
      items: 2
  - name: continue with data
    turns:
      - data:
          approved: true
      - message: Confirm
        data:
          receipt: requested
""",
    )

    config = load_config(path, {"ORDER_ID": "order-42"})

    assert config.agent.extensions == ["https://example.com/extensions/orders/v1"]
    assert config.scenarios[0].data == [{"order_id": "order-42", "items": 2}]
    assert config.scenarios[0].resolved_turns()[0].data == [{"order_id": "order-42", "items": 2}]
    assert config.scenarios[1].resolved_turns()[0].message is None
    assert config.scenarios[1].resolved_turns()[0].data == [{"approved": True}]
    assert config.scenarios[1].resolved_turns()[1].data == [{"receipt": "requested"}]


def test_loads_multiple_structured_input_parts(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: parts
    data:
      - {kind: order, id: 42}
      - [one, two]
      - true
      - null
""",
    )

    assert load_config(path).scenarios[0].data == [
        {"kind": "order", "id": 42},
        ["one", "two"],
        True,
        None,
    ]


def test_resolves_single_turn_structured_input() -> None:
    scenario = models_module.Scenario.model_validate(
        {
            "name": "structured",
            "message": "Create order",
            "data": {"order_id": "order-42"},
        }
    )

    assert scenario.resolved_turns()[0].model_dump() == {
        "message": "Create order",
        "data": [{"order_id": "order-42"}],
        "files": [],
        "action": None,
        "return_immediately": False,
        "history_length": None,
        "expect": {
            "state": None,
            "text": None,
            "states": None,
            "data": [],
            "files": [],
            "ap2": [],
            "max_seconds": None,
            "max_first_event_seconds": None,
        },
    }


def test_validates_task_action_turns() -> None:
    scenario = models_module.Scenario.model_validate(
        {
            "name": "cancel",
            "turns": [
                {"message": "Start", "return_immediately": True},
                {"action": "cancel", "expect": {"state": "canceled"}},
                {"action": "get_task", "history_length": 5},
            ],
        }
    )

    assert [turn.action for turn in scenario.resolved_turns()] == [None, "cancel", "get_task"]
    assert scenario.resolved_turns()[2].history_length == 5

    with pytest.raises(ValueError, match="first turn cannot be a task action"):
        models_module.Scenario(name="invalid", turns=[{"action": "cancel"}])
    with pytest.raises(ValueError, match="cannot combine an action"):
        models_module.Turn(message="Start", action="cancel")
    with pytest.raises(ValueError, match="return_immediately can only"):
        models_module.Turn(action="cancel", return_immediately=True)
    with pytest.raises(ValueError, match="history_length can only"):
        models_module.Turn(message="Start", history_length=1)


def test_loads_and_validates_relative_file_inputs(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "report.pdf").write_bytes(b"pdf")
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: summarize
    message: Summarize this
    files:
      - fixtures/report.pdf
      - path: fixtures/report.pdf
        media_type: application/x-test
""",
    )

    config = load_config(path)

    assert config.contract_dir == tmp_path.resolve()
    assert [file.path for file in config.scenarios[0].files] == [
        "fixtures/report.pdf",
        "fixtures/report.pdf",
    ]
    assert config.scenarios[0].files[1].media_type == "application/x-test"


def test_file_input_paths_must_be_portably_relative() -> None:
    assert models_module.FileInput.model_validate({"path": "fixtures/report.pdf"}).path == (
        "fixtures/report.pdf"
    )
    with pytest.raises(ValueError, match="relative to the contract file"):
        models_module.FileInput(path="/tmp/report.pdf")
    with pytest.raises(ValueError, match="relative to the contract file"):
        models_module.FileInput(path=r"C:\\report.pdf")
    with pytest.raises(ValueError, match="null byte"):
        models_module.FileInput(path="bad\0name")


def test_load_reports_invalid_file_references(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
scenarios: [{name: summarize, files: [missing.pdf]}]
""",
    )

    with pytest.raises(ConfigError, match=r"cannot access input file 'missing\.pdf'"):
        load_config(path)


def test_applies_scenario_defaults_only_when_omitted() -> None:
    config = models_module.ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "defaults": {"trials": 3, "pass_rate": 0.5},
            "scenarios": [
                {"name": "defaults", "message": "Hello"},
                {
                    "name": "override",
                    "message": "Hello",
                    "trials": 1,
                    "pass_rate": 1,
                },
            ],
        }
    )

    first, second = config.resolved_scenarios()
    assert (first.trials, first.pass_rate) == (3, 0.5)
    assert (second.trials, second.pass_rate) == (1, 1)
    assert config.scenarios[0].trials == 1


def test_validates_card_and_state_sequence_assertions() -> None:
    with pytest.raises(ValueError, match="card must define at least one assertion"):
        models_module.AgentCardExpectation()
    with pytest.raises(ValueError, match="capabilities must define at least one assertion"):
        models_module.AgentCapabilitiesExpectation()
    with pytest.raises(ValueError, match="cannot be null"):
        models_module.AgentCapabilitiesExpectation(streaming=None)
    with pytest.raises(ValueError, match="exactly one"):
        models_module.StateSequenceExpectation()
    with pytest.raises(ValueError, match="exactly one"):
        models_module.StateSequenceExpectation(equals=["working"], contains_in_order=["working"])
    with pytest.raises(ValueError, match="cannot be null"):
        models_module.StateSequenceExpectation(equals=None)


def test_rejects_artifact_name_for_message_file_expectation() -> None:
    with pytest.raises(ValueError, match="artifact_name cannot be used"):
        models_module.FileExpectation(source="message", artifact_name="report")


def test_checks_structured_input_size_after_environment_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models_module, "MAX_INPUT_DATA_BYTES", 10)
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
scenarios: [{name: data, data: {value: "${VALUE}"}}]
""",
    )

    with pytest.raises(ConfigError, match="input data exceeds 10 bytes"):
        load_config(path, {"VALUE": "expanded value"})


def test_structured_input_size_limit_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_module, "MAX_INPUT_DATA_BYTES", 4)

    assert models_module.Scenario(name="boundary", data={}).data == [{}]
    with pytest.raises(ValueError, match="input data exceeds 4 bytes"):
        models_module.Scenario(name="over limit", data={"x": 0})


def test_validates_structured_data_assertion_shape() -> None:
    with pytest.raises(ValueError, match="at least one assertion"):
        models_module.DataExpectation()
    with pytest.raises(ValueError, match="one assertion type"):
        models_module.DataExpectation(equals=1, gt=0)
    with pytest.raises(ValueError, match="cannot be null"):
        models_module.DataExpectation(exists=None)
    with pytest.raises(ValueError, match="cannot be null"):
        models_module.DataExpectation(matches=None)
    with pytest.raises(ValueError, match="cannot be null"):
        models_module.DataExpectation(json_schema=None)
    with pytest.raises(ValueError, match="non-empty path"):
        models_module.DataExpectation(exists=False)

    assert models_module.DataExpectation(equals=None).model_fields_set == {"equals"}


@pytest.mark.parametrize("value", [True, "1"])
def test_rejects_non_numeric_comparison_values(value: object) -> None:
    with pytest.raises(ValueError, match="numeric comparisons require a number"):
        models_module.DataExpectation.model_validate({"gt": value})


def test_validates_data_regular_expression() -> None:
    with pytest.raises(ValueError, match="invalid regular expression"):
        models_module.DataExpectation(matches="[")


def test_validates_embedded_json_schema_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {"type": "object"}
    models_module.DataExpectation(json_schema=schema)

    with pytest.raises(ValueError, match="invalid JSON Schema"):
        models_module.DataExpectation(json_schema={"type": "not-a-type"})
    with pytest.raises(ValueError, match="references must be local") as remote_reference:
        _validate_json_schema_limits({"$ref": "https://example.com/schema.json"})
    assert str(remote_reference.value) == "json_schema references must be local"
    with pytest.raises(ValueError, match="references must be local"):
        _validate_json_schema_limits({"$dynamicRef": "https://example.com/schema.json#anchor"})

    size = len(b'{"type":"object"}')
    monkeypatch.setattr(models_module, "MAX_JSON_SCHEMA_BYTES", size)
    models_module.DataExpectation(json_schema=schema)
    monkeypatch.setattr(models_module, "MAX_JSON_SCHEMA_BYTES", size - 1)
    with pytest.raises(ValueError, match=f"json_schema exceeds {size - 1} bytes"):
        models_module.DataExpectation(json_schema=schema)


def test_rejects_excessively_deep_embedded_json_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models_module, "MAX_JSON_SCHEMA_DEPTH", 2)

    _validate_json_schema_limits({"object": {"value": 1}})
    _validate_json_schema_limits({"array": [1]})
    with pytest.raises(ValueError, match="json_schema exceeds 2 levels"):
        _validate_json_schema_limits({"array": [[1]]})


def test_parses_legacy_extension_parameter() -> None:
    assert _split_extension_parameter("https://example.com/one, https://example.com/two") == [
        "https://example.com/one",
        "https://example.com/two",
    ]

    with pytest.raises(ValueError, match="comma-separated extension URIs") as raised:
        _split_extension_parameter("https://example.com/one,,https://example.com/two")
    assert str(raised.value) == "A2A-Extensions must contain comma-separated extension URIs"


def test_validates_extension_limits_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = [f"https://example.com/{index}" for index in range(models_module.MAX_EXTENSIONS)]
    _validate_extension_uris(valid)

    with pytest.raises(ValueError, match="at most 20 extension URIs") as excessive:
        _validate_extension_uris([*valid, "https://example.com/excessive"])
    assert str(excessive.value) == "configure at most 20 extension URIs"

    with pytest.raises(ValueError, match="extension URIs must be unique") as duplicate:
        _validate_extension_uris(["https://example.com/duplicate"] * 2)
    assert str(duplicate.value) == "extension URIs must be unique"

    uri = "https://example.com/extension"
    monkeypatch.setattr(models_module, "MAX_EXTENSION_PARAMETER_CHARS", len(uri))
    _validate_extension_uris([uri])

    monkeypatch.setattr(models_module, "MAX_EXTENSION_PARAMETER_CHARS", len(uri) - 1)
    with pytest.raises(ValueError, match="A2A-Extensions exceeds") as oversized:
        _validate_extension_uris([uri])
    assert str(oversized.value) == f"A2A-Extensions exceeds {len(uri) - 1} characters"


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/with space",
        "https://example.com/with,comma",
        "https://example.com/non-ascii/é",
        "not-a-uri",
    ],
)
def test_reports_invalid_extension_uri_exactly(uri: str) -> None:
    with pytest.raises(ValueError, match="invalid extension URI") as raised:
        _validate_extension_uris([uri])
    assert str(raised.value) == f"invalid extension URI: {uri!r}"


def test_limits_combined_extension_configuration() -> None:
    declared = [f"https://example.com/declared/{index}" for index in range(11)]
    legacy = ",".join(f"https://example.com/legacy/{index}" for index in range(10))

    with pytest.raises(ValueError, match="configure at most 20 extension URIs"):
        models_module.AgentConfig(
            url="https://example.com",
            extensions=declared,
            headers={"A2A-Extensions": legacy},
        )


def test_merges_declared_and_legacy_extension_configuration() -> None:
    config = models_module.AgentConfig(
        url="https://example.com",
        extensions=["https://example.com/extensions/one"],
        headers={
            "A2A-Extensions": (
                "https://example.com/extensions/one, https://example.com/extensions/two"
            )
        },
    )

    assert config.requested_extensions() == [
        "https://example.com/extensions/one",
        "https://example.com/extensions/two",
    ]


def test_limits_extension_parameter_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_module, "MAX_EXTENSION_PARAMETER_CHARS", 10)

    with pytest.raises(ValueError, match="A2A-Extensions exceeds 10 characters"):
        models_module.AgentConfig(
            url="https://example.com",
            extensions=["https://example.com/extension"],
        )


def test_loads_single_structured_data_expectation(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "proof.yaml",
        """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: forecast
    message: Weather in Paris?
    expect:
      data:
        source: artifact
        artifact_name: forecast
        media_type: application/json
        path: /city
        equals: Paris
""",
    )

    expectation = load_config(path).scenarios[0].expect.data[0]

    assert expectation.model_dump(exclude_none=True) == {
        "equals": "Paris",
        "path": "/city",
        "source": "artifact",
        "artifact_name": "forecast",
        "media_type": "application/json",
    }


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("[]", "root must be a mapping"),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - {name: duplicate, message: one}
  - {name: duplicate, message: two}
""",
            "scenario names must be unique",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: invalid
    message: one
    turns: [{message: two}]
""",
            "exactly one of single-turn input or turns",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios: [{name: empty}]
""",
            "scenario must contain message, data, files, or turns",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: empty turn
    turns: [{expect: {state: completed}}]
""",
            "turn must contain message, data, or files",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: mixed shape
    data: {value: one}
    turns: [{message: two}]
""",
            "exactly one of single-turn input or turns",
        ),
        (
            """
version: 1
agent:
  url: https://example.com
  extensions: ["https://example.com/ext", "https://example.com/ext"]
scenarios: [{name: smoke, message: Hello}]
""",
            "extension URIs must be unique",
        ),
        (
            """
version: 1
agent:
  url: https://example.com
  extensions: ["not a URI"]
scenarios: [{name: smoke, message: Hello}]
""",
            "invalid extension URI",
        ),
        (
            """
version: 1
agent:
  url: https://example.com
  headers: {A2A-Extensions: "https://example.com/one,,https://example.com/two"}
scenarios: [{name: smoke, message: Hello}]
""",
            "comma-separated extension URIs",
        ),
        (
            """
version: 1
agent: {url: https://example.com, typo: true}
scenarios: [{name: smoke, message: Hello}]
""",
            "Extra inputs are not permitted",
        ),
        (
            """
version: 1
agent:
  url: https://example.com
  headers: {Bad Header: value}
scenarios: [{name: smoke, message: Hello}]
""",
            "invalid HTTP header name",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: invalid regex
    message: Hello
    expect:
      text: {matches: "["}
""",
            "invalid regular expression",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: invalid turns expectation
    turns: [{message: one}]
    expect: {state: completed}
""",
            "put expect on each turn",
        ),
        (
            """
version: 1
agent:
  url: https://example.com
  headers: {Authorization: "bad\\nvalue"}
scenarios: [{name: smoke, message: Hello}]
""",
            "contains a line break",
        ),
        (
            """
version: 1
agent: {url: https://user:password@example.com}
scenarios: [{name: smoke, message: Hello}]
""",
            "agent URL must not contain credentials",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: invalid pointer
    message: Hello
    expect: {data: {path: /bad~2escape, equals: value}}
""",
            "RFC 6901 JSON Pointer",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: invalid source
    message: Hello
    expect:
      data: {source: message, artifact_name: result, equals: null}
""",
            "artifact_name cannot be used with source: message",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: non-finite
    message: Hello
    expect: {data: {equals: .nan}}
""",
            "Input should be a finite number",
        ),
    ],
)
def test_rejects_invalid_configuration(tmp_path: Path, content: str, expected: str) -> None:
    path = _write(tmp_path / "proof.yaml", content)

    with pytest.raises(ConfigError, match=expected):
        load_config(path)


def test_rejects_oversized_configuration(tmp_path: Path) -> None:
    path = _write(tmp_path / "proof.yaml", "x" * 1_000_001)

    with pytest.raises(ConfigError, match="exceeds"):
        load_config(path)


def test_bounds_configuration_read() -> None:
    assert load_config(cast(Path, _BoundedPath())).scenarios[0].name == "smoke"


def test_rejects_too_many_text_checks(tmp_path: Path) -> None:
    checks = ", ".join("x" for _ in range(101))
    path = _write(
        tmp_path / "proof.yaml",
        f"""
version: 1
agent: {{url: https://example.com}}
scenarios:
  - name: smoke
    message: Hello
    expect:
      text:
        contains: [{checks}]
""",
    )

    with pytest.raises(ConfigError, match="List should have at most 100 items"):
        load_config(path)


def test_rejects_too_many_structured_input_parts(tmp_path: Path) -> None:
    parts = ", ".join("null" for _ in range(101))
    path = _write(
        tmp_path / "proof.yaml",
        f"""
version: 1
agent: {{url: https://example.com}}
scenarios: [{{name: data, data: [{parts}]}}]
""",
    )

    with pytest.raises(ConfigError, match="List should have at most 100 items"):
        load_config(path)


def test_accepts_configuration_at_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = """
version: 1
agent: {url: https://example.com}
scenarios: [{name: smoke, message: Hello}]
"""
    path = _write(tmp_path / "proof.yaml", content)
    monkeypatch.setattr(config_module, "MAX_CONFIG_BYTES", path.stat().st_size)

    assert load_config(path).scenarios[0].name == "smoke"


def test_write_config_is_safe_by_default(tmp_path: Path) -> None:
    path = tmp_path / "proof.yaml"
    write_config(path, {"zeta": "Проверка", "alpha": 1})

    with pytest.raises(ConfigError, match="already exists"):
        write_config(path, {"version": 2})

    assert path.read_text(encoding="utf-8") == (
        f"# yaml-language-server: $schema={config_module.CONFIG_SCHEMA_URL}\n\n"
        "zeta: Проверка\n"
        "alpha: 1\n"
    )

    write_config(path, {"version": 2}, force=True)
    assert path.read_text(encoding="utf-8") == (
        f"# yaml-language-server: $schema={config_module.CONFIG_SCHEMA_URL}\n\nversion: 2\n"
    )


def test_write_config_requires_existing_parent(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="parent directory does not exist"):
        write_config(tmp_path / "missing" / "proof.yaml", {"version": 1})


def test_reports_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")

    path = _write(tmp_path / "invalid.yaml", "value: [")
    with pytest.raises(ConfigError, match="cannot parse"):
        load_config(path)

    invalid_utf = tmp_path / "invalid-utf.yaml"
    invalid_utf.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="cannot parse"):
        load_config(invalid_utf)


def test_reports_read_error_without_platform_noise() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(cast(Path, _UnreadablePath()))
    assert str(raised.value) == "cannot read proof.yaml: denied"


def test_reports_non_mapping_root_exactly(tmp_path: Path) -> None:
    path = _write(tmp_path / "proof.yaml", "[]")

    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert str(raised.value) == "configuration root must be a mapping"


def test_atomic_write_uses_destination_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_paths: list[Path] = []
    original_replace = config_module.os.replace

    def capture(source: Path, destination: Path) -> None:
        source_paths.append(Path(source))
        original_replace(source, destination)

    monkeypatch.setattr(config_module.os, "replace", capture)
    write_config(tmp_path / "proof.yaml", {"version": 1})

    assert source_paths[0].parent == tmp_path
    assert source_paths[0].name.startswith(".proof.yaml.")


def test_handles_temporary_file_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr(config_module.tempfile, "NamedTemporaryFile", fail)

    with pytest.raises(ConfigError) as raised:
        write_config(tmp_path / "proof.yaml", {"version": 1})
    assert str(raised.value) == f"cannot write {tmp_path / 'proof.yaml'}: denied"


def test_removes_temporary_file_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(source: Path, destination: Path) -> None:
        raise OSError("denied")

    monkeypatch.setattr(config_module.os, "replace", fail)

    path = tmp_path / "proof.yaml"
    with pytest.raises(ConfigError) as raised:
        write_config(path, {"version": 1})
    assert str(raised.value) == f"cannot write {path}: denied"
    assert list(tmp_path.iterdir()) == []


def test_tolerates_missing_temporary_file_during_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def remove_then_fail(source: Path, destination: Path) -> None:
        Path(source).unlink()
        raise OSError("denied")

    monkeypatch.setattr(config_module.os, "replace", remove_then_fail)

    with pytest.raises(ConfigError, match="cannot write"):
        write_config(tmp_path / "proof.yaml", {"version": 1})
    assert list(tmp_path.iterdir()) == []
