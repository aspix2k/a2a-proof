from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
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
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

NonEmptyText = Annotated[str, Field(min_length=1, max_length=100_000)]
StateName = Annotated[str, Field(min_length=1, max_length=64)]
ExtensionUri = Annotated[str, Field(min_length=1, max_length=1_000)]
EnvironmentName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=200)]
AP2AssertionId = Annotated[
    str,
    Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$", description="Local AP2 assertion ID."),
]
MAX_INPUT_DATA_BYTES = 1_000_000
MAX_INPUT_FILES = 20
MAX_EXTENSIONS = 20
MAX_EXTENSION_PARAMETER_CHARS = 8_000
MAX_JSON_SCHEMA_BYTES = 100_000
MAX_JSON_SCHEMA_DEPTH = 50
_URL_ADAPTER = TypeAdapter(AnyUrl)
_DATA_PREDICATES = {"equals", "exists", "matches", "gt", "gte", "lt", "lte", "json_schema"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ContainsExpectation(StrictModel):
    contains: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=200)]],
        Field(min_length=1, max_length=100, description="Required values."),
    ]

    @field_validator("contains", mode="before", json_schema_input_type=str | list[str])
    @classmethod
    def accept_single_value(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value


class AgentCapabilitiesExpectation(StrictModel):
    streaming: bool | SkipJsonSchema[None] = None
    push_notifications: bool | SkipJsonSchema[None] = None
    extended_agent_card: bool | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def require_capability(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("capabilities must define at least one assertion")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("capability assertions cannot be null")
        return self


class AgentCardExpectation(StrictModel):
    skills: ContainsExpectation | SkipJsonSchema[None] = Field(
        default=None,
        description="Required Agent Card skill IDs.",
    )
    capabilities: AgentCapabilitiesExpectation | SkipJsonSchema[None] = None
    input_modes: ContainsExpectation | SkipJsonSchema[None] = None
    output_modes: ContainsExpectation | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def require_assertion(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, name) is None for name in self.model_fields_set
        ):
            raise ValueError("card must define at least one assertion")
        return self


class FileInput(StrictModel):
    path: Annotated[
        str,
        Field(min_length=1, max_length=1_000, description="Path relative to the contract file."),
    ]
    media_type: (
        Annotated[str, Field(min_length=1, max_length=200, description="A2A media type.")] | None
    ) = None

    @model_validator(mode="before")
    @classmethod
    def accept_path(cls, value: object) -> object:
        return {"path": value} if isinstance(value, str) else value

    @field_validator("path")
    @classmethod
    def require_relative_path(cls, path: str) -> str:
        if "\0" in path:
            raise ValueError("file path contains a null byte")
        if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
            raise ValueError("file path must be relative to the contract file")
        return path


class FileExpectation(StrictModel):
    count: int = Field(default=1, ge=0, le=1_000, description="Required matching part count.")
    source: Literal["message", "artifact"] | None = None
    artifact_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    filename: Annotated[str, Field(min_length=1, max_length=1_000)] | None = None
    media_type: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    kind: Literal["raw", "url"] | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.source == "message" and self.artifact_name is not None:
            raise ValueError("artifact_name cannot be used with source: message")
        return self


class StateSequenceExpectation(StrictModel):
    equals: (
        Annotated[list[StateName], Field(min_length=1, max_length=100)] | SkipJsonSchema[None]
    ) = None
    contains_in_order: (
        Annotated[list[StateName], Field(min_length=1, max_length=100)] | SkipJsonSchema[None]
    ) = None

    @model_validator(mode="after")
    def require_one_assertion(self) -> Self:
        configured = self.model_fields_set & {"equals", "contains_in_order"}
        if len(configured) != 1:
            raise ValueError("states must define exactly one of equals or contains_in_order")
        if getattr(self, configured.pop()) is None:
            raise ValueError("state sequence assertion cannot be null")
        return self


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


class TextInvariant(StrictModel):
    not_contains: list[NonEmptyText] = Field(
        default_factory=list,
        max_length=100,
        description="Values forbidden in every response turn.",
    )
    not_contains_env: list[EnvironmentName] = Field(
        default_factory=list,
        max_length=100,
        description="Environment variables whose values are forbidden in every response turn.",
    )
    case_sensitive: bool = Field(default=True, description="Apply case-sensitive checks.")

    @field_validator(
        "not_contains",
        "not_contains_env",
        mode="before",
        json_schema_input_type=str | list[str],
    )
    @classmethod
    def accept_single_value(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_value(self) -> Self:
        if not self.not_contains and not self.not_contains_env:
            raise ValueError("text invariant must define not_contains or not_contains_env")
        return self


class Invariants(StrictModel):
    text: TextInvariant


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


class AP2MandateExpectation(StrictModel):
    id: AP2AssertionId | None = Field(
        default=None,
        description="ID used by a receipt assertion in the same turn.",
    )
    type: Literal["checkout", "payment"]
    trusted_root_jwk: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1_000,
            description="Public JWK path relative to the contract file.",
        ),
    ]
    audience: Annotated[str, Field(min_length=1, max_length=1_000)]
    nonce: Annotated[str, Field(min_length=1, max_length=1_000)]
    path: Annotated[str, Field(max_length=1_000)] | None = Field(
        default=None,
        description="RFC 6901 pointer to the mandate token; inferred from type by default.",
    )
    source: Literal["message", "artifact"] | None = None
    artifact_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    media_type: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    transaction_id: Annotated[str, Field(min_length=1, max_length=1_000)] | None = None
    open_checkout_hash: Annotated[str, Field(min_length=1, max_length=1_000)] | None = None
    checkout_hash: Annotated[str, Field(min_length=1, max_length=1_000)] | None = None

    @field_validator("trusted_root_jwk")
    @classmethod
    def require_relative_key_path(cls, path: str) -> str:
        if "\0" in path:
            raise ValueError("trusted_root_jwk contains a null byte")
        if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
            raise ValueError("trusted_root_jwk must be relative to the contract file")
        return path

    @field_validator("path")
    @classmethod
    def validate_json_pointer(cls, path: str | None) -> str | None:
        if path is not None and not re.fullmatch(r"(?:/(?:[^~]|~[01])*)*", path):
            raise ValueError("path must be an RFC 6901 JSON Pointer")
        return path

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.source == "message" and self.artifact_name is not None:
            raise ValueError("artifact_name cannot be used with source: message")
        if self.type == "checkout" and (
            self.transaction_id is not None or self.open_checkout_hash is not None
        ):
            raise ValueError("transaction_id and open_checkout_hash require an AP2 payment mandate")
        if self.type == "payment" and self.checkout_hash is not None:
            raise ValueError("checkout_hash requires an AP2 checkout mandate")
        return self

    @property
    def resolved_path(self) -> str:
        if self.path is not None:
            return self.path
        suffix = "Checkout" if self.type == "checkout" else "Payment"
        return f"/ap2.mandates.{suffix}MandateSdJwt"


