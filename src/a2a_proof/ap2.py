from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from stat import S_ISREG
from typing import Any, BinaryIO, Literal

from a2a_proof.models import AP2MandateExpectation, DataPartResult, ProofConfig

AP2_SDK_COMMIT = "b4587ac1d055888a73b4b21750973cffba961793"
AP2_INSTALL_REQUIREMENT = (
    "ap2 @ git+https://github.com/google-agentic-commerce/AP2.git@" + AP2_SDK_COMMIT
)
MAX_JWK_BYTES = 16_384
MAX_AP2_TOKEN_BYTES = 1_048_576
MAX_AP2_ERROR_CHARS = 500
MAX_AP2_DETAIL_CHARS = 2_000
MAX_AP2_EXPECTED_VALUE_CHARS = 1_000
REDACTED_AP2_MANDATE = "[REDACTED: AP2 mandate]"
_PRIVATE_JWK_FIELDS = {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}


class AP2Error(RuntimeError):
    pass


class AP2VerificationError(AP2Error):
    pass


@dataclass(frozen=True, slots=True)
class AP2Inspection:
    type: Literal["checkout", "payment"]
    chain_length: int
    audience: str
    checks: tuple[str, ...]
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": True,
            "type": self.type,
            "chain_length": self.chain_length,
            "audience": self.audience,
            "checks": list(self.checks),
            "details": self.details,
        }


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


def read_ap2_token(stream: BinaryIO) -> str:
    content = stream.read(MAX_AP2_TOKEN_BYTES + 1)
    if len(content) > MAX_AP2_TOKEN_BYTES:
        raise AP2Error(f"AP2 mandate token exceeds {MAX_AP2_TOKEN_BYTES} bytes")
    try:
        token = content.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise AP2Error("AP2 mandate token must be ASCII") from error
    _validate_token(token)
    return token


def _validate_token(token: str) -> None:
    if not token:
        raise AP2Error("AP2 mandate token is empty")
    try:
        size = len(token.encode("ascii"))
    except UnicodeEncodeError as error:
        raise AP2Error("AP2 mandate token must be ASCII") from error
    if size > MAX_AP2_TOKEN_BYTES:
        raise AP2Error(f"AP2 mandate token exceeds {MAX_AP2_TOKEN_BYTES} bytes")
    if any(character.isspace() for character in token):
        raise AP2Error("AP2 mandate token contains whitespace")


def inspect_ap2(
    token: str,
    trusted_root_jwk: Path,
    audience: str,
    nonce: str,
    *,
    mandate_type: Literal["auto", "checkout", "payment"] = "auto",
    transaction_id: str | None = None,
    open_checkout_hash: str | None = None,
    checkout_hash: str | None = None,
) -> AP2Inspection:
    _validate_token(token)
    for label, value in (
        ("audience", audience),
        ("nonce", nonce),
        ("transaction ID", transaction_id),
        ("open checkout hash", open_checkout_hash),
        ("checkout hash", checkout_hash),
    ):
        if value is not None and not 1 <= len(value) <= MAX_AP2_EXPECTED_VALUE_CHARS:
            raise AP2Error(
                f"{label} must contain between 1 and {MAX_AP2_EXPECTED_VALUE_CHARS} characters"
            )
    sdk = _load_sdk()
    public_jwk = _read_public_jwk_file(trusted_root_jwk)
    root_key = _parse_public_jwk(public_jwk, f"trusted root {trusted_root_jwk}", sdk)
    payloads = _verify_signed_chain(token, root_key, audience, nonce, sdk)
    resolved_type = _resolve_mandate_type(payloads, mandate_type)
    if resolved_type == "checkout" and (
        transaction_id is not None or open_checkout_hash is not None
    ):
        raise AP2Error("--transaction-id and --open-checkout-hash require a payment mandate")
    if resolved_type == "payment" and checkout_hash is not None:
        raise AP2Error("--checkout-hash requires a checkout mandate")
    chain = _verify_typed_chain(
        resolved_type,
        payloads,
        sdk,
        transaction_id=transaction_id,
        open_checkout_hash=open_checkout_hash,
        checkout_hash=checkout_hash,
    )
    try:
        return _inspection_result(resolved_type, payloads, chain, audience)
    except Exception as error:
        raise AP2Error("verified AP2 mandate could not be summarized") from error


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
        _parse_public_jwk(value, f"trusted_root_jwk {path!r}", sdk)


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
    root_key = _parse_public_jwk(
        public_jwk,
        f"trusted_root_jwk {expectation.trusted_root_jwk!r}",
        sdk,
    )
    payloads = _verify_signed_chain(
        token,
        root_key,
        expectation.audience,
        expectation.nonce,
        sdk,
    )
    _verify_typed_chain(
        expectation.type,
        payloads,
        sdk,
        transaction_id=expectation.transaction_id,
        open_checkout_hash=expectation.open_checkout_hash,
        checkout_hash=expectation.checkout_hash,
    )


