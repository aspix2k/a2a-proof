from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import cast

import pytest

import a2a_proof.config as config_module
import a2a_proof.models as models_module
from a2a_proof.config import ConfigError, load_config, write_config
from a2a_proof.models import _split_extension_parameter, _validate_extension_uris


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
        "expect": {"state": None, "text": None, "data": [], "max_seconds": None},
    }


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

    assert expectation.model_dump() == {
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
            "scenario must contain message, data, or turns",
        ),
        (
            """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: empty turn
    turns: [{expect: {state: completed}}]
""",
            "turn must contain message or data",
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

    assert path.read_text(encoding="utf-8") == "zeta: Проверка\nalpha: 1\n"

    write_config(path, {"version": 2}, force=True)
    assert path.read_text(encoding="utf-8") == "version: 2\n"


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
