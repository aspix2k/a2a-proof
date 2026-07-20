from __future__ import annotations

import json
import os
from base64 import urlsafe_b64decode
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from stat import S_ISREG
from typing import Any, BinaryIO, Literal

from a2a_proof.models import (
    AP2Expectation,
    AP2MandateExpectation,
    AP2ReceiptExpectation,
    DataPartResult,
    ProofConfig,
)

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
REDACTED_AP2_RECEIPT = "[REDACTED: AP2 receipt]"
COMPACT_JWT_SEGMENTS = 3
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
class AP2ReceiptInspection:
    type: Literal["checkout", "payment"]
    issuer: str
    status: Literal["Success", "Error"]
    reference: str
    checks: tuple[str, ...]
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": True,
            "kind": "receipt",
            "type": self.type,
            "issuer": self.issuer,
            "status": self.status,
            "reference": self.reference,
            "checks": list(self.checks),
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class _SDK:
    mandate_client: type[Any]
    checkout_chain: type[Any]
    payment_chain: type[Any]
    checkout_receipt: type[Any]
    payment_receipt: type[Any]
    jwk: type[Any]
    compute_sha256_b64url: Any
    verify_jwt: Any


class _Missing:
    pass


_MISSING = _Missing()


def read_ap2_token(stream: BinaryIO) -> str:
    return _read_ap2_token(stream, "mandate")


def read_ap2_receipt_token(stream: BinaryIO) -> str:
    return _read_ap2_token(stream, "receipt")


def _read_ap2_token(stream: BinaryIO, kind: Literal["mandate", "receipt"]) -> str:
    content = stream.read(MAX_AP2_TOKEN_BYTES + 1)
    if len(content) > MAX_AP2_TOKEN_BYTES:
        raise AP2Error(f"AP2 {kind} token exceeds {MAX_AP2_TOKEN_BYTES} bytes")
    try:
        token = content.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise AP2Error(f"AP2 {kind} token must be ASCII") from error
    _validate_token(token, kind)
    return token


def _validate_token(token: str, kind: Literal["mandate", "receipt"] = "mandate") -> None:
    if not token:
        raise AP2Error(f"AP2 {kind} token is empty")
    try:
        size = len(token.encode("ascii"))
    except UnicodeEncodeError as error:
        raise AP2Error(f"AP2 {kind} token must be ASCII") from error
    if size > MAX_AP2_TOKEN_BYTES:
        raise AP2Error(f"AP2 {kind} token exceeds {MAX_AP2_TOKEN_BYTES} bytes")
    if any(character.isspace() for character in token):
        raise AP2Error(f"AP2 {kind} token contains whitespace")


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


def inspect_ap2_receipt(
    token: str,
    trusted_issuer_jwk: Path,
    reference: str,
    *,
    receipt_type: Literal["checkout", "payment"],
    issuer: str | None = None,
    status: Literal["Success", "Error"] | None = None,
    payment_id: str | None = None,
    order_id: str | None = None,
    error_code: str | None = None,
) -> AP2ReceiptInspection:
    _validate_token(token, "receipt")
    _validate_receipt_inputs(
        receipt_type,
        reference,
        issuer=issuer,
        status=status,
        payment_id=payment_id,
        order_id=order_id,
        error_code=error_code,
    )
    sdk = _load_sdk()
    public_jwk = _read_public_jwk_file(trusted_issuer_jwk, "trusted receipt issuer")
    issuer_key = _parse_public_jwk(
        public_jwk,
        f"trusted receipt issuer {trusted_issuer_jwk}",
        sdk,
    )
    payload = _verify_receipt_token(
        token,
        issuer_key,
        receipt_type,
        reference,
        sdk,
        issuer=issuer,
        status=status,
        payment_id=payment_id,
        order_id=order_id,
        error_code=error_code,
    )
    return _receipt_inspection_result(receipt_type, payload)


def has_ap2_expectations(config: ProofConfig) -> bool:
    return bool(_configured_expectations(config))


def validate_config_ap2(config: ProofConfig) -> None:
    paths = {
        (
            "trusted_root_jwk"
            if isinstance(expectation, AP2MandateExpectation)
            else "trusted_issuer_jwk",
            expectation.trusted_root_jwk
            if isinstance(expectation, AP2MandateExpectation)
            else expectation.trusted_issuer_jwk,
        )
        for expectation in _configured_expectations(config)
    }
    for field, path in sorted(paths):
        _read_public_jwk(path, config.contract_dir, field)


