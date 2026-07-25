from __future__ import annotations

import json
from pathlib import Path

import pytest

import a2a_proof.cassette as cassette_module
from a2a_proof.cassette import (
    CassetteError,
    Recorder,
    load_cassette,
    write_cassette,
)
from a2a_proof.config import load_config
from a2a_proof.models import DataPartResult, FilePartResult, ProofConfig
from a2a_proof.protocol import TurnOutcome
from a2a_proof.runner import run_with_sender

CONTRACT = """
version: 1
agent: {url: https://example.com}
scenarios:
  - name: routing
    message: Where does this ticket go?
    trials: 2
    expect:
      state: completed
      data: [{path: /queue, equals: billing}]
      files: {source: artifact, kind: raw, sha256: %s}
""" % ("a" * 64)


def _config(tmp_path: Path) -> ProofConfig:
    path = tmp_path / "a2a-proof.yaml"
    path.write_text(CONTRACT, encoding="utf-8")
    return load_config(path)


def _outcome(index: int) -> TurnOutcome:
    return TurnOutcome(
        state="completed",
        text=f"routed {index}",
        task_id=f"task-{index}",
        context_id="context",
        duration_ms=index,
        first_event_ms=1,
        states=("working", "completed"),
        data=(DataPartResult(source="artifact", value={"queue": "billing"}),),
        files=(
            FilePartResult(
                source="artifact",
                kind="raw",
                filename="receipt.txt",
                size_bytes=12,
                sha256="a" * 64,
            ),
        ),
    )


async def _recorded_sender(recorder: Recorder):
    responses = iter([_outcome(1), _outcome(2)])

    async def send_turn(message: str | None, **context: object) -> TurnOutcome:
        return next(responses)

    return recorder.wrap(send_turn)


@pytest.mark.asyncio
async def test_records_and_replays_a_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    recorder = Recorder()
    live = await run_with_sender(config, await _recorded_sender(recorder))
    path = tmp_path / "cassette.json"

    write_cassette(path, config, recorder)
    cassette = load_cassette(path)
    replayed = await run_with_sender(config, cassette.sender())

    assert live.passed
    assert replayed.passed
    assert cassette.contract_sha256 == config.contract_sha256
    assert cassette.agent_url == "https://example.com/"
    assert cassette.card is None
    assert [turn.text for turn in cassette.turns] == ["routed 1", "routed 2"]
    assert cassette.turns[0].files[0].sha256 == "a" * 64


@pytest.mark.asyncio
async def test_reports_an_exhausted_cassette(tmp_path: Path) -> None:
    config = _config(tmp_path)
    recorder = Recorder()
    await run_with_sender(config, await _recorded_sender(recorder))
    recorder.turns.pop()
    path = tmp_path / "cassette.json"
    write_cassette(path, config, recorder)

    result = await run_with_sender(config, load_cassette(path).sender())

    assert not result.passed
    assert result.scenarios[0].trials[1].error == (
        "CassetteError: cassette holds 1 recorded turns, and the contract asked for more"
    )


def test_refuses_to_overwrite_or_write_outside_a_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    existing = tmp_path / "cassette.json"
    existing.write_text("{}", encoding="utf-8")

    with pytest.raises(CassetteError, match="cassette path already exists"):
        write_cassette(existing, config, Recorder())
    with pytest.raises(CassetteError, match="cassette parent directory does not exist"):
        write_cassette(tmp_path / "missing" / "cassette.json", config, Recorder())


def test_reports_a_failed_cassette_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    path = tmp_path / "cassette.json"

    def fail(source: object, target: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(cassette_module.os, "replace", fail)

    with pytest.raises(CassetteError, match=r"cannot write .*: Permission denied"):
        write_cassette(path, config, Recorder())
    assert list(tmp_path.glob(".cassette.json.*")) == []


def test_reports_unreadable_and_invalid_cassettes(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    outdated = tmp_path / "outdated.json"
    outdated.write_text(
        json.dumps({"cassette_version": 99, "agent_url": "https://example.com", "turns": []}),
        encoding="utf-8",
    )
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(
        json.dumps(
            {
                "cassette_version": 1,
                "agent_url": "https://example.com",
                "turns": [{"state": "completed"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CassetteError, match="cannot read"):
        load_cassette(missing)
    with pytest.raises(CassetteError, match="cannot parse"):
        load_cassette(invalid)
    with pytest.raises(CassetteError, match="cannot parse"):
        load_cassette(outdated)
    with pytest.raises(CassetteError, match="cannot parse"):
        load_cassette(incomplete)


def test_rejects_an_oversized_cassette(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cassette_module, "MAX_CASSETTE_BYTES", 1)
    path = tmp_path / "cassette.json"
    path.write_text(json.dumps({"cassette_version": 1}), encoding="utf-8")

    with pytest.raises(CassetteError, match="cassette exceeds 1 bytes"):
        load_cassette(path)


def test_rejects_an_invalid_recorded_agent_card(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    path.write_text(
        json.dumps(
            {
                "cassette_version": 1,
                "agent_url": "https://example.com",
                "agent_card": "not base64!",
                "turns": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CassetteError, match="invalid cassette agent_card"):
        load_cassette(path)
