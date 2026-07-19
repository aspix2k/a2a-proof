from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Self

import regex
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

NonEmptyText = Annotated[str, Field(min_length=1, max_length=100_000)]
ExtensionUri = Annotated[str, Field(min_length=1, max_length=1_000)]
MAX_INPUT_DATA_BYTES = 1_000_000
MAX_EXTENSIONS = 20
MAX_EXTENSION_PARAMETER_CHARS = 8_000
MAX_JSON_SCHEMA_BYTES = 100_000
MAX_JSON_SCHEMA_DEPTH = 50
_URL_ADAPTER = TypeAdapter(AnyUrl)
_DATA_PREDICATES = {"equals", "exists", "matches", "gt", "gte", "lt", "lte", "json_schema"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TextExpectation(StrictModel):
    contains: list[NonEmptyText] = Field(
        default_factory=list,
        max_length=100,
        description="Required substrings.",
    )
    not_contains: list[NonEmptyText] = Field(
        default_factory=list,
        max_length=100,
        description="Forbidden substrings.",
    )
    matches: list[NonEmptyText] = Field(
        default_factory=list,
        max_length=100,
        description="Required regular expressions.",
    )
    equals: str | None = Field(default=None, description="Required complete response text.")
    case_sensitive: bool = Field(default=True, description="Apply case-sensitive text checks.")

    @field_validator(
        "contains",
        "not_contains",
        "matches",
        mode="before",
        json_schema_input_type=str | list[str],
    )
    @classmethod
    def accept_single_value(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value

    @field_validator("matches")
    @classmethod
    def validate_patterns(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                regex.compile(pattern)
            except regex.error as error:
                raise ValueError(f"invalid regular expression {pattern!r}: {error}") from error
        return patterns


class DataExpectation(StrictModel):
    equals: JsonValue | None = Field(default=None, description="Required JSON value.")
    exists: bool | SkipJsonSchema[None] = Field(
        default=None,
        description="Whether the selected path must exist.",
    )
    matches: NonEmptyText | SkipJsonSchema[None] = Field(
        default=None,
        description="Regular expression required for a string value.",
    )
    gt: int | float | SkipJsonSchema[None] = Field(
        default=None,
        description="Exclusive numeric lower bound.",
    )
    gte: int | float | SkipJsonSchema[None] = Field(
        default=None,
        description="Inclusive numeric lower bound.",
    )
    lt: int | float | SkipJsonSchema[None] = Field(
        default=None,
        description="Exclusive numeric upper bound.",
    )
    lte: int | float | SkipJsonSchema[None] = Field(
        default=None,
        description="Inclusive numeric upper bound.",
    )
    json_schema: dict[str, JsonValue] | bool | SkipJsonSchema[None] = Field(
        default=None,
        description="Inline JSON Schema Draft 2020-12 document.",
    )
    path: Annotated[
        str,
        Field(max_length=1_000, description="RFC 6901 JSON Pointer; empty selects the root."),
    ] = ""
    source: Literal["message", "artifact"] | None = Field(
        default=None,
        description="Limit matching to message or artifact data parts.",
    )
    artifact_name: (
        Annotated[
            str,
            Field(min_length=1, max_length=200, description="Required artifact name."),
        ]
        | None
    ) = None
    media_type: (
        Annotated[
            str,
            Field(min_length=1, max_length=200, description="Required data-part media type."),
        ]
        | None
    ) = None

    @field_validator("path")
    @classmethod
    def validate_json_pointer(cls, path: str) -> str:
        if not re.fullmatch(r"(?:/(?:[^~]|~[01])*)*", path):
            raise ValueError("path must be an RFC 6901 JSON Pointer")
        return path

    @field_validator("matches")
    @classmethod
    def validate_pattern(cls, pattern: str | None) -> str | None:
        if pattern is None:
            return None
        try:
            regex.compile(pattern)
        except regex.error as error:
            raise ValueError(f"invalid regular expression {pattern!r}: {error}") from error
        return pattern

    @field_validator("gt", "gte", "lt", "lte", mode="before")
    @classmethod
    def require_number(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("numeric comparisons require a number")
        return value

    @field_validator("json_schema")
    @classmethod
    def validate_json_schema(
        cls,
        schema: dict[str, JsonValue] | bool | None,
    ) -> dict[str, JsonValue] | bool | None:
        if schema is None:
            return None
        _validate_json_schema_limits(schema)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ValueError(f"invalid JSON Schema: {error.message}") from error
        return schema

    @model_validator(mode="after")
    def validate_expectation(self) -> Self:
        if self.source == "message" and self.artifact_name is not None:
            raise ValueError("artifact_name cannot be used with source: message")
        predicates = self.model_fields_set & _DATA_PREDICATES
        if not predicates:
            raise ValueError("data expectation must define at least one assertion")
        empty = sorted(
            predicate for predicate in predicates - {"equals"} if getattr(self, predicate) is None
        )
        if empty:
            raise ValueError(f"data assertion {empty[0]} cannot be null")
        groups = [
            bool(predicates & {"equals"}),
            bool(predicates & {"exists"}),
            bool(predicates & {"matches"}),
            bool(predicates & {"gt", "gte", "lt", "lte"}),
            bool(predicates & {"json_schema"}),
        ]
        if sum(groups) > 1:
            raise ValueError("data expectation must use one assertion type")
        if self.exists is False and not self.path:
            raise ValueError("exists: false requires a non-empty path")
        return self


class Expectation(StrictModel):
    state: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Required terminal A2A state.",
    )
    text: TextExpectation | None = Field(default=None, description="Response text checks.")
    data: list[DataExpectation] = Field(
        default_factory=list,
        max_length=100,
        description="Structured response checks.",
    )
    max_seconds: float | None = Field(
        default=None,
        gt=0,
        le=600,
        description="Maximum complete turn duration.",
    )
    max_first_event_seconds: float | None = Field(
        default=None,
        gt=0,
        le=600,
        description="Maximum time until the first response event.",
    )

    @field_validator(
        "data",
        mode="before",
        json_schema_input_type=DataExpectation | list[DataExpectation],
    )
    @classmethod
    def accept_single_data_expectation(cls, value: object) -> object:
        return [value] if isinstance(value, (dict, DataExpectation)) else value


class Turn(StrictModel):
    message: NonEmptyText | None = Field(default=None, description="Text sent to the agent.")
    data: list[JsonValue] = Field(
        default_factory=list,
        max_length=100,
        description="Structured data parts sent to the agent.",
    )
    expect: Expectation = Field(default_factory=Expectation, description="Expected response.")

    @field_validator(
        "data",
        mode="before",
        json_schema_input_type=dict[str, JsonValue] | list[JsonValue],
    )
    @classmethod
    def accept_single_data_part(cls, value: object) -> object:
        return [value] if isinstance(value, dict) else value

    @field_validator("data")
    @classmethod
    def validate_data_size(cls, data: list[JsonValue]) -> list[JsonValue]:
        _validate_input_data_size(data)
        return data

    @model_validator(mode="after")
    def require_content(self) -> Self:
        if self.message is None and not self.data:
            raise ValueError("turn must contain message or data")
        return self


class Scenario(StrictModel):
    name: Annotated[
        str,
        Field(min_length=1, max_length=200, description="Unique scenario name."),
    ]
    message: NonEmptyText | None = Field(default=None, description="Single-turn text input.")
    data: list[JsonValue] = Field(
        default_factory=list,
        max_length=100,
        description="Single-turn structured input.",
    )
    turns: (
        Annotated[
            list[Turn],
            Field(min_length=1, max_length=50, description="Ordered multi-turn conversation."),
        ]
        | None
    ) = None
    expect: Expectation = Field(default_factory=Expectation, description="Single-turn response.")
    trials: int = Field(default=1, ge=1, le=100, description="Independent repetitions.")
    pass_rate: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description="Minimum successful trial fraction.",
    )

    @field_validator(
        "data",
        mode="before",
        json_schema_input_type=dict[str, JsonValue] | list[JsonValue],
    )
    @classmethod
    def accept_single_data_part(cls, value: object) -> object:
        return [value] if isinstance(value, dict) else value

    @field_validator("data")
    @classmethod
    def validate_data_size(cls, data: list[JsonValue]) -> list[JsonValue]:
        _validate_input_data_size(data)
        return data

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.turns is not None and (self.message is not None or self.data):
            raise ValueError("set exactly one of single-turn input or turns")
        if self.turns is None and self.message is None and not self.data:
            raise ValueError("scenario must contain message, data, or turns")
        if self.turns is not None and "expect" in self.model_fields_set:
            raise ValueError("put expect on each turn when using turns")
        return self

    def resolved_turns(self) -> list[Turn]:
        if self.turns is not None:
            return self.turns
        return [Turn(message=self.message, data=self.data, expect=self.expect)]


class AgentConfig(StrictModel):
    url: HttpUrl = Field(description="Agent Card discovery base URL.")
    timeout: float = Field(default=30, gt=0, le=600, description="Turn timeout in seconds.")
    transport: Literal["auto", "JSONRPC", "HTTP+JSON", "GRPC"] = Field(
        default="auto",
        description="Required protocol binding or Agent Card preference.",
    )
    grpc_tls: bool = Field(default=True, description="Use TLS for gRPC interfaces.")
    allow_cross_origin_interfaces: bool = Field(
        default=False,
        description="Allow Agent Card interfaces on another origin.",
    )
    card_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Custom Agent Card path.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Request headers; keep values in environment variables.",
    )
    extensions: list[ExtensionUri] = Field(
        default_factory=list,
        max_length=MAX_EXTENSIONS,
        description="Advertised A2A extensions to activate.",
    )

    @field_validator("url")
    @classmethod
    def reject_url_credentials(cls, url: HttpUrl) -> HttpUrl:
        if url.username is not None or url.password is not None:
            raise ValueError("agent URL must not contain credentials; use headers instead")
        return url

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        for name, value in headers.items():
            if not regex.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise ValueError(f"invalid HTTP header name: {name!r}")
            if "\r" in value or "\n" in value:
                raise ValueError(f"HTTP header {name!r} contains a line break")
            if name.lower() == "a2a-extensions":
                _validate_extension_uris(_split_extension_parameter(value))
        return headers

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, extensions: list[str]) -> list[str]:
        _validate_extension_uris(extensions)
        return extensions

    @model_validator(mode="after")
    def validate_combined_extensions(self) -> Self:
        _validate_extension_uris(self.requested_extensions())
        return self

    def requested_extensions(self) -> list[str]:
        extensions = list(self.extensions)
        for name, value in self.headers.items():
            if name.lower() == "a2a-extensions":
                extensions.extend(_split_extension_parameter(value))
        return list(dict.fromkeys(extensions))