def ensure_ap2_sdk(config: ProofConfig) -> None:
    if not has_ap2_expectations(config):
        return
    sdk = _load_sdk()
    paths = {
        (
            "trusted_root_jwk"
            if isinstance(expectation, AP2MandateExpectation)
            else "trusted_issuer_jwk",
            expectation.trusted_root_jwk
            if isinstance(expectation, AP2MandateExpectation)
            else expectation.trusted_issuer_jwk,
        )
        for expectation in _configured_expectations(config)
    }
    for field, path in sorted(paths):
        value = _read_public_jwk(path, config.contract_dir, field)
        _parse_public_jwk(value, f"{field} {path!r}", sdk)


def _configured_expectations(config: ProofConfig) -> list[AP2Expectation]:
    return [
        expectation
        for scenario in config.scenarios
        for turn in scenario.resolved_turns()
        for expectation in turn.expect.ap2
    ]


def evaluate_ap2(
    expectations: Sequence[AP2Expectation],
    parts: tuple[DataPartResult, ...],
    contract_dir: Path,
) -> list[str]:
    if not expectations:
        return []
    sdk = _load_sdk()
    failures: list[tuple[int, str]] = []
    references: dict[str, str] = {}
    for index, expectation in enumerate(expectations):
        if isinstance(expectation, AP2ReceiptExpectation):
            continue
        tokens, input_failure = _candidate_values(
            expectation,
            expectation.resolved_path,
            "mandate",
            parts,
        )
        if input_failure is not None:
            failures.append((index, input_failure))
            continue
        reference, failure = _verify_any(expectation, tokens, contract_dir, sdk)
        if failure is not None:
            failures.append((index, failure))
        elif expectation.id is not None and reference is not None:
            references[expectation.id] = reference
    for index, expectation in enumerate(expectations):
        if not isinstance(expectation, AP2ReceiptExpectation):
            continue
        tokens, input_failure = _candidate_values(expectation, expectation.path, "receipt", parts)
        if input_failure is not None:
            failures.append((index, input_failure))
            continue
        reference = references.get(expectation.binds_to)
        if reference is None:
            failures.append(
                (
                    index,
                    f"AP2 {expectation.type} receipt verification failed: bound mandate "
                    f"{expectation.binds_to!r} did not verify",
                )
            )
            continue
        failure = _verify_receipt_any(expectation, tokens, reference, contract_dir, sdk)
        if failure is not None:
            failures.append((index, failure))
    return [failure for _, failure in sorted(failures)]


def redact_ap2(
    expectations: Sequence[AP2Expectation],
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
                expectation.resolved_path
                if isinstance(expectation, AP2MandateExpectation)
                else expectation.path,
                REDACTED_AP2_MANDATE
                if isinstance(expectation, AP2MandateExpectation)
                else REDACTED_AP2_RECEIPT,
            )
            changed = changed or replaced
        redacted.append(part.model_copy(update={"value": value}) if changed else part)
    return tuple(redacted)


def _candidate_values(
    expectation: AP2Expectation,
    path: str,
    kind: Literal["mandate", "receipt"],
    parts: tuple[DataPartResult, ...],
) -> tuple[list[Any], str | None]:
    candidates = [part for part in parts if _matches_location(expectation, part)]
    if not candidates:
        return [], f"no AP2 data matched {_location(expectation)}"
    values = [
        value
        for part in candidates
        if not isinstance(value := _resolve_pointer(part.value, path), _Missing)
    ]
    if not values:
        return [], f"AP2 {kind} path {path!r} was not found"
    return values, None


def _verify_any(
    expectation: AP2MandateExpectation,
    values: list[Any],
    contract_dir: Path,
    sdk: _SDK,
) -> tuple[str | None, str | None]:
    errors: list[str] = []
    for value in values:
        if not isinstance(value, str):
            errors.append("mandate value is not a string")
            continue
        try:
            return _verify(expectation, value, contract_dir, sdk), None
        except Exception as error:
            errors.append(_bounded_error(error))
    detail = errors[0] if errors else "no mandate value was available"
    return None, f"AP2 {expectation.type} mandate verification failed: {detail}"


