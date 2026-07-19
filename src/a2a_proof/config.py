from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from a2a_proof.ap2 import AP2Error, validate_config_ap2
from a2a_proof.files import FileInputError, validate_config_files
from a2a_proof.models import ProofConfig

MAX_CONFIG_BYTES = 1_000_000
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
CONFIG_SCHEMA_URL = (
    "https://raw.githubusercontent.com/aspix2k/a2a-proof/main/schema/a2a-proof.schema.json"
)
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


class ConfigError(ValueError):
    pass


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> ProofConfig:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror or error}") from error
    if len(content) > MAX_CONFIG_BYTES:
        raise ConfigError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")

    try:
        raw = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise ConfigError(f"cannot parse {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    try:
        environment = os.environ if environ is None else environ
        expanded = _expand_environment(raw, environment)
        config = ProofConfig.model_validate(expanded)
        resolve_invariant_secrets(config, environment)
        config.bind_contract_dir(path.parent)
        config.bind_contract_sha256(sha256(content).hexdigest())
        config.bind_redaction_values(_header_environment_values(raw, environment))
        validate_config_files(config)
        validate_config_ap2(config)
        return config
    except (AP2Error, ConfigError, FileInputError, ValidationError) as error:
        raise ConfigError(str(error)) from error


def write_config(path: Path, data: Mapping[str, Any], *, force: bool = False) -> None:
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists; pass --force to replace it")
    if not path.parent.is_dir():
        raise ConfigError(f"parent directory does not exist: {path.parent}")

    content = f"# yaml-language-server: $schema={CONFIG_SCHEMA_URL}\n\n" + yaml.safe_dump(
        dict(data),
        allow_unicode=True,
        sort_keys=False,
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ConfigError(f"cannot write {path}: {error.strerror or error}") from error


def config_schema() -> dict[str, Any]:
    schema = ProofConfig.model_json_schema(mode="validation")
    definitions = schema["$defs"]
    data_expectation = schema["$defs"]["DataExpectation"]
    for name in ("exists", "matches", "gt", "gte", "lt", "lte", "json_schema"):
        data_expectation["properties"][name].pop("default")
    data_expectation["oneOf"] = [
        {"required": ["equals"]},
        {"required": ["exists"]},
        {"required": ["matches"]},
        {
            "anyOf": [
                {"required": ["gt"]},
                {"required": ["gte"]},
                {"required": ["lt"]},
                {"required": ["lte"]},
            ]
        },
        {"required": ["json_schema"]},
    ]
    file_input = definitions["FileInput"]
    definitions["FileInput"] = {
        "title": file_input["title"],
        "description": "A relative path or file input object.",
        "anyOf": [
            {"type": "string", "minLength": 1, "maxLength": 1_000},
            file_input,
        ],
    }
    contains = definitions["ContainsExpectation"]["properties"]["contains"]
    contains["anyOf"] = [
        {"type": "string", "minLength": 1, "maxLength": 200},
        {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "minItems": 1,
            "maxItems": 100,
        },
    ]
    for name in ("data", "files", "ap2"):
        multiple = definitions["Expectation"]["properties"][name]["anyOf"][1]
        multiple["maxItems"] = 100
    ap2_expectation = definitions["AP2MandateExpectation"]
    ap2_expectation["properties"]["path"]["anyOf"][0]["pattern"] = r"^(?:/(?:[^~]|~[01])*)*$"
    ap2_expectation["allOf"] = [
        {
            "if": {
                "properties": {"source": {"const": "message"}},
                "required": ["source"],
            },
            "then": {"not": {"required": ["artifact_name"]}},
        },
        {
            "if": {
                "properties": {"type": {"const": "checkout"}},
                "required": ["type"],
            },
            "then": {
                "not": {
                    "anyOf": [
                        {"required": ["transaction_id"]},
                        {"required": ["open_checkout_hash"]},
                    ]
                }
            },
        },
        {
            "if": {
                "properties": {"type": {"const": "payment"}},
                "required": ["type"],
            },
            "then": {"not": {"required": ["checkout_hash"]}},
        },
    ]
    capabilities = definitions["AgentCapabilitiesExpectation"]
    capability_names = ("streaming", "push_notifications", "extended_agent_card")
    for name in capability_names:
        capabilities["properties"][name].pop("default")
    capabilities["anyOf"] = [{"required": [name]} for name in capability_names]
    card = definitions["AgentCardExpectation"]
    card_names = ("skills", "capabilities", "input_modes", "output_modes")
    for name in card_names:
        card["properties"][name].pop("default")
    card["anyOf"] = [{"required": [name]} for name in card_names]
    states = definitions["StateSequenceExpectation"]
    for name in ("equals", "contains_in_order"):
        states["properties"][name].pop("default")
    states["oneOf"] = [
        {"required": ["equals"]},
        {"required": ["contains_in_order"]},
    ]
    latency = definitions["LatencyExpectation"]
    for name in ("p50_seconds", "p95_seconds"):
        latency["properties"][name].pop("default")
    latency["anyOf"] = [
        {"required": ["p50_seconds"]},
        {"required": ["p95_seconds"]},
    ]
    text_invariant = definitions["TextInvariant"]
    invariant_properties = text_invariant["properties"]
    invariant_properties["not_contains"]["anyOf"] = [
        {"type": "string", "minLength": 1, "maxLength": 100_000},
        {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 100_000},
            "maxItems": 100,
        },
    ]
    environment_name = {
        "type": "string",
        "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
        "maxLength": 200,
    }
    invariant_properties["not_contains_env"]["anyOf"] = [
        environment_name,
        {"type": "array", "items": environment_name, "maxItems": 100},
    ]
    text_invariant["anyOf"] = [
        {"required": ["not_contains"]},
        {"required": ["not_contains_env"]},
    ]
    turn = definitions["Turn"]
    content_fields = [{"required": [name]} for name in ("message", "data", "files")]
    content_forbidden = [{"required": [name]} for name in ("action", "history_length")]
    action_forbidden = [
        {"required": [name]} for name in ("message", "data", "files", "return_immediately")
    ]
    turn["oneOf"] = [
        {
            "anyOf": content_fields,
            "not": {"anyOf": content_forbidden},
        },
        {
            "required": ["action"],
            "properties": {"action": {"const": "cancel"}},
            "not": {"anyOf": [*action_forbidden, {"required": ["history_length"]}]},
        },
        {
            "required": ["action"],
            "properties": {"action": {"const": "get_task"}},
            "not": {"anyOf": action_forbidden},
        },
    ]
    schema.update(
        {
            "$schema": JSON_SCHEMA_DIALECT,
            "$id": CONFIG_SCHEMA_URL,
            "title": "a2a-proof configuration",
            "description": "Black-box behavior contracts for one A2A agent.",
        }
    )
    return schema


def resolve_invariant_secrets(
    config: ProofConfig,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if config.invariants is None:
        return {}
    environment = os.environ if environ is None else environ
    names = config.invariants.text.not_contains_env
    missing = sorted(name for name in names if name not in environment)
    if missing:
        raise ConfigError(f"missing environment variable(s): {', '.join(missing)}")
    empty = sorted(name for name in names if not environment[name])
    if empty:
        raise ConfigError(f"environment variable(s) must not be empty: {', '.join(empty)}")
    return {name: environment[name] for name in names}


def _header_environment_values(
    raw: Mapping[str, Any],
    environ: Mapping[str, str],
) -> list[str]:
    agent = raw.get("agent")
    if not isinstance(agent, Mapping):
        return []
    headers = agent.get("headers")
    if not isinstance(headers, Mapping):
        return []
    return [
        environ[name]
        for value in headers.values()
        if isinstance(value, str)
        for name in ENV_REFERENCE.findall(value)
        if name in environ
    ]


def _expand_environment(value: Any, environ: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        missing: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = environ.get(name)
            if resolved is None:
                missing.add(name)
                return match.group(0)
            return resolved

        expanded = ENV_REFERENCE.sub(replace, value)
        if missing:
            names = ", ".join(sorted(missing))
            raise ConfigError(f"missing environment variable(s): {names}")
        return expanded
    if isinstance(value, Mapping):
        return {key: _expand_environment(item, environ) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_expand_environment(item, environ) for item in value]
    return value
