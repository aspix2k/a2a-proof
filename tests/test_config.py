from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import cast

import pytest

import a2a_proof.config as config_module
from a2a_proof.config import ConfigError, load_config, write_config


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
            "exactly one of message or turns",
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