def _verify(
    expectation: AP2MandateExpectation,
    token: str,
    contract_dir: Path,
    sdk: _SDK,
) -> str:
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
    return _mandate_reference(token, sdk)


def _verify_receipt_any(
    expectation: AP2ReceiptExpectation,
    values: list[Any],
    reference: str,
    contract_dir: Path,
    sdk: _SDK,
) -> str | None:
    errors: list[str] = []
    for value in values:
        if not isinstance(value, str):
            errors.append("receipt value is not a string")
            continue
        try:
            _verify_receipt(expectation, value, reference, contract_dir, sdk)
            return None
        except Exception as error:
            errors.append(_bounded_error(error))
    detail = errors[0] if errors else "no receipt value was available"
    return f"AP2 {expectation.type} receipt verification failed: {detail}"


def _verify_receipt(
    expectation: AP2ReceiptExpectation,
    token: str,
    reference: str,
    contract_dir: Path,
    sdk: _SDK,
) -> None:
    public_jwk = _read_public_jwk(
        expectation.trusted_issuer_jwk,
        contract_dir,
        "trusted_issuer_jwk",
    )
    issuer_key = _parse_public_jwk(
        public_jwk,
        f"trusted_issuer_jwk {expectation.trusted_issuer_jwk!r}",
        sdk,
    )
    _verify_receipt_token(
        token,
        issuer_key,
        expectation.type,
        reference,
        sdk,
        issuer=expectation.issuer,
        status=expectation.status,
        payment_id=expectation.payment_id,
        order_id=expectation.order_id,
        error_code=expectation.error,
    )


def _verify_receipt_token(
    token: str,
    issuer_key: Any,
    receipt_type: Literal["checkout", "payment"],
    reference: str,
    sdk: _SDK,
    *,
    issuer: str | None,
    status: Literal["Success", "Error"] | None,
    payment_id: str | None,
    order_id: str | None,
    error_code: str | None,
) -> dict[str, Any]:
    _validate_token(token, "receipt")
    _require_es256_header(token)
    try:
        payload = sdk.verify_jwt(token, issuer_key)
        if not isinstance(payload, dict):
            raise TypeError
        receipt_model = sdk.payment_receipt if receipt_type == "payment" else sdk.checkout_receipt
        receipt_model.model_validate(payload, strict=True)
    except Exception as error:
        raise AP2VerificationError("receipt signature or payload is invalid") from error

    if payload.get("reference") != reference:
        raise AP2VerificationError("receipt reference does not bind to the verified mandate")
    expected = {
        "iss": issuer,
        "status": status,
        "payment_id": payment_id,
        "order_id": order_id,
        "error": error_code,
    }
    mismatches = [
        f"receipt {field} does not match the expected value"
        for field, value in expected.items()
        if value is not None and payload.get(field) != value
    ]
    if mismatches:
        raise AP2VerificationError("; ".join(mismatches))
    return payload


def _validate_receipt_inputs(
    receipt_type: Literal["checkout", "payment"],
    reference: str,
    *,
    issuer: str | None,
    status: Literal["Success", "Error"] | None,
    payment_id: str | None,
    order_id: str | None,
    error_code: str | None,
) -> None:
    for label, value in (
        ("reference", reference),
        ("issuer", issuer),
        ("status", status),
        ("payment ID", payment_id),
        ("order ID", order_id),
        ("error", error_code),
    ):
        if value is not None and not 1 <= len(value) <= MAX_AP2_EXPECTED_VALUE_CHARS:
            raise AP2Error(
                f"{label} must contain between 1 and {MAX_AP2_EXPECTED_VALUE_CHARS} characters"
            )
    if receipt_type == "checkout" and payment_id is not None:
        raise AP2Error("--payment-id requires a payment receipt")
    if receipt_type == "payment" and order_id is not None:
        raise AP2Error("--order-id requires a checkout receipt")
    if status == "Success" and error_code is not None:
        raise AP2Error("--error cannot be asserted for a successful receipt")


def _require_es256_header(token: str) -> None:
    segments = token.split(".")
    if len(segments) != COMPACT_JWT_SEGMENTS or not all(segments):
        raise AP2VerificationError("receipt is not a compact JWT")
    try:
        header = json.loads(urlsafe_b64decode(f"{segments[0]}==="))
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise AP2VerificationError("receipt has an invalid protected header") from error
    if not isinstance(header, dict) or header.get("alg") != "ES256":
        raise AP2VerificationError("receipt protected header must use ES256")