class ProofConfig(StrictModel):
    version: Literal[1] = Field(description="Configuration format version.")
    agent: AgentConfig = Field(description="Agent discovery and transport settings.")
    scenarios: Annotated[
        list[Scenario],
        Field(min_length=1, max_length=1_000, description="Behavior contracts to run."),
    ]

    @field_validator("scenarios")
    @classmethod
    def require_unique_names(cls, scenarios: list[Scenario]) -> list[Scenario]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for scenario in scenarios:
            if scenario.name in seen:
                duplicates.add(scenario.name)
            seen.add(scenario.name)
        if duplicates:
            names = ", ".join(sorted(duplicates))
            raise ValueError(f"scenario names must be unique: {names}")
        return scenarios


class DataPartResult(StrictModel):
    source: Literal["message", "artifact"]
    value: JsonValue
    media_type: str | None = None
    artifact_id: str | None = None
    artifact_name: str | None = None


class TurnResult(StrictModel):
    index: int
    passed: bool
    state: str
    duration_ms: int
    first_event_ms: int | None = None
    text: str
    data: list[DataPartResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class TrialResult(StrictModel):
    index: int
    passed: bool
    duration_ms: int
    turns: list[TurnResult] = Field(default_factory=list)
    error: str | None = None


class ScenarioResult(StrictModel):
    name: str
    passed: bool
    passed_trials: int
    required_trials: int
    trials: list[TrialResult]


class SuiteResult(StrictModel):
    passed: bool
    duration_ms: int
    scenarios: list[ScenarioResult]


def _validate_input_data_size(data: list[JsonValue]) -> None:
    size = len(
        json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    )
    if size > MAX_INPUT_DATA_BYTES:
        raise ValueError(f"input data exceeds {MAX_INPUT_DATA_BYTES} bytes")


def _validate_json_schema_limits(schema: dict[str, JsonValue] | bool) -> None:
    size = len(
        json.dumps(schema, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    if size > MAX_JSON_SCHEMA_BYTES:
        raise ValueError(f"json_schema exceeds {MAX_JSON_SCHEMA_BYTES} bytes")

    pending: list[tuple[JsonValue, int]] = [(schema, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_JSON_SCHEMA_DEPTH:
            raise ValueError(f"json_schema exceeds {MAX_JSON_SCHEMA_DEPTH} levels")
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in {"$ref", "$dynamicRef"}
                    and isinstance(item, str)
                    and not item.startswith("#")
                ):
                    raise ValueError("json_schema references must be local")
                pending.append((item, depth + 1))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


def _split_extension_parameter(value: str) -> list[str]:
    extensions = [item.strip() for item in value.split(",")]
    if any(not item for item in extensions):
        raise ValueError("A2A-Extensions must contain comma-separated extension URIs")
    return extensions


def _validate_extension_uris(extensions: list[str]) -> None:
    if len(extensions) > MAX_EXTENSIONS:
        raise ValueError(f"configure at most {MAX_EXTENSIONS} extension URIs")
    if len(set(extensions)) != len(extensions):
        raise ValueError("extension URIs must be unique")
    if len(",".join(extensions)) > MAX_EXTENSION_PARAMETER_CHARS:
        raise ValueError(f"A2A-Extensions exceeds {MAX_EXTENSION_PARAMETER_CHARS} characters")
    for uri in extensions:
        if not uri.isascii() or "," in uri or any(character.isspace() for character in uri):
            raise ValueError(f"invalid extension URI: {uri!r}")
        try:
            _URL_ADAPTER.validate_python(uri)
        except ValidationError as error:
            raise ValueError(f"invalid extension URI: {uri!r}") from error
