from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Self

import regex
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

NonEmptyText = Annotated[str, Field(min_length=1, max_length=100_000)]
ExtensionUri = Annotated[str, Field(min_length=1, max_length=1_000)]
MAX_INPUT_DATA_BYTES = 1_000_000
MAX_EXTENSIONS = 20
MAX_EXTENSION_PARAMETER_CHARS = 8_000
_URL_ADAPTER = TypeAdapter(AnyUrl)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TextExpectation(StrictModel):
    contains: list[NonEmptyText] = Field(default_factory=list, max_length=100)
    not_contains: list[NonEmptyText] = Field(default_factory=list, max_length=100)
    matches: list[NonEmptyText] = Field(default_factory=list, max_length=100)
    equals: str | None = None
    case_sensitive: bool = True

    @field_validator("contains", "not_contains", "matches", mode="before")
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
    equals: JsonValue
    path: Annotated[str, Field(max_length=1_000)] = ""
    source: Literal["message", "artifact"] | None = None
    artifact_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    media_type: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @field_validator("path")
    @classmethod
    def validate_json_pointer(cls, path: str) -> str:
        if not re.fullmatch(r"(?:/(?:[^~]|~[01])*)*", path):
            raise ValueError("path must be an RFC 6901 JSON Pointer")
        return path

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.source == "message" and self.artifact_name is not None:
            raise ValueError("artifact_name cannot be used with source: message")
        return self


class Expectation(StrictModel):
    state: str | None = Field(default=None, min_length=1, max_length=64)
    text: TextExpectation | None = None
    data: list[DataExpectation] = Field(default_factory=list, max_length=100)
    max_seconds: float | None = Field(default=None, gt=0, le=600)

    @field_validator("data", mode="before")
    @classmethod
    def accept_single_data_expectation(cls, value: object) -> object:
        return [value] if isinstance(value, (dict, DataExpectation)) else value


class Turn(StrictModel):
    message: NonEmptyText | None = None
    data: list[JsonValue] = Field(default_factory=list, max_length=100)
    expect: Expectation = Field(default_factory=Expectation)

    @field_validator("data", mode="before")
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
    name: Annotated[str, Field(min_length=1, max_length=200)]
    message: NonEmptyText | None = None
    data: list[JsonValue] = Field(default_factory=list, max_length=100)
    turns: Annotated[list[Turn], Field(min_length=1, max_length=50)] | None = None
    expect: Expectation = Field(default_factory=Expectation)
    trials: int = Field(default=1, ge=1, le=100)
    pass_rate: float = Field(default=1.0, gt=0, le=1)

    @field_validator("data", mode="before")
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
    url: HttpUrl
    timeout: float = Field(default=30, gt=0, le=600)
    transport: Literal["auto", "JSONRPC", "HTTP+JSON", "GRPC"] = "auto"
    grpc_tls: bool = True
    allow_cross_origin_interfaces: bool = False
    card_path: str | None = Field(default=None, min_length=1, max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)
    extensions: list[ExtensionUri] = Field(default_factory=list, max_length=MAX_EXTENSIONS)

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
    version: Literal[1]
    agent: AgentConfig
    scenarios: Annotated[list[Scenario], Field(min_length=1, max_length=1_000)]

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
