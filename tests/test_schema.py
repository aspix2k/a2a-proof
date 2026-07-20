from __future__ import annotations

import json
from collections.abc import Mapping
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
                        "ap2": {
                            "type": "payment",
                            "trusted_root_jwk": "fixtures/root.jwk",
                            "audience": "merchant",
                            "nonce": "nonce-1",
                            "transaction_id": "tx-1",
                        },
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
            "push_notifications": {},
            "scenarios": [
                {
                    "name": "lifecycle",
                    "turns": [
                        {"message": "Start", "return_immediately": True},
                        {"action": "cancel"},
                        {"action": "get_task", "history_length": 5},
                        {"action": "subscribe"},
                    ],
                },
                {
                    "name": "push",
                    "turns": [
                        {
                            "message": "Start",
                            "return_immediately": True,
                            "push_notification": True,
                        },
                        {"action": "await_push", "timeout_seconds": 30},
                    ],
                },
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
    assert not validator.is_valid(configuration({"action": "await_push", "history_length": 1}))
    assert not validator.is_valid(configuration({"message": "Start", "timeout_seconds": 1}))

    push = configuration(
        {"message": "Start", "return_immediately": True, "push_notification": True}
    )
    assert validator.is_valid(push)
    assert not validator.is_valid(configuration({"message": "Start", "push_notification": True}))


def test_schema_requires_raw_file_content_assertions() -> None:
    validator = Draft202012Validator(config_schema())

    def configuration(file_expectation: dict[str, object]) -> dict[str, object]:
        return {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "file integrity",
                    "message": "Create a file",
                    "expect": {"files": file_expectation},
                }
            ],
        }

    assert validator.is_valid(
        configuration(
            {
                "kind": "raw",
                "min_size_bytes": 1,
                "max_size_bytes": 10,
                "sha256": "a" * 64,
            }
        )
    )
    assert not validator.is_valid(configuration({"sha256": "a" * 64}))
    assert not validator.is_valid(configuration({"kind": "url", "size_bytes": 1}))
    assert not validator.is_valid(
        configuration({"kind": "raw", "size_bytes": 1, "max_size_bytes": 2})
    )
    assert not validator.is_valid(configuration({"kind": "raw", "size_bytes": -1}))
    assert not validator.is_valid(configuration({"kind": "raw", "size_bytes": 20_000_001}))
    assert not validator.is_valid(configuration({"kind": "raw", "sha256": None}))


def test_schema_rejects_invalid_ap2_expectation_shape() -> None:
    validator = Draft202012Validator(config_schema())

    def configuration(expectation: Mapping[str, object]) -> dict[str, object]:
        return {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "payment",
                    "message": "Pay",
                    "expect": {"ap2": expectation},
                }
            ],
        }

    base = {
        "type": "payment",
        "trusted_root_jwk": "fixtures/root.jwk",
        "audience": "merchant",
        "nonce": "nonce-1",
    }
    assert not validator.is_valid(configuration({**base, "path": "/bad~2escape"}))
    assert not validator.is_valid(
        configuration({**base, "source": "message", "artifact_name": "payment"})
    )
    assert not validator.is_valid(configuration({**base, "checkout_hash": "hash"}))
    assert not validator.is_valid(
        configuration({**base, "type": "checkout", "transaction_id": "tx"})
    )

    receipt = {
        "kind": "receipt",
        "type": "payment",
        "binds_to": "payment",
        "trusted_issuer_jwk": "fixtures/issuer.jwk",
        "path": "/payment_receipt",
    }
    assert validator.is_valid(configuration(receipt))
    assert not validator.is_valid(configuration({**receipt, "path": "/bad~2escape"}))
    assert not validator.is_valid(
        configuration({**receipt, "source": "message", "artifact_name": "receipt"})
    )
    assert not validator.is_valid(configuration({**receipt, "order_id": "order-1"}))
    assert not validator.is_valid(
        configuration({**receipt, "type": "checkout", "payment_id": "pay-1"})
    )
    assert not validator.is_valid(
        configuration({**receipt, "status": "Success", "error": "declined"})
    )