class AP2ReceiptExpectation(StrictModel):
    kind: Literal["receipt"]
    type: Literal["checkout", "payment"]
    binds_to: AP2AssertionId = Field(description="Verified mandate assertion ID.")
    trusted_issuer_jwk: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1_000,
            description="Receipt issuer public JWK path relative to the contract file.",
        ),
    ]
    path: Annotated[
        str,
        Field(max_length=1_000, description="RFC 6901 pointer to the signed receipt JWT."),
    ]
    source: Literal["message", "artifact"] | None = None
    artifact_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    media_type: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    issuer: Annotated[str, Field(min_length=1, max_length=1_000)] | None = None
    status: Literal["Success", "Error"] | None = None
    payment_id: Annotated[str, Field(min_length=1, max_length=1_000)] | None = None
    order_id: Annotated[str, Field(min_length=1, max_length=1_000)] | None = None
    error: Annotated[str, Field(min_length=1, max_length=1_000)] | None = None

    @field_validator("trusted_issuer_jwk")
    @classmethod
    def require_relative_key_path(cls, path: str) -> str:
        if "\0" in path:
            raise ValueError("trusted_issuer_jwk contains a null byte")
        if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
            raise ValueError("trusted_issuer_jwk must be relative to the contract file")
        return path

    @field_validator("path")
    @classmethod
    def validate_json_pointer(cls, path: str) -> str:
        if not re.fullmatch(r"(?:/(?:[^~]|~[01])*)*", path):
            raise ValueError("path must be an RFC 6901 JSON Pointer")
        return path

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.source == "message" and self.artifact_name is not None:
            raise ValueError("artifact_name cannot be used with source: message")
        if self.type == "checkout" and self.payment_id is not None:
            raise ValueError("payment_id requires an AP2 payment receipt")
        if self.type == "payment" and self.order_id is not None:
            raise ValueError("order_id requires an AP2 checkout receipt")
        if self.status == "Success" and self.error is not None:
            raise ValueError("error cannot be asserted for a successful AP2 receipt")
        return self


AP2Expectation = AP2MandateExpectation | AP2ReceiptExpectation


