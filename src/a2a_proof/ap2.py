from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from stat import S_ISREG
from typing import Any

from a2a_proof.models import AP2MandateExpectation, DataPartResult, ProofConfig

AP2_SDK_COMMIT = "b4587ac1d055888a73b4b21750973cffba961793"
AP2_INSTALL_REQUIREMENT = (
    "ap2 @ git+https://github.com/google-agentic-commerce/AP2.git@" + AP2_SDK_COMMIT
)
MAX_JWK_BYTES = 16_384
MAX_AP2_ERROR_CHARS = 500
REDACTED_AP2_MANDATE = "[REDACTED: AP2 mandate]"
_PRIVATE_JWK_FIELDS = {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}


class AP2Error(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _SDK:
    mandate_client: type[Any]
    checkout_chain: type[Any]
    payment_chain: type[Any]
    jwk: type[Any]
    compute_sha256_b64url: Any


class _Missing:
    pass


_MISSING = _Missing()


def has_ap2_expectations(config: ProofConfig) -> bool:
    return bool(_configured_expectations(config))


def validate_config_ap2(config: ProofConfig) -> None:
    paths = {expectation.trusted_root_jwk for expectation in _configured_expectations(config)}
    for path in sorted(paths):
        _read_public_jwk(path, config.contract_dir)


def ensure_ap2_sdk(config: ProofConfig) -> None:
    if not has_ap2_expectations(config):
        return
    sdk = _load_sdk()
    paths = {expectation.trusted_root_jwk for expectation in _configured_expectations(config)}
    for path in sorted(paths):
        value = _read_public_jwk(path, config.contract_dir)
        try:
            sdk.jwk.from_json(json.dumps(value, separators=(",", ":")))
        except Exception as error:
            raise AP2Error(f"trusted_root_jwk {path!r} is not a valid public P-256 JWK") from error


def _configured_expectations(config: ProofConfig) -> list[AP2MandateExpectation]:
    return [
        expectation
        for scenario in config.scenarios
        for turn in scenario.resolved_turns()
        for expectation in turn.expect.ap2
    ]


def evaluate_ap2(
    expectations: list[AP2MandateExpectation],
    parts: tuple[DataPartResult, ...],
    contract_dir: Path,
) -> list[str]:
    if not expectations:
        return []
    sdk = _load_sdk()
    failures: list[str] = []
    for expectation in expectations:
        candidates = [part for part in parts if _matches_location(expectation, part)]
        tokens = [
            value
            for part in candidates
            if not isinstance(
                value := _resolve_pointer(part.value, expectation.resolved_path),
                _Missing,
            )
        ]
        if not candidates:
            failures.append(f"no AP2 data matched {_location(expectation)}")
            continue
        if not tokens:
            failures.append(f"AP2 mandate path {expectation.resolved_path!r} was not found")
            continue
        failure = _verify_any(expectation, tokens, contract_dir, sdk)
        if failure is not None:
            failures.append(failure)
    return failures


def redact_ap2(
    expectations: list[AP2MandateExpectation],
    parts: tuple[DataPartResult, ...],
) -> tuple[DataPartResult, ...]:
    redacted: list[DataPartResult] = []
    for part in parts:
        value = part.value
        changed = False
        for expectation in expectations:
            if not _matches_location(expectation, part):
                continue
            replaced, value = _replace_pointer(
                value,
                expectation.resolved_path,
                REDACTED_AP2_MANDATE,
            )
            changed = changed or replaced
        redacted.append(part.model_copy(update={"value": value}) if changed else part)
    return tuple(redacted)


def _verify_any(
    expectation: AP2MandateExpectation,
    values: list[Any],
    contract_dir: Path,
    sdk: _SDK,
) -> str | None:
    errors: list[str] = []
    for value in values:
        if not isinstance(value, str):
            errors.append("mandate value is not a string")
            continue
        try:
            _verify(expectation, value, contract_dir, sdk)
            return None
        except Exception as error:
            errors.append(_bounded_error(error))
    detail = errors[0] if errors else "no mandate value was available"
    return f"AP2 {expectation.type} mandate verification failed: {detail}"


def _verify(
    expectation: AP2MandateExpectation,
    token: str,
    contract_dir: Path,
    sdk: _SDK,
) -> None:
    public_jwk = _read_public_jwk(expectation.trusted_root_jwk, contract_dir)
    root_key = sdk.jwk.from_json(json.dumps(public_jwk, separators=(",", ":")))
    try:
        payloads = sdk.mandate_client().verify(
            token=token,
            key_or_provider=lambda _token: root_key,
            expected_aud=expectation.audience,
            expected_nonce=expectation.nonce,
        )
    except Exception as error:
        raise AP2Error(
            "signed chain failed signature, binding, audience, nonce, or validity checks"
        ) from error
    if not isinstance(payloads, list):
        raise AP2Error("expected a signed mandate chain")

    if expectation.type == "payment":
        try:
            chain = sdk.payment_chain.parse(payloads)
        except Exception as error:
            raise AP2Error("payment mandate payload is invalid") from error
        try:
            violations = chain.verify(
                expected_transaction_id=expectation.transaction_id,
                expected_open_checkout_hash=expectation.open_checkout_hash,
            )
        except Exception as error:
            raise AP2Error("payment mandate constraints could not be evaluated") from error
    else:
        try:
            chain = sdk.checkout_chain.parse(payloads)
        except Exception as error:
            raise AP2Error("checkout mandate payload is invalid") from error
        checkout_jwt = chain.closed_mandate.checkout_jwt
        try:
            violations = chain.verify(
                expected_checkout_hash=expectation.checkout_hash,
                checkout_jwt=checkout_jwt,
            )
            actual_hash = sdk.compute_sha256_b64url(checkout_jwt)
        except Exception as error:
            raise AP2Error("checkout mandate constraints could not be evaluated") from error
        if actual_hash != chain.closed_mandate.checkout_hash:
            violations.append("Checkout checkout_hash does not bind the signed checkout_jwt")
    if violations:
        raise AP2Error("; ".join(violations))


def _load_sdk() -> _SDK:
    try:
        mandate = import_module("ap2.sdk.mandate")
        checkout = import_module("ap2.sdk.checkout_mandate_chain")
        payment = import_module("ap2.sdk.payment_mandate_chain")
        utils = import_module("ap2.sdk.utils")
        jwk = import_module("jwcrypto.jwk")
        mandate.__dict__["LOG_FILE_PATH"] = os.devnull
        return _SDK(
            mandate_client=mandate.MandateClient,
            checkout_chain=checkout.CheckoutMandateChain,
            payment_chain=payment.PaymentMandateChain,
            jwk=jwk.JWK,
            compute_sha256_b64url=utils.compute_sha256_b64url,
        )
    except (AttributeError, ImportError) as error:
        raise AP2Error(
            "AP2 assertions require the official AP2 v0.2.0 SDK; install "
            f"{AP2_INSTALL_REQUIREMENT!r} alongside a2a-proof"
        ) from error


def _read_public_jwk(relative_path: str, contract_dir: Path) -> dict[str, Any]:
    root = contract_dir.resolve()
    try:
        path = (root / relative_path).resolve()
        stat = path.stat()
    except OSError as error:
        raise AP2Error(f"cannot access trusted_root_jwk {relative_path!r}: {error}") from error
    if not path.is_relative_to(root):
        raise AP2Error(f"trusted_root_jwk {relative_path!r} escapes the contract directory")
    if not S_ISREG(stat.st_mode):
        raise AP2Error(f"trusted_root_jwk {relative_path!r} is not a regular file")
    if stat.st_size > MAX_JWK_BYTES:
        raise AP2Error(f"trusted_root_jwk {relative_path!r} exceeds {MAX_JWK_BYTES} bytes")
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_JWK_BYTES + 1)
    except OSError as error:
        raise AP2Error(f"cannot read trusted_root_jwk {relative_path!r}: {error}") from error
    if len(content) > MAX_JWK_BYTES:
        raise AP2Error(f"trusted_root_jwk {relative_path!r} exceeds {MAX_JWK_BYTES} bytes")
    try:
        value = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AP2Error(f"trusted_root_jwk {relative_path!r} is not valid JSON") from error
    if not isinstance(value, dict):
        raise AP2Error(f"trusted_root_jwk {relative_path!r} must contain a JSON object")
    if _PRIVATE_JWK_FIELDS.intersection(value):
        raise AP2Error(f"trusted_root_jwk {relative_path!r} must contain a public key only")
    required = {"kty": "EC", "crv": "P-256"}
    if any(value.get(name) != expected for name, expected in required.items()) or any(
        not isinstance(value.get(name), str) or not value[name] for name in ("x", "y")
    ):
        raise AP2Error(f"trusted_root_jwk {relative_path!r} must be a public P-256 JWK")
    return value