def _mandate_reference(token: str, sdk: _SDK) -> str:
    try:
        closed_jwt = sdk.mandate_client().get_closed_mandate_jwt(token)
        if not isinstance(closed_jwt, str) or not closed_jwt:
            raise ValueError
        reference = sdk.compute_sha256_b64url(closed_jwt)
    except Exception as error:
        raise AP2VerificationError("closed mandate reference could not be computed") from error
    if not isinstance(reference, str) or not reference:
        raise AP2VerificationError("closed mandate reference could not be computed")
    return reference


def ap2_mandate_reference(token: str) -> str:
    _validate_token(token)
    return _mandate_reference(token, _load_sdk())


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


def _receipt_inspection_result(
    receipt_type: Literal["checkout", "payment"],
    payload: dict[str, Any],
) -> AP2ReceiptInspection:
    details = {
        "issued_at": payload["iat"],
        "error": _bounded_optional_detail(payload.get("error")),
        "error_description": _bounded_optional_detail(payload.get("error_description")),
    }
    if receipt_type == "payment":
        details.update(
            {
                "payment_id": _bounded_detail(payload["payment_id"]),
                "psp_confirmation_id": _bounded_optional_detail(payload.get("psp_confirmation_id")),
                "network_confirmation_id": _bounded_optional_detail(
                    payload.get("network_confirmation_id")
                ),
            }
        )
    else:
        details["order_id"] = _bounded_optional_detail(payload.get("order_id"))
    return AP2ReceiptInspection(
        type=receipt_type,
        issuer=_bounded_detail(payload["iss"]),
        status=payload["status"],
        reference=_bounded_detail(payload["reference"]),
        checks=("es256_signature", "payload_schema", "mandate_reference"),
        details=details,
    )


def _bounded_detail(value: Any) -> str:
    text = str(value)
    if len(text) <= MAX_AP2_DETAIL_CHARS:
        return text
    return f"{text[: MAX_AP2_DETAIL_CHARS - 1]}…"


def _bounded_optional_detail(value: Any) -> str | None:
    return None if value is None else _bounded_detail(value)


def _load_sdk() -> _SDK:
    try:
        mandate = import_module("ap2.sdk.mandate")
        checkout = import_module("ap2.sdk.checkout_mandate_chain")
        payment = import_module("ap2.sdk.payment_mandate_chain")
        checkout_receipt = import_module("ap2.sdk.generated.checkout_receipt")
        payment_receipt = import_module("ap2.sdk.generated.payment_receipt")
        jwt_helper = import_module("ap2.sdk.jwt_helper")
        utils = import_module("ap2.sdk.utils")
        jwk = import_module("jwcrypto.jwk")
        mandate.__dict__["LOG_FILE_PATH"] = os.devnull
        return _SDK(
            mandate_client=mandate.MandateClient,
            checkout_chain=checkout.CheckoutMandateChain,
            payment_chain=payment.PaymentMandateChain,
            checkout_receipt=checkout_receipt.CheckoutReceipt,
            payment_receipt=payment_receipt.PaymentReceipt,
            jwk=jwk.JWK,
            compute_sha256_b64url=utils.compute_sha256_b64url,
            verify_jwt=jwt_helper.verify_jwt,
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


def _read_public_jwk_file(path: Path, label: str = "trusted root") -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AP2Error(f"cannot access {label} {path}: {error}") from error
    return _read_public_jwk_path(resolved, f"{label} {path}")


def _read_public_jwk(
    relative_path: str,
    contract_dir: Path,
    field: str = "trusted_root_jwk",
) -> dict[str, Any]:
    root = contract_dir.resolve()
    try:
        path = (root / relative_path).resolve()
    except OSError as error:
        raise AP2Error(f"cannot access {field} {relative_path!r}: {error}") from error
    if not path.is_relative_to(root):
        raise AP2Error(f"{field} {relative_path!r} escapes the contract directory")
    return _read_public_jwk_path(path, f"{field} {relative_path!r}")


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
    expectation: AP2Expectation,
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


def _location(expectation: AP2Expectation) -> str:
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
