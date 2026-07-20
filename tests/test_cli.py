from __future__ import annotations

import json
from pathlib import Path

import pytest
from a2a.client.errors import AgentCardResolutionError
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill
from click.testing import CliRunner

import a2a_proof.cli as cli_module
from a2a_proof.ap2 import AP2Inspection, AP2ReceiptInspection
from a2a_proof.cli import main
from a2a_proof.models import ProofConfig, ScenarioResult, SuiteResult, TrialResult

VALID_CONFIG = """
version: 1
agent: {url: https://example.com}
scenarios: [{name: smoke, message: Hello}]
"""


def _ap2_inspection() -> AP2Inspection:
    return AP2Inspection(
        type="payment",
        chain_length=2,
        audience="merchant",
        checks=("chain_signatures", "nonce"),
        details={
            "transaction_id": "checkout-hash",
            "payee": {"id": "shop-1", "name": "Shop"},
            "amount": {"minor_units": 1_000, "currency": "USD"},
            "payment_instrument_type": "card",
        },
    )


def _receipt_inspection() -> AP2ReceiptInspection:
    return AP2ReceiptInspection(
        type="payment",
        issuer="processor.example",
        status="Success",
        reference="mandate-hash",
        checks=("es256_signature", "payload_schema", "mandate_reference"),
        details={
            "issued_at": 1_700_000_000,
            "error": None,
            "error_description": None,
            "payment_id": "pay-1",
            "psp_confirmation_id": "psp-1",
            "network_confirmation_id": "network-1",
        },
    )


def test_ap2_inspect_reads_stdin_and_renders_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def inspect(token, root, audience, nonce, **options):
        assert token == "signed-chain"
        assert root == Path("root.jwk")
        assert audience == "merchant"
        assert nonce == "nonce-1"
        assert options == {
            "mandate_type": "auto",
            "transaction_id": None,
            "open_checkout_hash": None,
            "checkout_hash": None,
        }
        return _ap2_inspection()

    monkeypatch.setattr(cli_module, "inspect_ap2", inspect)

    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect",
            "--trust-root",
            "root.jwk",
            "--audience",
            "merchant",
            "--nonce",
            "nonce-1",
            "--format",
            "json",
        ],
        input="signed-chain\n",
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == _ap2_inspection().as_dict()


def test_ap2_inspect_reads_file_and_renders_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "mandate.txt"
    token.write_text("signed-chain", encoding="ascii")
    monkeypatch.setattr(cli_module, "inspect_ap2", lambda *args, **kwargs: _ap2_inspection())

    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect",
            str(token),
            "--trust-root",
            str(tmp_path / "root.jwk"),
            "--audience",
            "merchant",
            "--nonce",
            "nonce-1",
            "--type",
            "payment",
            "--transaction-id",
            "checkout-hash",
            "--open-checkout-hash",
            "open-hash",
        ],
    )

    assert result.exit_code == 0
    assert "AP2 PAYMENT — VALID" in result.output
    assert "Shop (shop-1)" in result.output
    assert "1000 USD" in result.output
    assert "signed-chain" not in result.output


@pytest.mark.parametrize("output_format", ["terminal", "json"])
def test_ap2_inspect_reports_invalid_mandate_with_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    def reject(*args, **kwargs):
        raise cli_module.AP2VerificationError("signature validation failed")

    monkeypatch.setattr(cli_module, "inspect_ap2", reject)
    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect",
            "--trust-root",
            "root.jwk",
            "--audience",
            "merchant",
            "--nonce",
            "nonce-1",
            "--format",
            output_format,
        ],
        input="signed-chain\n",
    )

    assert result.exit_code == 1
    assert "signature validation failed" in result.output
    assert "signed-chain" not in result.output
    if output_format == "json":
        assert json.loads(result.output) == {
            "valid": False,
            "error": "signature validation failed",
        }
    else:
        assert "AP2 MANDATE — INVALID" in result.output


def test_ap2_inspect_reports_setup_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args, **kwargs):
        raise cli_module.AP2Error("official AP2 SDK is unavailable")

    monkeypatch.setattr(cli_module, "inspect_ap2", fail)
    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect",
            "--trust-root",
            "root.jwk",
            "--audience",
            "merchant",
            "--nonce",
            "nonce-1",
        ],
        input="signed-chain\n",
    )

    assert result.exit_code == 2
    assert result.output == "Error: official AP2 SDK is unavailable\n"