def _verify_signed_chain(
    token: str,
    root_key: Any,
    audience: str,
    nonce: str,
    sdk: _SDK,
) -> list[dict[str, Any]]:
    try:
        payloads = sdk.mandate_client().verify(
            token=token,
            key_or_provider=lambda _token: root_key,
            expected_aud=audience,
            expected_nonce=nonce,
        )
    except Exception as error:
        raise AP2VerificationError(
            "signed chain failed signature, binding, audience, nonce, or validity checks"
        ) from error
    if (
        not isinstance(payloads, list)
        or not payloads
        or not all(isinstance(payload, dict) for payload in payloads)
    ):
        raise AP2VerificationError("expected a signed mandate chain")
    return payloads


def _verify_typed_chain(
    mandate_type: Literal["checkout", "payment"],
    payloads: list[dict[str, Any]],
    sdk: _SDK,
    *,
    transaction_id: str | None,
    open_checkout_hash: str | None,
    checkout_hash: str | None,
) -> Any:
    if mandate_type == "payment":
        try:
            chain = sdk.payment_chain.parse(payloads)
        except Exception as error:
            raise AP2VerificationError("payment mandate payload is invalid") from error
        try:
            violations = chain.verify(
                expected_transaction_id=transaction_id,
                expected_open_checkout_hash=open_checkout_hash,
            )
        except Exception as error:
            raise AP2VerificationError(
                "payment mandate constraints could not be evaluated"
            ) from error
    else:
        try:
            chain = sdk.checkout_chain.parse(payloads)
        except Exception as error:
            raise AP2VerificationError("checkout mandate payload is invalid") from error
        checkout_jwt = chain.closed_mandate.checkout_jwt
        try:
            violations = chain.verify(
                expected_checkout_hash=checkout_hash,
                checkout_jwt=checkout_jwt,
            )
            actual_hash = sdk.compute_sha256_b64url(checkout_jwt)
        except Exception as error:
            raise AP2VerificationError(
                "checkout mandate constraints could not be evaluated"
            ) from error
        if actual_hash != chain.closed_mandate.checkout_hash:
            violations.append("Checkout checkout_hash does not bind the signed checkout_jwt")
    if violations:
        raise AP2VerificationError("; ".join(violations))
    return chain


def _resolve_mandate_type(
    payloads: list[dict[str, Any]],
    requested: Literal["auto", "checkout", "payment"],
) -> Literal["checkout", "payment"]:
    value = payloads[-1].get("vct")
    if value == "mandate.checkout.1":
        detected: Literal["checkout", "payment"] = "checkout"
    elif value == "mandate.payment.1":
        detected = "payment"
    else:
        raise AP2VerificationError("signed chain does not contain a supported AP2 mandate type")
    if requested not in ("auto", detected):
        raise AP2VerificationError(f"expected an AP2 {requested} mandate, got {detected}")
    return detected


