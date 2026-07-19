from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from a2a_proof.config import CONFIG_SCHEMA_URL, config_schema

ROOT = Path(__file__).parents[1]
SCHEMA_PATH = ROOT / "schema" / "a2a-proof.schema.json"


def test_committed_schema_matches_models() -> None:
    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8")) == config_schema()


def test_schema_accepts_examples_and_configuration_shorthand() -> None:
    schema = config_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    for path in (ROOT / "examples").glob("*.yaml"):
        validator.validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    validator.validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "structured",
                    "data": {"action": "forecast"},
                    "latency": {"p50_seconds": 1, "p95_seconds": 2},
                    "files": [
                        "fixtures/report.pdf",
                        {"path": "fixtures/report.pdf", "media_type": "application/pdf"},
                    ],
                    "expect": {
                        "text": {"contains": "Paris"},
                        "data": {"path": "/temperature", "gte": 20, "lt": 30},
                        "states": {"contains_in_order": ["working", "completed"]},
                        "files": {"media_type": "application/pdf", "count": 1},
                    },
                }
            ],
            "card": {
                "skills": {"contains": "summarize"},
                "capabilities": {"streaming": True},
            },
            "invariants": {
                "text": {
                    "not_contains": "system prompt",
                    "not_contains_env": "API_TOKEN",
                }
            },
            "defaults": {"trials": 3, "pass_rate": 0.66},
        }
    )
    validator.validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "lifecycle",
                    "turns": [
                        {"message": "Start", "return_immediately": True},
                        {"action": "cancel"},
                        {"action": "get_task", "history_length": 5},
                    ],
                }
            ],
        }
    )


def test_schema_rejects_unknown_fields() -> None:
    validator = Draft202012Validator(config_schema())
    errors = list(
        validator.iter_errors(
            {
                "version": 1,
                "agent": {"url": "https://example.com", "unknown": True},
                "scenarios": [{"name": "smoke", "message": "Hello"}],
            }
        )
    )

    assert [error.message for error in errors] == [
        "Additional properties are not allowed ('unknown' was unexpected)"
    ]


def test_schema_requires_one_structured_assertion_type() -> None:
    validator = Draft202012Validator(config_schema())

    def configuration(data_expectation: dict[str, object]) -> dict[str, object]:
        return {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "structured",
                    "message": "Hello",
                    "expect": {"data": data_expectation},
                }
            ],
        }

    assert not validator.is_valid(configuration({"path": "/value"}))
    assert not validator.is_valid(configuration({"equals": 1, "gt": 0}))
    assert validator.is_valid(configuration({"gte": 0, "lt": 10}))


def test_schema_requires_nonempty_card_and_exclusive_state_sequence() -> None:
    validator = Draft202012Validator(config_schema())
    base = {
        "version": 1,
        "agent": {"url": "https://example.com"},
        "scenarios": [{"name": "smoke", "message": "Hello"}],
    }

    assert not validator.is_valid({**base, "card": {}})
    assert not validator.is_valid({**base, "invariants": {"text": {}}})
    assert not validator.is_valid(
        {
            **base,
            "invariants": {"text": {"not_contains_env": "invalid-name"}},
        }
    )
    assert not validator.is_valid(
        {
            **base,
            "scenarios": [{"name": "smoke", "message": "Hello", "latency": {}}],
        }
    )
    assert not validator.is_valid(
        {
            **base,
            "scenarios": [
                {
                    "name": "smoke",
                    "message": "Hello",
                    "expect": {
                        "states": {
                            "equals": ["completed"],
                            "contains_in_order": ["completed"],
                        }
                    },
                }
            ],
        }
    )


def test_schema_has_stable_identity() -> None:
    schema = config_schema()

    assert schema["$id"] == CONFIG_SCHEMA_URL
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_schema_rejects_invalid_task_action_turns() -> None:
    validator = Draft202012Validator(config_schema())

    def configuration(turn: dict[str, object]) -> dict[str, object]:
        return {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "lifecycle",
                    "turns": [{"message": "Start"}, turn],
                }
            ],
        }

    assert not validator.is_valid(configuration({"action": "cancel", "message": "also send"}))
    assert not validator.is_valid(configuration({"action": "cancel", "history_length": 1}))
    assert not validator.is_valid(
        configuration({"action": "get_task", "return_immediately": False})
    )
    assert not validator.is_valid(configuration({"history_length": 1}))
