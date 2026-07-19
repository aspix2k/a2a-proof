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
                    "expect": {
                        "text": {"contains": "Paris"},
                        "data": {"path": "/temperature", "gte": 20, "lt": 30},
                    },
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


def test_schema_has_stable_identity() -> None:
    schema = config_schema()

    assert schema["$id"] == CONFIG_SCHEMA_URL
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