def test_ap2_inspect_receipt_reads_stdin_and_renders_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inspect(token, key, reference, **options):
        assert token == "header.payload.signature"
        assert key == Path("issuer.jwk")
        assert reference == "mandate-hash"
        assert options == {
            "receipt_type": "payment",
            "issuer": "processor.example",
            "status": "Success",
            "payment_id": "pay-1",
            "order_id": None,
            "error_code": None,
        }
        return _receipt_inspection()

    monkeypatch.setattr(cli_module, "inspect_ap2_receipt", inspect)

    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect-receipt",
            "--issuer-key",
            "issuer.jwk",
            "--type",
            "payment",
            "--reference",
            "mandate-hash",
            "--issuer",
            "processor.example",
            "--status",
            "Success",
            "--payment-id",
            "pay-1",
            "--format",
            "json",
        ],
        input="header.payload.signature\n",
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == _receipt_inspection().as_dict()


def test_ap2_inspect_receipt_computes_reference_from_mandate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mandate = tmp_path / "mandate.txt"
    mandate.write_text("signed-chain", encoding="ascii")
    monkeypatch.setattr(cli_module, "ap2_mandate_reference", lambda token: f"hash:{token}")
    monkeypatch.setattr(
        cli_module,
        "inspect_ap2_receipt",
        lambda token, key, reference, **options: _receipt_inspection(),
    )

    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect-receipt",
            "--issuer-key",
            "issuer.jwk",
            "--type",
            "payment",
            "--mandate",
            str(mandate),
        ],
        input="header.payload.signature\n",
    )

    assert result.exit_code == 0
    assert "AP2 PAYMENT RECEIPT — VALID" in result.output
    assert "pay-1" in result.output
    assert "header.payload.signature" not in result.output


@pytest.mark.parametrize(
    "references",
    [[], ["--reference", "hash", "--mandate", "tests/test_cli.py"]],
)
def test_ap2_inspect_receipt_requires_exactly_one_reference(references: list[str]) -> None:
    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect-receipt",
            "--issuer-key",
            "issuer.jwk",
            "--type",
            "payment",
            *references,
        ],
        input="header.payload.signature\n",
    )

    assert result.exit_code == 2
    assert "provide exactly one of --mandate or --reference" in result.output


@pytest.mark.parametrize("output_format", ["terminal", "json"])
def test_ap2_inspect_receipt_reports_invalid_signature(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    def reject(*args, **kwargs):
        raise cli_module.AP2VerificationError("receipt signature is invalid")

    monkeypatch.setattr(cli_module, "inspect_ap2_receipt", reject)
    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect-receipt",
            "--issuer-key",
            "issuer.jwk",
            "--type",
            "payment",
            "--reference",
            "hash",
            "--format",
            output_format,
        ],
        input="header.payload.signature\n",
    )

    assert result.exit_code == 1
    assert "receipt signature is invalid" in result.output
    assert "header.payload.signature" not in result.output
    if output_format == "terminal":
        assert "AP2 RECEIPT — INVALID" in result.output
    else:
        assert json.loads(result.output)["valid"] is False


def test_ap2_inspect_receipt_reports_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise cli_module.AP2Error("receipt key is unavailable")

    monkeypatch.setattr(cli_module, "inspect_ap2_receipt", fail)
    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect-receipt",
            "--issuer-key",
            "issuer.jwk",
            "--type",
            "payment",
            "--reference",
            "hash",
        ],
        input="header.payload.signature\n",
    )

    assert result.exit_code == 2
    assert result.output == "Error: receipt key is unavailable\n"


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


