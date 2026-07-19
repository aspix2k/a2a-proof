from __future__ import annotations

from typing import Annotated, Literal, Self, cast

import regex
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

NonEmptyText = Annotated[str, Field(min_length=1, max_length=100_000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class Expectation(StrictModel):
    state: str | None = Field(default=None, min_length=1, max_length=64)
    text: TextExpectation | None = None
    max_seconds: float | None = Field(default=None, gt=0, le=600)


class Turn(StrictModel):
    message: NonEmptyText
    expect: Expectation = Field(default_factory=Expectation)


class Scenario(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    message: NonEmptyText | None = None
    turns: Annotated[list[Turn], Field(min_length=1, max_length=50)] | None = None
    expect: Expectation = Field(default_factory=Expectation)
    trials: int = Field(default=1, ge=1, le=100)
    pass_rate: float = Field(default=1.0, gt=0, le=1)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if (self.message is None) == (self.turns is None):
            raise ValueError("set exactly one of message or turns")
        if self.turns is not None and "expect" in self.model_fields_set:
            raise ValueError("put expect on each turn when using turns")
        return self

    def resolved_turns(self) -> list[Turn]:
        if self.turns is not None:
            return self.turns
        return [Turn(message=cast(str, self.message), expect=self.expect)]


class AgentConfig(StrictModel):
    url: HttpUrl
    timeout: float = Field(default=30, gt=0, le=600)
    transport: Literal["auto", "JSONRPC", "HTTP+JSON", "GRPC"] = "auto"
    grpc_tls: bool = True
    allow_cross_origin_interfaces: bool = False
    card_path: str | None = Field(default=None, min_length=1, max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)

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
        return headers


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


class TurnResult(StrictModel):
    index: int
    passed: bool
    state: str
    duration_ms: int
    text: str
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
