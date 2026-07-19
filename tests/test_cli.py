from __future__ import annotations

import json
from pathlib import Path

from a2a.client.errors import AgentCardResolutionError
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill
from click.testing import CliRunner

import a2a_proof.cli as cli_module
from a2a_proof.cli import main
from a2a_proof.models import ScenarioResult, SuiteResult, TrialResult

VALID_CONFIG = """
version: 1
agent: {url: https://example.com}
scenarios: [{name: smoke, message: Hello}]
"""


def test_check_validates_configuration(tmp_path: Path) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    result = CliRunner().invoke(main, ["check", str(path)])

    assert result.exit_code == 0
    assert result.output == "Valid: 1 scenario.\n"


def test_check_returns_two_for_invalid_configuration(tmp_path: Path) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text("invalid", encoding="utf-8")

    result = CliRunner().invoke(main, ["check", str(path)])

    assert result.exit_code == 2
    assert "configuration root must be a mapping" in result.output


def test_init_writes_environment_reference_without_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    card = AgentCard(
        name="Agent",
        description="Test",
        version="1",
        supported_interfaces=[
            AgentInterface(
                url="https://example.com/a2a",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(),
        skills=[AgentSkill(id="weather", name="Weather", examples=["Weather in Moscow?"])],
    )

    async def discover(config):
        assert config.headers["Authorization"] == "Bearer secret"
        return card

    monkeypatch.setattr(cli_module, "discover_agent", discover)
    monkeypatch.setenv("A2A_AUTH", "Bearer secret")
    output = tmp_path / "proof.yaml"

    result = CliRunner().invoke(
        main,
        [
            "init",
            "https://example.com",
            "--header-env",
            "Authorization=A2A_AUTH",
            "--output",
            str(output),
        ],
    )

    content = output.read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert content.startswith("# yaml-language-server: $schema=https://")
    assert "${A2A_AUTH}" in content
    assert "Bearer secret" not in content
    assert "Weather in Moscow?" in content


def test_init_rejects_missing_header_environment_variable() -> None:
    result = CliRunner().invoke(
        main,
        ["init", "https://example.com", "--header-env", "Authorization=MISSING"],
    )

    assert result.exit_code == 2
    assert "is not set" in result.output


def test_init_rejects_malformed_header_environment_reference() -> None:
    result = CliRunner().invoke(
        main,
        ["init", "https://example.com", "--header-env", "Authorization"],
    )

    assert result.exit_code == 2
    assert "expected HEADER=ENV_VAR" in result.output


def test_init_reports_agent_card_connection_error_without_traceback(monkeypatch) -> None:
    async def fail(config):
        raise AgentCardResolutionError("agent is unreachable")

    monkeypatch.setattr(cli_module, "discover_agent", fail)

    result = CliRunner().invoke(main, ["init", "https://example.com"])

    assert result.exit_code == 2
    assert result.output == "Error: agent is unreachable\n"


def test_init_preserves_custom_card_path_and_refuses_overwrite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    card = AgentCard(
        name="Agent",
        supported_interfaces=[
            AgentInterface(
                url="https://example.com/a2a",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
    )

    async def discover(config):
        return card

    monkeypatch.setattr(cli_module, "discover_agent", discover)
    output = tmp_path / "proof.yaml"
    first = CliRunner().invoke(
        main,
        [
            "init",
            "https://example.com",
            "--card-path",
            "/agent-card.json",
            "--output",
            str(output),
        ],
    )
    second = CliRunner().invoke(
        main,
        ["init", "https://example.com", "--output", str(output)],
    )

    assert first.exit_code == 0
    assert "card_path: /agent-card.json" in output.read_text(encoding="utf-8")
    assert second.exit_code == 2
    assert "already exists" in second.output


def test_init_can_explicitly_allow_cross_origin_interfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def discover(config):
        assert config.allow_cross_origin_interfaces
        return AgentCard(name="Agent")

    monkeypatch.setattr(cli_module, "discover_agent", discover)
    output = tmp_path / "proof.yaml"

    result = CliRunner().invoke(
        main,
        [
            "init",
            "https://example.com",
            "--allow-cross-origin",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "allow_cross_origin_interfaces: true" in output.read_text(encoding="utf-8")


def test_init_enables_required_agent_card_extensions(tmp_path: Path, monkeypatch) -> None:
    async def discover(config):
        return AgentCard(
            name="Agent",
            capabilities=AgentCapabilities(
                extensions=[
                    AgentExtension(uri="https://example.com/optional"),
                    AgentExtension(uri="https://example.com/required", required=True),
                    AgentExtension(uri="https://example.com/required", required=True),
                ]
            ),
        )

    monkeypatch.setattr(cli_module, "discover_agent", discover)
    output = tmp_path / "proof.yaml"

    result = CliRunner().invoke(
        main,
        ["init", "https://example.com", "--output", str(output)],
    )

    assert result.exit_code == 0
    content = output.read_text(encoding="utf-8")
    assert content.count("https://example.com/required") == 1
    assert "https://example.com/optional" not in content


def test_init_refuses_invalid_required_agent_card_extension(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def discover(config):
        return AgentCard(
            name="Agent",
            capabilities=AgentCapabilities(
                extensions=[AgentExtension(uri="not-a-uri", required=True)]
            ),
        )

    monkeypatch.setattr(cli_module, "discover_agent", discover)
    output = tmp_path / "proof.yaml"

    result = CliRunner().invoke(
        main,
        ["init", "https://example.com", "--output", str(output)],
    )

    assert result.exit_code == 2
    assert "invalid extension URI" in result.output
    assert not output.exists()


def test_run_json_and_exit_status(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    suite = SuiteResult(
        passed=False,
        duration_ms=1,
        scenarios=[
            ScenarioResult(
                name="smoke",
                passed=False,
                passed_trials=0,
                required_trials=1,
                trials=[TrialResult(index=1, passed=False, duration_ms=1, error="failed")],
            )
        ],
    )

    async def run(config, *, max_parallel_trials):
        assert max_parallel_trials == 1
        return suite

    monkeypatch.setattr(cli_module, "run", run)

    result = CliRunner().invoke(main, ["run", str(path), "--format", "json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["passed"] is False


def test_run_writes_requested_evidence_bundle(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    suite = SuiteResult(passed=True, duration_ms=1, scenarios=[])
    evidence = tmp_path / "evidence"
    captured: list[tuple[Path, SuiteResult]] = []

    async def run(config, *, max_parallel_trials):
        assert max_parallel_trials == 3
        return suite

    def write(directory, config, result, *, max_parallel_trials):
        assert config.contract_sha256 is not None
        assert max_parallel_trials == 3
        captured.append((directory, result))

    monkeypatch.setattr(cli_module, "run", run)
    monkeypatch.setattr(cli_module, "write_evidence", write)

    result = CliRunner().invoke(
        main,
        ["run", str(path), "--evidence", str(evidence), "--jobs", "3"],
    )

    assert result.exit_code == 0
    assert captured == [(evidence, suite)]


def test_run_requires_json_for_file_output(tmp_path: Path) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    result = CliRunner().invoke(main, ["run", str(path), "--output", str(tmp_path / "out")])

    assert result.exit_code == 2
    assert "--output requires --format json or junit" in result.output


def test_run_writes_json_file_and_renders_terminal(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    suite = SuiteResult(passed=True, duration_ms=1, scenarios=[])

    async def run(config, *, max_parallel_trials):
        assert max_parallel_trials == 1
        return suite

    monkeypatch.setattr(cli_module, "run", run)
    output = tmp_path / "result.json"

    json_result = CliRunner().invoke(
        main,
        ["run", str(path), "--format", "json", "--output", str(output)],
    )
    terminal_result = CliRunner().invoke(main, ["run", str(path)])

    assert json_result.exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
    assert terminal_result.exit_code == 0
    assert "0 scenarios passed" in terminal_result.output


def test_run_writes_junit_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    async def run(config, *, max_parallel_trials):
        assert max_parallel_trials == 1
        return SuiteResult(passed=True, duration_ms=1, scenarios=[])

    monkeypatch.setattr(cli_module, "run", run)
    output = tmp_path / "result.xml"

    result = CliRunner().invoke(
        main,
        ["run", str(path), "--format", "junit", "--output", str(output)],
    )

    assert result.exit_code == 0
    assert output.read_text(encoding="utf-8").startswith("<?xml version='1.0'")


def test_run_selects_named_scenarios_in_configuration_order(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(
        """
version: 1
agent: {url: https://example.com}
scenarios:
  - {name: first, message: One}
  - {name: second, message: Two}
  - {name: third, message: Three}
""",
        encoding="utf-8",
    )
    selected: list[str] = []

    async def run(config, *, max_parallel_trials):
        assert max_parallel_trials == 1
        selected.extend(scenario.name for scenario in config.scenarios)
        return SuiteResult(passed=True, duration_ms=1, scenarios=[])

    monkeypatch.setattr(cli_module, "run", run)

    result = CliRunner().invoke(
        main,
        ["run", str(path), "--scenario", "third", "--scenario", "first"],
    )

    assert result.exit_code == 0
    assert selected == ["first", "third"]


def test_run_rejects_unknown_scenario_before_connecting(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    async def run(config, *, max_parallel_trials):
        assert max_parallel_trials == 1
        raise AssertionError("runner must not start")

    monkeypatch.setattr(cli_module, "run", run)

    result = CliRunner().invoke(main, ["run", str(path), "--scenario", "missing"])

    assert result.exit_code == 2
    assert result.output == "Error: unknown scenario: missing\n"


def test_run_reports_execution_and_output_errors(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    async def fail(config, *, max_parallel_trials):
        assert max_parallel_trials == 1
        raise RuntimeError("cannot connect")

    monkeypatch.setattr(cli_module, "run", fail)
    execution = CliRunner().invoke(main, ["run", str(path)])

    assert execution.exit_code == 2
    assert "cannot connect" in execution.output

    async def succeed(config, *, max_parallel_trials):
        assert max_parallel_trials == 1
        return SuiteResult(passed=True, duration_ms=1, scenarios=[])

    def deny_write(self, content, encoding):
        raise OSError("denied")

    monkeypatch.setattr(cli_module, "run", succeed)
    monkeypatch.setattr(Path, "write_text", deny_write)
    output = CliRunner().invoke(
        main,
        ["run", str(path), "--format", "json", "--output", str(tmp_path / "result.json")],
    )

    assert output.exit_code == 2
    assert "cannot write" in output.output


def test_run_reports_agent_connection_error_without_traceback(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    async def fail(config, *, max_parallel_trials):
        assert max_parallel_trials == 1
        raise AgentCardResolutionError("agent is unreachable")

    monkeypatch.setattr(cli_module, "run", fail)

    result = CliRunner().invoke(main, ["run", str(path)])

    assert result.exit_code == 2
    assert result.output == "Error: agent is unreachable\n"


def test_generates_bounded_unique_scenarios() -> None:
    skills = [AgentSkill(id="none")]
    skills.extend(
        AgentSkill(id=str(index), name="Same", examples=[f"example {index}"]) for index in range(21)
    )
    card = AgentCard(name="Agent", skills=skills)

    scenarios = cli_module._scenarios_from_card(card)

    assert len(scenarios) == 20
    assert scenarios[0]["name"] == "Same"
    assert scenarios[1]["name"] == "Same 2"


def test_generates_smoke_when_card_has_no_usable_examples() -> None:
    card = AgentCard(
        name="Agent",
        skills=[
            AgentSkill(id="blank", examples=["  "]),
            AgentSkill(id="huge", examples=["x" * 100_001]),
        ],
    )

    assert cli_module._scenarios_from_card(card) == [{"name": "smoke", "message": "Hello"}]