def _inspection_result(
    mandate_type: Literal["checkout", "payment"],
    payloads: list[dict[str, Any]],
    chain: Any,
    audience: str,
) -> AP2Inspection:
    checks = (
        "chain_signatures",
        "delegation_bindings",
        "audience",
        "nonce",
        "validity",
        "payload_schema",
        "mandate_constraints",
    )
    closed = chain.closed_mandate
    if mandate_type == "payment":
        details = {
            "transaction_id": _bounded_detail(closed.transaction_id),
            "payee": {
                "id": _bounded_detail(closed.payee.id),
                "name": _bounded_detail(closed.payee.name),
            },
            "amount": {
                "minor_units": closed.payment_amount.amount,
                "currency": _bounded_detail(closed.payment_amount.currency),
            },
            "payment_instrument_type": _bounded_detail(closed.payment_instrument.type),
        }
    else:
        checkout = chain.extract_parsed_checkout_object(closed.checkout_jwt)
        merchant = checkout.merchant
        details = {
            "checkout_hash": _bounded_detail(closed.checkout_hash),
            "checkout": {
                "id": _bounded_detail(checkout.id),
                "merchant": (
                    {
                        "id": _bounded_detail(merchant.id),
                        "name": _bounded_detail(merchant.name),
                    }
                    if merchant is not None
                    else None
                ),
                "status": _bounded_detail(getattr(checkout.status, "value", checkout.status)),
                "currency": _bounded_detail(checkout.currency),
                "line_items": len(checkout.line_items),
            },
        }
        checks = (*checks, "checkout_hash_binding")
    return AP2Inspection(
        type=mandate_type,
        chain_length=len(payloads),
        audience=_bounded_detail(audience),
        checks=checks,
        details=details,
    )


def _bounded_detail(value: Any) -> str:
    text = str(value)
    if len(text) <= MAX_AP2_DETAIL_CHARS:
        return text
    return f"{text[: MAX_AP2_DETAIL_CHARS - 1]}…"


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
            "AP2 support requires the official AP2 v0.2.0 SDK; install "
            f"{AP2_INSTALL_REQUIREMENT!r} alongside a2a-proof"
        ) from error


def _parse_public_jwk(value: dict[str, Any], label: str, sdk: _SDK) -> Any:
    try:
        return sdk.jwk.from_json(json.dumps(value, separators=(",", ":")))
    except Exception as error:
        raise AP2Error(f"{label} is not a valid public P-256 JWK") from error


def _read_public_jwk_file(path: Path) -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AP2Error(f"cannot access trusted root {path}: {error}") from error
    return _read_public_jwk_path(resolved, f"trusted root {path}")


def _read_public_jwk(relative_path: str, contract_dir: Path) -> dict[str, Any]:
    root = contract_dir.resolve()
    try:
        path = (root / relative_path).resolve()
    except OSError as error:
        raise AP2Error(f"cannot access trusted_root_jwk {relative_path!r}: {error}") from error
    if not path.is_relative_to(root):
        raise AP2Error(f"trusted_root_jwk {relative_path!r} escapes the contract directory")
    return _read_public_jwk_path(path, f"trusted_root_jwk {relative_path!r}")


def _read_public_jwk_path(path: Path, label: str) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as error:
        raise AP2Error(f"cannot access {label}: {error}") from error
    if not S_ISREG(stat.st_mode):
        raise AP2Error(f"{label} is not a regular file")
    if stat.st_size > MAX_JWK_BYTES:
        raise AP2Error(f"{label} exceeds {MAX_JWK_BYTES} bytes")
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_JWK_BYTES + 1)
    except OSError as error:
        raise AP2Error(f"cannot read {label}: {error}") from error
    if len(content) > MAX_JWK_BYTES:
        raise AP2Error(f"{label} exceeds {MAX_JWK_BYTES} bytes")
    try:
        value = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AP2Error(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise AP2Error(f"{label} must contain a JSON object")
    if _PRIVATE_JWK_FIELDS.intersection(value):
        raise AP2Error(f"{label} must contain a public key only")
    required = {"kty": "EC", "crv": "P-256"}
    if any(value.get(name) != expected for name, expected in required.items()) or any(
        not isinstance(value.get(name), str) or not value[name] for name in ("x", "y")
    ):
        raise AP2Error(f"{label} must be a public P-256 JWK")
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