class Expectation(StrictModel):
    state: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Required terminal A2A state.",
    )
    text: TextExpectation | None = Field(default=None, description="Response text checks.")
    states: StateSequenceExpectation | None = Field(
        default=None,
        description="Observed A2A state sequence checks.",
    )
    data: list[DataExpectation] = Field(
        default_factory=list,
        max_length=100,
        description="Structured response checks.",
    )
    files: list[FileExpectation] = Field(
        default_factory=list,
        max_length=100,
        description="File-part metadata checks.",
    )
    ap2: list[AP2Expectation] = Field(
        default_factory=list,
        max_length=100,
        description="Signed AP2 mandate and receipt checks.",
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

    @field_validator(
        "files",
        mode="before",
        json_schema_input_type=FileExpectation | list[FileExpectation],
    )
    @classmethod
    def accept_single_file_expectation(cls, value: object) -> object:
        return [value] if isinstance(value, (dict, FileExpectation)) else value

    @field_validator(
        "ap2",
        mode="before",
        json_schema_input_type=AP2Expectation | list[AP2Expectation],
    )
    @classmethod
    def accept_single_ap2_expectation(cls, value: object) -> object:
        return (
            [value]
            if isinstance(value, (dict, AP2MandateExpectation, AP2ReceiptExpectation))
            else value
        )

    @model_validator(mode="after")
    def validate_ap2_bindings(self) -> Self:
        mandates = {
            expectation.id: expectation
            for expectation in self.ap2
            if isinstance(expectation, AP2MandateExpectation) and expectation.id is not None
        }
        mandate_ids = [
            expectation.id
            for expectation in self.ap2
            if isinstance(expectation, AP2MandateExpectation) and expectation.id is not None
        ]
        if len(mandates) != len(mandate_ids):
            raise ValueError("AP2 mandate assertion IDs must be unique within a turn")
        for expectation in self.ap2:
            if not isinstance(expectation, AP2ReceiptExpectation):
                continue
            mandate = mandates.get(expectation.binds_to)
            if mandate is None:
                raise ValueError(
                    f"AP2 receipt binds_to {expectation.binds_to!r} must name a mandate "
                    "assertion in the same turn"
                )
            if mandate.type != expectation.type:
                raise ValueError(
                    f"AP2 {expectation.type} receipt cannot bind to {mandate.type} mandate "
                    f"{expectation.binds_to!r}"
                )
        return self


class Turn(StrictModel):
    message: NonEmptyText | None = Field(default=None, description="Text sent to the agent.")
    data: list[JsonValue] = Field(
        default_factory=list,
        max_length=100,
        description="Structured data parts sent to the agent.",
    )
    files: list[FileInput] = Field(
        default_factory=list,
        max_length=MAX_INPUT_FILES,
        description="Local files sent as inline A2A raw parts.",
    )
    action: Literal["cancel", "get_task"] | None = Field(
        default=None,
        description="Task lifecycle operation using the preceding task ID.",
    )
    return_immediately: bool = Field(
        default=False,
        description="Ask the agent to return the initial task state without waiting.",
    )
    history_length: int | None = Field(
        default=None,
        ge=0,
        le=1_000,
        description="Task history entries requested by a get_task action.",
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
    def validate_operation(self) -> Self:
        has_content = self.message is not None or bool(self.data) or bool(self.files)
        if self.action is None and not has_content:
            raise ValueError("turn must contain message, data, or files")
        if self.action is not None and has_content:
            raise ValueError("turn cannot combine an action with message, data, or files")
        if self.action is not None and "return_immediately" in self.model_fields_set:
            raise ValueError("return_immediately can only be used with an input turn")
        if "history_length" in self.model_fields_set and self.action != "get_task":
            raise ValueError("history_length can only be used with action: get_task")
        return self


class LatencyExpectation(StrictModel):
    p50_seconds: (
        Annotated[
            float,
            Field(gt=0, le=600, description="Maximum median trial duration."),
        ]
        | SkipJsonSchema[None]
    ) = None
    p95_seconds: (
        Annotated[
            float,
            Field(gt=0, le=600, description="Maximum 95th-percentile trial duration."),
        ]
        | SkipJsonSchema[None]
    ) = None

    @model_validator(mode="after")
    def require_percentile(self) -> Self:
        configured = self.model_fields_set & {"p50_seconds", "p95_seconds"}
        if not configured:
            raise ValueError("latency must define p50_seconds or p95_seconds")
        if any(getattr(self, name) is None for name in configured):
            raise ValueError("latency percentile cannot be null")
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
    files: list[FileInput] = Field(
        default_factory=list,
        max_length=MAX_INPUT_FILES,
        description="Single-turn local file input.",
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
    latency: LatencyExpectation | None = Field(
        default=None,
        description="Aggregate duration checks across completed trials.",
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
        if self.turns is not None and (self.message is not None or self.data or self.files):
            raise ValueError("set exactly one of single-turn input or turns")
        if self.turns is None and self.message is None and not self.data and not self.files:
            raise ValueError("scenario must contain message, data, files, or turns")
        if self.turns is not None and "expect" in self.model_fields_set:
            raise ValueError("put expect on each turn when using turns")
        if self.turns is not None and self.turns[0].action is not None:
            raise ValueError("the first turn cannot be a task action")
        return self

    def resolved_turns(self) -> list[Turn]:
        if self.turns is not None:
            return self.turns
        return [Turn(message=self.message, data=self.data, files=self.files, expect=self.expect)]


class ScenarioDefaults(StrictModel):
    trials: int = Field(default=1, ge=1, le=100, description="Default independent repetitions.")
    pass_rate: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description="Default minimum successful trial fraction.",
    )


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
    card: AgentCardExpectation | None = Field(
        default=None,
        description="Agent Card preflight checks.",
    )
    invariants: Invariants | None = Field(
        default=None,
        description="Checks applied to every response turn.",
    )
    defaults: ScenarioDefaults = Field(
        default_factory=ScenarioDefaults,
        description="Defaults applied to scenarios that omit these fields.",
    )
    scenarios: Annotated[
        list[Scenario],
        Field(min_length=1, max_length=1_000, description="Behavior contracts to run."),
    ]
    _contract_dir: Path = PrivateAttr(default_factory=lambda: Path.cwd().resolve())
    _contract_sha256: str | None = PrivateAttr(default=None)
    _redaction_values: tuple[str, ...] = PrivateAttr(default=())

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

    @property
    def contract_dir(self) -> Path:
        return self._contract_dir

    def bind_contract_dir(self, path: Path) -> None:
        self._contract_dir = path.resolve()

    @property
    def contract_sha256(self) -> str | None:
        return self._contract_sha256

    def bind_contract_sha256(self, digest: str) -> None:
        self._contract_sha256 = digest

    @property
    def redaction_values(self) -> tuple[str, ...]:
        return self._redaction_values

    def bind_redaction_values(self, values: Sequence[str]) -> None:
        self._redaction_values = tuple(dict.fromkeys(value for value in values if value))

    def resolved_scenarios(self) -> list[Scenario]:
        resolved: list[Scenario] = []
        for scenario in self.scenarios:
            updates = {
                name: getattr(self.defaults, name)
                for name in ("trials", "pass_rate")
                if name not in scenario.model_fields_set
            }
            resolved.append(scenario.model_copy(update=updates) if updates else scenario)
        return resolved


class DataPartResult(StrictModel):
    source: Literal["message", "artifact"]
    value: JsonValue
    media_type: str | None = None
    artifact_id: str | None = None
    artifact_name: str | None = None


class FilePartResult(StrictModel):
    source: Literal["message", "artifact"]
    kind: Literal["raw", "url"]
    filename: Annotated[str, Field(max_length=1_000)] | None = None
    media_type: Annotated[str, Field(max_length=200)] | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    artifact_id: Annotated[str, Field(max_length=1_000)] | None = None
    artifact_name: Annotated[str, Field(max_length=200)] | None = None


class CardResult(StrictModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)


class TurnResult(StrictModel):
    index: int
    passed: bool
    state: str
    states: list[str] = Field(default_factory=list)
    duration_ms: int
    first_event_ms: int | None = None
    text: str
    data: list[DataPartResult] = Field(default_factory=list)
    files: list[FilePartResult] = Field(default_factory=list)
    response_redacted: bool = False
    failures: list[str] = Field(default_factory=list)


class TrialResult(StrictModel):
    index: int
    passed: bool
    duration_ms: int
    turns: list[TurnResult] = Field(default_factory=list)
    error: str | None = None


class LatencyResult(StrictModel):
    passed: bool
    samples: int
    p50_ms: int | None = None
    p95_ms: int | None = None
    failures: list[str] = Field(default_factory=list)


class ScenarioResult(StrictModel):
    name: str
    passed: bool
    passed_trials: int
    required_trials: int
    trials: list[TrialResult]
    latency: LatencyResult | None = None


class SuiteResult(StrictModel):
    passed: bool
    duration_ms: int
    card: CardResult | None = None
    scenarios: list[ScenarioResult]
    agent_card_sha256: str | None = Field(default=None, exclude=True)


class DiffCheck(StrictModel):
    name: str
    baseline: Literal["passed", "failed", "not_run"]
    candidate: Literal["passed", "failed", "not_run"]
    change: Literal["unchanged", "regression", "improvement", "changed"]


class DiffResult(StrictModel):
    passed: bool
    baseline: SuiteResult
    candidate: SuiteResult
    checks: list[DiffCheck]


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