def _matches_location(
    expectation: AP2MandateExpectation,
    part: DataPartResult,
) -> bool:
    if expectation.source is not None and part.source != expectation.source:
        return False
    if expectation.artifact_name is not None and part.artifact_name != expectation.artifact_name:
        return False
    return expectation.media_type is None or (
        part.media_type is not None
        and part.media_type.casefold() == expectation.media_type.casefold()
    )


def _location(expectation: AP2MandateExpectation) -> str:
    filters = [
        f"{label} {value!r}"
        for label, value in (
            ("source", expectation.source),
            ("artifact", expectation.artifact_name),
            ("media type", expectation.media_type),
        )
        if value is not None
    ]
    return ", ".join(filters) or "the expectation"


def _resolve_pointer(value: Any, pointer: str) -> Any:
    current = value
    for encoded in pointer.split("/")[1:]:
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
            continue
        if isinstance(current, list) and (
            token == "0" or (token.isdigit() and not token.startswith("0"))
        ):
            index = int(token)
            if index < len(current):
                current = current[index]
                continue
        return _MISSING
    return current


def _replace_pointer(value: Any, pointer: str, replacement: Any) -> tuple[bool, Any]:
    tokens = [encoded.replace("~1", "/").replace("~0", "~") for encoded in pointer.split("/")[1:]]
    return _replace_tokens(value, tokens, replacement)


def _replace_tokens(value: Any, tokens: list[str], replacement: Any) -> tuple[bool, Any]:
    if not tokens:
        return True, replacement
    token, *remaining = tokens
    if isinstance(value, dict) and token in value:
        changed, nested = _replace_tokens(value[token], remaining, replacement)
        if changed:
            copied = dict(value)
            copied[token] = nested
            return True, copied
    elif isinstance(value, list) and (
        token == "0" or (token.isdigit() and not token.startswith("0"))
    ):
        index = int(token)
        if index < len(value):
            changed, nested = _replace_tokens(value[index], remaining, replacement)
            if changed:
                copied = list(value)
                copied[index] = nested
                return True, copied
    return False, value


def _bounded_error(error: Exception) -> str:
    message = " ".join(str(error).split()) or type(error).__name__
    if len(message) <= MAX_AP2_ERROR_CHARS:
        return message
    return f"{message[: MAX_AP2_ERROR_CHARS - 1]}…"
