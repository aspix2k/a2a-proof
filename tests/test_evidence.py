from __future__ import annotations

import json
from pathlib import Path

import pytest
from a2a.types import AgentCard

import a2a_proof.evidence as evidence_module
from a2a_proof.config import load_config
from a2a_proof.evidence import (
    REDACTION,
    EvidenceError,
    _bound_record,
    _bounded_data,
    _bounded_files,
    _bounded_prefix,
    _json_line,
    _Redactor,
    _truncate_text,
    agent_card_sha256,
    write_evidence,
)
from a2a_proof.models import (
    CardResult,
    DataPartResult,
    FilePartResult,
    LatencyResult,
    ProofConfig,
    ScenarioResult,
    SuiteResult,
    TrialResult,
    TurnResult,
)


def _config() -> ProofConfig:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {
                "url": "https://example.com",
                "headers": {"Authorization": "Bearer header-secret"},
            },
            "invariants": {"text": {"not_contains_env": "API_TOKEN"}},
            "scenarios": [{"name": "smoke", "message": "Hello"}],
        }
    )
    config.bind_contract_sha256("contract-digest")
    return config


def _failed_result(*, text: str = "Bearer HEADER-SECRET and TOKEN-SECRET") -> SuiteResult:
    turn = TurnResult(
        index=1,
        passed=False,
        state="completed",
        states=["working", "completed"],
        duration_ms=12,
        first_event_ms=2,
        text=text,
        data=[
            DataPartResult(
                source="artifact",
                value={"city": "Москва", "token": "token-secret"},
                artifact_name="result",
            )
        ],
        files=[
            FilePartResult(
                source="artifact",
                kind="url",
                filename="token-secret.txt",
            )
        ],
        failures=["response contains TOKEN-SECRET"],
    )
    trial = TrialResult(
        index=1,
        passed=False,
        duration_ms=15,
        turns=[turn],
    )
    scenario = ScenarioResult(
        name="smoke",
        passed=False,
        passed_trials=0,
        required_trials=1,
        trials=[trial],
    )
    return SuiteResult(
        passed=False,
        duration_ms=20,
        scenarios=[scenario],
        agent_card_sha256="card-digest",
    )