def test_check_reports_missing_ap2_runtime_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    def unavailable(_config: ProofConfig) -> None:
        raise cli_module.AP2Error("official AP2 SDK is unavailable")

    monkeypatch.setattr(cli_module, "ensure_ap2_sdk", unavailable)

    result = CliRunner().invoke(main, ["check", str(path)])

    assert result.exit_code == 2
    assert result.output == "Error: official AP2 SDK is unavailable\n"


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
        assert config.agent.transport == "auto"
        assert max_parallel_trials == 1
        return suite

    monkeypatch.setattr(cli_module, "run", run)

    result = CliRunner().invoke(main, ["run", str(path), "--format", "json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["passed"] is False


def test_run_overrides_transport_without_changing_contract(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    original = path.read_text(encoding="utf-8")

    async def run(config, *, max_parallel_trials):
        assert config.agent.transport == "GRPC"
        assert max_parallel_trials == 1
        return SuiteResult(passed=True, duration_ms=1, scenarios=[])

    monkeypatch.setattr(cli_module, "run", run)

    result = CliRunner().invoke(main, ["run", str(path), "--transport", "GRPC"])

    assert result.exit_code == 0
    assert path.read_text(encoding="utf-8") == original


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


def test_diff_runs_selected_contract_against_candidate_and_writes_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(
        """
version: 1
agent: {url: https://baseline.example}
scenarios:
  - {name: first, message: One}
  - {name: second, message: Two}
""",
        encoding="utf-8",
    )
    baseline = SuiteResult(
        passed=True,
        duration_ms=1,
        scenarios=[
            ScenarioResult(
                name="second",
                passed=True,
                passed_trials=1,
                required_trials=1,
                trials=[],
            )
        ],
    )
    candidate = baseline.model_copy(deep=True)

    async def run_pair(baseline_config, candidate_config, *, max_parallel_trials):
        assert [scenario.name for scenario in baseline_config.scenarios] == ["second"]
        assert str(candidate_config.agent.url) == "https://candidate.example/"
        assert baseline_config.agent.transport == "HTTP+JSON"
        assert candidate_config.agent.transport == "HTTP+JSON"
        assert max_parallel_trials == 3
        return baseline, candidate

    monkeypatch.setattr(cli_module, "_run_pair", run_pair)
    output = tmp_path / "diff.json"

    result = CliRunner().invoke(
        main,
        [
            "diff",
            str(path),
            "--against",
            "https://candidate.example",
            "--scenario",
            "second",
            "--jobs",
            "3",
            "--transport",
            "HTTP+JSON",
            "--format",
            "json",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["checks"][0]["change"] == "unchanged"


def test_diff_renders_regression_and_uses_candidate_exit_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    baseline = SuiteResult(
        passed=True,
        duration_ms=1,
        scenarios=[
            ScenarioResult(
                name="smoke",
                passed=True,
                passed_trials=1,
                required_trials=1,
                trials=[],
            )
        ],
    )
    candidate = SuiteResult(
        passed=False,
        duration_ms=1,
        scenarios=[
            ScenarioResult(
                name="smoke",
                passed=False,
                passed_trials=0,
                required_trials=1,
                trials=[],
            )
        ],
    )

    async def run_pair(*args, **kwargs):
        return baseline, candidate

    monkeypatch.setattr(cli_module, "_run_pair", run_pair)

    result = CliRunner().invoke(
        main,
        ["diff", str(path), "--against", "https://candidate.example"],
    )

    assert result.exit_code == 1
    assert "regression" in result.output
    assert "Candidate failed" in result.output

    json_result = CliRunner().invoke(
        main,
        [
            "diff",
            str(path),
            "--against",
            "https://candidate.example",
            "--format",
            "json",
        ],
    )
    assert json_result.exit_code == 1
    assert json.loads(json_result.output)["passed"] is False


def test_diff_validates_options_and_reports_errors(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    terminal_output = CliRunner().invoke(
        main,
        [
            "diff",
            str(path),
            "--against",
            "https://candidate.example",
            "--output",
            str(tmp_path / "diff.json"),
        ],
    )
    invalid_url = CliRunner().invoke(
        main,
        ["diff", str(path), "--against", "not a URL"],
    )

    assert terminal_output.exit_code == 2
    assert "--output requires --format json" in terminal_output.output
    assert invalid_url.exit_code == 2
    assert "URL" in invalid_url.output

    async def fail(*args, **kwargs):
        raise RuntimeError("candidate unavailable")

    monkeypatch.setattr(cli_module, "_run_pair", fail)
    unavailable = CliRunner().invoke(
        main,
        ["diff", str(path), "--against", "https://candidate.example"],
    )
    assert unavailable.exit_code == 2
    assert "candidate unavailable" in unavailable.output


def test_diff_reports_output_write_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "proof.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    suite = SuiteResult(passed=True, duration_ms=1, scenarios=[])

    async def run_pair(*args, **kwargs):
        return suite, suite

    def deny_write(self, content, encoding):
        raise OSError("denied")

    monkeypatch.setattr(cli_module, "_run_pair", run_pair)
    monkeypatch.setattr(Path, "write_text", deny_write)

    result = CliRunner().invoke(
        main,
        [
            "diff",
            str(path),
            "--against",
            "https://candidate.example",
            "--format",
            "json",
            "--output",
            str(tmp_path / "diff.json"),
        ],
    )

    assert result.exit_code == 2
    assert "cannot write" in result.output


@pytest.mark.asyncio
async def test_run_pair_executes_baseline_before_candidate(monkeypatch) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [{"name": "smoke", "message": "Hello"}],
        }
    )
    calls: list[ProofConfig] = []

    async def run(proof, *, max_parallel_trials):
        assert max_parallel_trials == 2
        calls.append(proof)
        return SuiteResult(passed=True, duration_ms=1, scenarios=[])

    monkeypatch.setattr(cli_module, "run", run)

    await cli_module._run_pair(config, config, max_parallel_trials=2)

    assert calls == [config, config]


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