def test_writes_redacted_failed_trial_bundle(tmp_path: Path) -> None:
    output = tmp_path / "evidence"

    write_evidence(output, _config(), _failed_result(), {"API_TOKEN": "token-secret"})

    manifest_text = (output / "manifest.json").read_text(encoding="utf-8")
    records_text = (output / "failures.jsonl").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    record = json.loads(records_text)
    serialized = json.dumps(record).casefold()
    assert manifest | {"tool": None} == {
        "schema_version": 1,
        "tool": None,
        "execution": {"scenarios": ["smoke"], "max_parallel_trials": 1},
        "passed": False,
        "duration_ms": 20,
        "contract_sha256": "contract-digest",
        "agent_card_sha256": "card-digest",
        "failed_trials": 1,
        "recorded_trials": 1,
        "failed_latency_contracts": 0,
        "agent_card_failed": False,
        "truncated": False,
        "records": "failures.jsonl",
    }
    assert manifest["tool"] == {"name": "a2a-proof", "version": "0.11.0"}
    assert manifest_text == json.dumps(manifest, indent=2) + "\n"
    assert record == {
        "kind": "trial",
        "scenario": "smoke",
        "trial": 1,
        "duration_ms": 15,
        "error": None,
        "turns": [
            {
                "index": 1,
                "passed": False,
                "state": "completed",
                "states": ["working", "completed"],
                "duration_ms": 12,
                "first_event_ms": 2,
                "response_redacted": False,
                "failures": ["response contains [REDACTED]"],
            }
        ],
        "events": [
            {"type": "state", "state": "working"},
            {"type": "state", "state": "completed"},
            {
                "type": "response",
                "text": "[REDACTED] and [REDACTED]",
                "data": [
                    {
                        "source": "artifact",
                        "value": {"city": "Москва", "token": "[REDACTED]"},
                        "media_type": None,
                        "artifact_id": None,
                        "artifact_name": "result",
                    }
                ],
                "files": [
                    {
                        "source": "artifact",
                        "kind": "url",
                        "filename": "[REDACTED].txt",
                        "media_type": None,
                        "size_bytes": None,
                        "artifact_id": None,
                        "artifact_name": None,
                    }
                ],
            },
        ],
    }
    assert (
        records_text
        == json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    assert "token-secret" not in serialized
    assert "header-secret" not in serialized
    assert serialized.count("[redacted]") >= 5


def test_writes_empty_records_for_passing_suite(tmp_path: Path) -> None:
    output = tmp_path / "evidence"
    result = SuiteResult(passed=True, duration_ms=1, scenarios=[])

    write_evidence(output, _config(), result, {"API_TOKEN": "token-secret"})

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["failed_trials"] == 0
    assert manifest["failed_latency_contracts"] == 0
    assert manifest["agent_card_failed"] is False
    assert manifest["agent_card_sha256"] is None
    assert (output / "failures.jsonl").read_text(encoding="utf-8") == ""


def test_redacts_environment_value_embedded_in_request_header(tmp_path: Path) -> None:
    contract = tmp_path / "proof.yaml"
    contract.write_text(
        """
version: 1
agent:
  url: https://example.com
  headers:
    Authorization: Bearer ${AUTH_TOKEN}
scenarios: [{name: smoke, message: Hello}]
""",
        encoding="utf-8",
    )
    config = load_config(contract, {"AUTH_TOKEN": "header-secret"})
    output = tmp_path / "evidence"

    write_evidence(output, config, _failed_result(text="HEADER-SECRET"), {})

    content = (output / "failures.jsonl").read_text(encoding="utf-8")
    assert "header-secret" not in content.casefold()
    assert "[REDACTED]" in content


def test_redacts_scenario_names_in_manifest(tmp_path: Path) -> None:
    config = _config()
    config.scenarios[0].name = "token-secret scenario"
    output = tmp_path / "evidence"

    write_evidence(
        output,
        config,
        SuiteResult(passed=True, duration_ms=1, scenarios=[]),
        {"API_TOKEN": "token-secret"},
    )

    manifest = (output / "manifest.json").read_text(encoding="utf-8")
    assert "token-secret" not in manifest.casefold()
    assert json.loads(manifest)["execution"]["scenarios"] == ["[REDACTED] scenario"]


def test_bounds_evidence_after_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_TEXT_CHARS", 10)
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_DATA_CHARS", 10)
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_FILE_PARTS", 0)
    text = "x" * 9 + "token-secret"
    output = tmp_path / "evidence"
    result = _failed_result(text=text)
    result.scenarios[0].trials[0].error = "y" * 20

    write_evidence(output, _config(), result, {"API_TOKEN": "token-secret"})

    record = json.loads((output / "failures.jsonl").read_text(encoding="utf-8"))
    response = record["events"][-1]
    assert response["text"] == "xxxxxxxxx[REDACTED]…[truncated]"
    assert response["data"]["truncated"] is True
    assert response["files"] == {"items": [], "total": 1, "truncated": True}
    assert record["error"] == "yyyyyyyyyy…[truncated]"
    assert "token-secret" not in json.dumps(record).casefold()


def test_caps_recorded_failed_trials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_TRIALS", 1)
    result = _failed_result()
    result.scenarios[0].trials.append(result.scenarios[0].trials[0].model_copy(update={"index": 2}))
    output = tmp_path / "evidence"

    write_evidence(output, _config(), result, {"API_TOKEN": "token-secret"})

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["failed_trials"] == 2
    assert manifest["recorded_trials"] == 1
    assert manifest["truncated"] is True
    assert len((output / "failures.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_records_card_and_latency_failures_without_failed_trials(tmp_path: Path) -> None:
    result = SuiteResult(
        passed=False,
        duration_ms=5,
        card=CardResult(passed=False, failures=["missing skill"]),
        scenarios=[
            ScenarioResult(
                name="timed",
                passed=False,
                passed_trials=1,
                required_trials=1,
                trials=[TrialResult(index=1, passed=True, duration_ms=5)],
                latency=LatencyResult(
                    passed=False,
                    samples=1,
                    p50_ms=5,
                    p95_ms=5,
                    failures=["latency failed"],
                ),
            )
        ],
    )
    output = tmp_path / "evidence"

    write_evidence(output, _config(), result, {"API_TOKEN": "token-secret"})

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (output / "failures.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert manifest["failed_trials"] == 0
    assert manifest["failed_latency_contracts"] == 1
    assert manifest["agent_card_failed"] is True
    assert records == [
        {"kind": "agent_card", "failures": ["missing skill"]},
        {
            "kind": "latency",
            "scenario": "timed",
            "passed": False,
            "samples": 1,
            "p50_ms": 5,
            "p95_ms": 5,
            "failures": ["latency failed"],
        },
    ]


def test_records_trial_error_before_first_turn(tmp_path: Path) -> None:
    result = SuiteResult(
        passed=False,
        duration_ms=5,
        scenarios=[
            ScenarioResult(
                name="broken",
                passed=False,
                passed_trials=0,
                required_trials=1,
                trials=[
                    TrialResult(
                        index=1,
                        passed=False,
                        duration_ms=5,
                        error="connection failed",
                    )
                ],
            )
        ],
    )
    output = tmp_path / "evidence"

    write_evidence(output, _config(), result, {"API_TOKEN": "token-secret"})

    record = json.loads((output / "failures.jsonl").read_text(encoding="utf-8"))
    assert record["error"] == "connection failed"
    assert record["turns"] == []
    assert record["events"] == []


def test_rejects_existing_path_and_missing_parent(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()

    with pytest.raises(EvidenceError, match="already exists"):
        write_evidence(existing, _config(), _failed_result(), {"API_TOKEN": "token-secret"})
    with pytest.raises(EvidenceError, match="parent directory does not exist"):
        write_evidence(
            tmp_path / "missing" / "evidence",
            _config(),
            _failed_result(),
            {"API_TOKEN": "token-secret"},
        )


def test_removes_temporary_bundle_after_write_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[bool] = []
    remove_tree = evidence_module.shutil.rmtree

    def fail(path: Path, content: str) -> None:
        raise OSError("denied")

    def remove(path: Path, *, ignore_errors: bool) -> None:
        removed.append(ignore_errors)
        remove_tree(path)

    monkeypatch.setattr(evidence_module, "_write_file", fail)
    monkeypatch.setattr(evidence_module.shutil, "rmtree", remove)

    with pytest.raises(OSError, match="denied"):
        write_evidence(
            tmp_path / "evidence",
            _config(),
            _failed_result(),
            {"API_TOKEN": "token-secret"},
        )
    assert list(tmp_path.iterdir()) == []
    assert removed == [True]


def test_redactor_handles_nested_values_and_ignores_bytes() -> None:
    redactor = _Redactor({"secret", "secret-long", "bc", "abc", ""})

    assert redactor.redact({"secret-key": ["SECRET-LONG abc", 1, b"secret"]}) == {
        "[REDACTED]-key": ["[REDACTED] [REDACTED]", 1, b"secret"]
    }


def test_redactor_applies_bounded_regex_substitution() -> None:
    class Pattern:
        def sub(self, replacement: str, value: str, *, timeout: float) -> str:
            assert replacement == REDACTION
            assert value == "secret"
            assert timeout == 0.1
            return replacement

    redactor = _Redactor(set())
    redactor._patterns = [Pattern()]  # type: ignore[list-item]

    assert redactor.redact("secret") == REDACTION


def test_serialization_and_bounds_are_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    value = [{"city": "Москва", "count": 1}, {"city": "Казань", "count": 2}]
    encoded = '[{"city":"Москва","count":1},{"city":"Казань","count":2}]'
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_DATA_CHARS", len(encoded))
    assert _bounded_data(value) == value
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_DATA_CHARS", len(encoded) - 1)
    assert _bounded_data(value) == {"preview": encoded[:-1], "truncated": True}

    files = [{"filename": "a"}]
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_FILE_PARTS", 1)
    assert _bounded_files(files) == files
    assert _bounded_files([*files, {"filename": "b"}]) == {
        "items": files,
        "total": 2,
        "truncated": True,
    }

    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_TEXT_CHARS", 3)
    assert _truncate_text("abc") == "abc"
    assert _truncate_text("abcd") == "abc…[truncated]"
    assert _bounded_prefix("abcdef", 3) == "abc"
    assert _bounded_prefix(f"{REDACTION}yy", 4) == REDACTION
    assert _bounded_prefix(f"xx{REDACTION}yy", 4) == f"xx{REDACTION}"
    assert _bounded_prefix(f"[noise]xx{REDACTION}yy", 12) == f"[noise]xx{REDACTION}"
    assert _json_line({"z": "Москва", "a": 1}) == '{"a":1,"z":"Москва"}\n'


def test_bounds_top_level_failure_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence_module, "MAX_EVIDENCE_TEXT_CHARS", 3)

    assert _bound_record({"kind": "agent_card", "failures": ["abcd"]}) == {
        "kind": "agent_card",
        "failures": ["abc…[truncated]"],
    }


def test_uses_hidden_temporary_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = evidence_module.tempfile.mkdtemp

    def make_temporary(**arguments: object) -> str:
        assert arguments == {"prefix": ".evidence.", "dir": tmp_path}
        prefix = arguments["prefix"]
        parent = arguments["dir"]
        assert isinstance(prefix, str)
        assert isinstance(parent, Path)
        return original(prefix=prefix, dir=parent)

    monkeypatch.setattr(evidence_module.tempfile, "mkdtemp", make_temporary)

    write_evidence(
        tmp_path / "evidence",
        _config(),
        SuiteResult(passed=True, duration_ms=1, scenarios=[]),
        {"API_TOKEN": "token-secret"},
    )


def test_agent_card_digest_is_deterministic() -> None:
    card = AgentCard(name="Agent")

    assert agent_card_sha256(card) == agent_card_sha256(card)
    assert len(agent_card_sha256(card)) == 64
