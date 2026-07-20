from __future__ import annotations

import json
from base64 import urlsafe_b64encode
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import a2a_proof.ap2 as ap2_module
from a2a_proof.ap2 import (
    _SDK,
    AP2Error,
    AP2VerificationError,
    _bounded_detail,
    _bounded_error,
    _load_sdk,
    _read_public_jwk,
    _read_public_jwk_file,
    _replace_pointer,
    _require_es256_header,
    _resolve_pointer,
    _validate_receipt_inputs,
    _verify_receipt,
    _verify_receipt_token,
    _verify_signed_chain,
    _verify_typed_chain,
    ap2_mandate_reference,
    ensure_ap2_sdk,
    evaluate_ap2,
    has_ap2_expectations,
    inspect_ap2,
    inspect_ap2_receipt,
    read_ap2_receipt_token,
    read_ap2_token,
    redact_ap2,
    validate_config_ap2,
)
from a2a_proof.models import (
    AP2MandateExpectation,
    AP2ReceiptExpectation,
    DataPartResult,
    Expectation,
    ProofConfig,
)


def _write_jwk(path: Path) -> Path:
    path.write_text(
        json.dumps({"kty": "EC", "crv": "P-256", "x": "eA", "y": "eQ", "kid": "root"}),
        encoding="utf-8",
    )
    return path


def _expectation(**updates: Any) -> AP2MandateExpectation:
    values = {
        "type": "payment",
        "trusted_root_jwk": "root.jwk",
        "audience": "merchant",
        "nonce": "nonce-1",
    }
    values.update(updates)
    return AP2MandateExpectation.model_validate(values)


def _sdk(
    calls: dict[str, Any],
    *,
    payloads: Any = None,
    payment_parse_error: Exception | None = None,
    payment_violations: list[str] | Exception | None = None,
    checkout_parse_error: Exception | None = None,
    checkout_violations: list[str] | Exception | None = None,
    checkout_jwt: str = "checkout.jwt.value",
    checkout_hash: str = "computed-hash",
    hash_error: Exception | None = None,
    hash_result: Any = "computed-hash",
    jwk_error: Exception | None = None,
    receipt_payload: Any = None,
    receipt_error: Exception | None = None,
    receipt_model_error: Exception | None = None,
    closed_mandate_jwt: Any = "closed.jwt",
) -> _SDK:
    class JWK:
        @classmethod
        def from_json(cls, value: str) -> dict[str, Any]:
            if jwk_error is not None:
                raise jwk_error
            calls["jwk"] = json.loads(value)
            return calls["jwk"]

    class MandateClient:
        def verify(self, **kwargs: Any) -> Any:
            calls["verify"] = kwargs
            if isinstance(payloads, Exception):
                raise payloads
            return (
                [{"vct": "mandate.payment.open.1"}, {"vct": "mandate.payment.1"}]
                if payloads is None
                else payloads
            )

        def get_closed_mandate_jwt(self, token: str) -> str:
            calls["closed_mandate"] = token
            if isinstance(closed_mandate_jwt, Exception):
                raise closed_mandate_jwt
            return closed_mandate_jwt

    class PaymentReceiptModel:
        @classmethod
        def model_validate(cls, value: Any, *, strict: bool) -> Any:
            if receipt_model_error is not None:
                raise receipt_model_error
            calls["receipt_model"] = ("payment", value, strict)
            return value

    class CheckoutReceiptModel:
        @classmethod
        def model_validate(cls, value: Any, *, strict: bool) -> Any:
            if receipt_model_error is not None:
                raise receipt_model_error
            calls["receipt_model"] = ("checkout", value, strict)
            return value

    def verify_jwt(token: str, key: Any) -> Any:
        if receipt_error is not None:
            raise receipt_error
        calls["receipt_verify"] = (token, key)
        return (
            {
                "status": "Success",
                "iss": "processor.example",
                "iat": 1_700_000_000,
                "reference": "computed-hash",
                "payment_id": "pay-1",
                "psp_confirmation_id": "psp-1",
                "network_confirmation_id": "network-1",
            }
            if receipt_payload is None
            else receipt_payload
        )

    class PaymentChain:
        closed_mandate = SimpleNamespace(
            transaction_id="checkout-hash",
            payee=SimpleNamespace(id="shop-1", name="Shop"),
            payment_amount=SimpleNamespace(amount=1_000, currency="USD"),
            payment_instrument=SimpleNamespace(type="card"),
        )

        @classmethod
        def parse(cls, value: list[dict[str, Any]]) -> PaymentChain:
            if payment_parse_error is not None:
                raise payment_parse_error
            calls["payment_payloads"] = value
            return cls()

        def verify(self, **kwargs: Any) -> list[str]:
            calls["payment_expectations"] = kwargs
            if isinstance(payment_violations, Exception):
                raise payment_violations
            return payment_violations or []

    class CheckoutChain:
        closed_mandate = SimpleNamespace(
            checkout_jwt=checkout_jwt,
            checkout_hash=checkout_hash,
        )

        @classmethod
        def parse(cls, value: list[dict[str, Any]]) -> CheckoutChain:
            if checkout_parse_error is not None:
                raise checkout_parse_error
            calls["checkout_payloads"] = value
            return cls()

        def verify(self, **kwargs: Any) -> list[str]:
            calls["checkout_expectations"] = kwargs
            if isinstance(checkout_violations, Exception):
                raise checkout_violations
            return list(checkout_violations or [])

        def extract_parsed_checkout_object(self, value: str) -> Any:
            calls["parsed_checkout"] = value
            return SimpleNamespace(
                id="checkout-1",
                merchant=SimpleNamespace(id="shop-1", name="Shop"),
                status=SimpleNamespace(value="completed"),
                currency="USD",
                line_items=[object(), object()],
            )

    def compute_hash(value: str) -> str:
        if hash_error is not None:
            raise hash_error
        calls.setdefault("hashed", value)
        return hash_result

    return _SDK(
        mandate_client=MandateClient,
        checkout_chain=CheckoutChain,
        payment_chain=PaymentChain,
        checkout_receipt=CheckoutReceiptModel,
        payment_receipt=PaymentReceiptModel,
        jwk=JWK,
        compute_sha256_b64url=compute_hash,
        verify_jwt=verify_jwt,
    )


def _part(value: Any, **updates: Any) -> DataPartResult:
    return DataPartResult(
        source="artifact",
        artifact_name="payment",
        media_type="application/json",
        value=value,
        **updates,
    )


def _receipt_token(alg: str = "ES256") -> str:
    header = urlsafe_b64encode(json.dumps({"alg": alg}).encode()).decode().rstrip("=")
    return f"{header}.payload.signature"


def _receipt_expectation(**updates: Any) -> AP2ReceiptExpectation:
    values = {
        "kind": "receipt",
        "type": "payment",
        "binds_to": "payment",
        "trusted_issuer_jwk": "issuer.jwk",
        "path": "/payment_receipt",
    }
    values.update(updates)
    return AP2ReceiptExpectation.model_validate(values)


def test_reads_bounded_ascii_token() -> None:
    assert read_ap2_token(BytesIO(b"  signed.chain~~presented.chain\n")) == (
        "signed.chain~~presented.chain"
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "is empty"),
        ("токен".encode(), "must be ASCII"),
        (b"signed chain", "contains whitespace"),
    ],
)
def test_rejects_invalid_token_input(content: bytes, message: str) -> None:
    with pytest.raises(AP2Error, match=message):
        read_ap2_token(BytesIO(content))


def test_bounds_token_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ap2_module, "MAX_AP2_TOKEN_BYTES", 4)

    with pytest.raises(AP2Error, match="exceeds 4 bytes"):
        read_ap2_token(BytesIO(b"12345"))
    with pytest.raises(AP2Error, match="exceeds 4 bytes"):
        inspect_ap2("12345", Path("root.jwk"), "merchant", "nonce")

    with pytest.raises(AP2Error, match="must be ASCII"):
        inspect_ap2("токен", Path("root.jwk"), "merchant", "nonce")


def test_inspects_payment_mandate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    calls: dict[str, Any] = {}
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk(calls))

    result = inspect_ap2(
        "signed-chain",
        root,
        "merchant",
        "payment-nonce",
        transaction_id="checkout-hash",
    )

    assert result.as_dict() == {
        "valid": True,
        "type": "payment",
        "chain_length": 2,
        "audience": "merchant",
        "checks": [
            "chain_signatures",
            "delegation_bindings",
            "audience",
            "nonce",
            "validity",
            "payload_schema",
            "mandate_constraints",
        ],
        "details": {
            "transaction_id": "checkout-hash",
            "payee": {"id": "shop-1", "name": "Shop"},
            "amount": {"minor_units": 1_000, "currency": "USD"},
            "payment_instrument_type": "card",
        },
    }
    assert calls["verify"]["expected_aud"] == "merchant"
    assert calls["verify"]["expected_nonce"] == "payment-nonce"


def test_signed_chain_verifier_passes_security_inputs() -> None:
    calls: dict[str, Any] = {}
    sdk = _sdk(calls)
    root_key = object()

    payloads = _verify_signed_chain("signed-chain", root_key, "merchant", "nonce", sdk)

    assert payloads[-1]["vct"] == "mandate.payment.1"
    assert calls["verify"]["token"] == "signed-chain"
    assert calls["verify"]["key_or_provider"](object()) is root_key
    assert calls["verify"]["expected_aud"] == "merchant"
    assert calls["verify"]["expected_nonce"] == "nonce"


def test_typed_chain_verifier_preserves_all_violations() -> None:
    sdk = _sdk({}, payment_violations=["amount exceeds maximum", "currency differs"])

    with pytest.raises(AP2VerificationError) as raised:
        _verify_typed_chain(
            "payment",
            [{"vct": "open"}, {"vct": "mandate.payment.1"}],
            sdk,
            transaction_id=None,
            open_checkout_hash=None,
            checkout_hash=None,
        )

    assert str(raised.value) == "amount exceeds maximum; currency differs"


def test_inspects_checkout_mandate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    calls: dict[str, Any] = {}
    payloads = [
        {"vct": "mandate.checkout.open.1"},
        {"vct": "mandate.checkout.1"},
    ]
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk(calls, payloads=payloads),
    )

    result = inspect_ap2(
        "signed-chain",
        root,
        "merchant",
        "checkout-nonce",
        mandate_type="checkout",
        checkout_hash="computed-hash",
    )

    assert result.type == "checkout"
    assert result.checks[-1] == "checkout_hash_binding"
    assert result.details == {
        "checkout_hash": "computed-hash",
        "checkout": {
            "id": "checkout-1",
            "merchant": {"id": "shop-1", "name": "Shop"},
            "status": "completed",
            "currency": "USD",
            "line_items": 2,
        },
    }
    assert calls["parsed_checkout"] == "checkout.jwt.value"


def test_hides_verified_summary_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))

    def fail(*_args: Any) -> Any:
        raise ValueError("sensitive verified payload")

    monkeypatch.setattr(ap2_module, "_inspection_result", fail)

    with pytest.raises(AP2Error, match="could not be summarized") as raised:
        inspect_ap2("signed-chain", root, "merchant", "nonce")
    assert "sensitive verified payload" not in str(raised.value)


@pytest.mark.parametrize(
    ("payloads", "mandate_type", "message"),
    [
        ([{"vct": "open"}, {"vct": "unknown"}], "auto", "supported AP2 mandate type"),
        (
            [{"vct": "open"}, {"vct": "mandate.payment.1"}],
            "checkout",
            "expected an AP2 checkout mandate, got payment",
        ),
    ],
)
def test_rejects_unknown_or_mismatched_inspection_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, Any]],
    mandate_type: Any,
    message: str,
) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}, payloads=payloads))

    with pytest.raises(AP2VerificationError, match=message):
        inspect_ap2(
            "signed-chain",
            root,
            "merchant",
            "nonce",
            mandate_type=mandate_type,
        )


def test_rejects_incompatible_inspection_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))

    with pytest.raises(AP2Error, match="--checkout-hash requires a checkout mandate"):
        inspect_ap2("signed-chain", root, "merchant", "nonce", checkout_hash="hash")

    checkout_payloads = [
        {"vct": "mandate.checkout.open.1"},
        {"vct": "mandate.checkout.1"},
    ]
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, payloads=checkout_payloads),
    )
    with pytest.raises(AP2Error, match=r"transaction-id.*require a payment mandate"):
        inspect_ap2("signed-chain", root, "merchant", "nonce", transaction_id="tx")


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("audience", "audience"),
        ("nonce", "nonce"),
        ("transaction_id", "transaction ID"),
        ("open_checkout_hash", "open checkout hash"),
        ("checkout_hash", "checkout hash"),
    ],
)
def test_bounds_inspection_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    label: str,
) -> None:
    values: dict[str, Any] = {
        "token": "signed-chain",
        "trusted_root_jwk": tmp_path / "root.jwk",
        "audience": "merchant",
        "nonce": "nonce",
        field: "",
    }
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: pytest.fail("must validate first"))

    with pytest.raises(
        AP2Error,
        match=rf"^{label} must contain between 1 and 1000 characters$",
    ):
        inspect_ap2(**values)


def test_names_invalid_transaction_id(tmp_path: Path) -> None:
    with pytest.raises(
        AP2Error,
        match="transaction ID must contain between 1 and 1000 characters",
    ):
        inspect_ap2("signed-chain", tmp_path / "root.jwk", "merchant", "nonce", transaction_id="")


def test_reports_missing_inspector_trust_root(tmp_path: Path) -> None:
    with pytest.raises(AP2Error, match="cannot access trusted root"):
        _read_public_jwk_file(tmp_path / "missing.jwk")


def test_bounds_inspected_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ap2_module, "MAX_AP2_DETAIL_CHARS", 4)

    assert _bounded_detail("1234") == "1234"
    assert _bounded_detail("12345") == "123…"


def test_evaluates_signed_payment_mandate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_jwk(tmp_path / "root.jwk")
    calls: dict[str, Any] = {}
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk(calls))
    expectation = _expectation(
        source="artifact",
        artifact_name="payment",
        media_type="APPLICATION/JSON",
        transaction_id="tx-1",
        open_checkout_hash="checkout-1",
    )
    parts = (_part({"ap2.mandates.PaymentMandateSdJwt": "signed-chain"}),)

    assert evaluate_ap2([expectation], parts, tmp_path) == []
    assert calls["verify"]["token"] == "signed-chain"
    assert calls["verify"]["expected_aud"] == "merchant"
    assert calls["verify"]["expected_nonce"] == "nonce-1"
    assert calls["verify"]["key_or_provider"](object()) == calls["jwk"]
    assert calls["payment_expectations"] == {
        "expected_transaction_id": "tx-1",
        "expected_open_checkout_hash": "checkout-1",
    }


def test_evaluates_checkout_binding_and_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    calls: dict[str, Any] = {}
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk(calls))
    expectation = AP2MandateExpectation(
        type="checkout",
        trusted_root_jwk="root.jwk",
        audience="merchant",
        nonce="nonce-1",
        checkout_hash="computed-hash",
    )
    parts = (_part({"ap2.mandates.CheckoutMandateSdJwt": "signed-chain"}),)

    assert evaluate_ap2([expectation], parts, tmp_path) == []
    assert calls["checkout_expectations"] == {
        "expected_checkout_hash": "computed-hash",
        "checkout_jwt": "checkout.jwt.value",
    }
    assert calls["hashed"] == "checkout.jwt.value"


@pytest.mark.parametrize(
    ("sdk_updates", "expected"),
    [
        (
            {"payment_violations": ["amount exceeds maximum"]},
            "AP2 payment mandate verification failed: amount exceeds maximum",
        ),
        (
            {"payloads": {"not": "a chain"}},
            "AP2 payment mandate verification failed: expected a signed mandate chain",
        ),
        (
            {"payloads": ValueError("bad\n signature")},
            "AP2 payment mandate verification failed: signed chain failed signature, binding, "
            "audience, nonce, or validity checks",
        ),
        (
            {"payment_parse_error": ValueError("sensitive payload")},
            "AP2 payment mandate verification failed: payment mandate payload is invalid",
        ),
        (
            {"payment_violations": ValueError("sensitive payload")},
            "AP2 payment mandate verification failed: payment mandate constraints could not be "
            "evaluated",
        ),
    ],
)
def test_reports_payment_verification_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_updates: dict[str, Any],
    expected: str,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}, **sdk_updates))
    parts = (_part({"ap2.mandates.PaymentMandateSdJwt": "invalid"}),)

    assert evaluate_ap2([_expectation()], parts, tmp_path) == [expected]


def test_reports_checkout_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_jwk(tmp_path / "root.jwk")
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, checkout_hash="different"),
    )
    expectation = AP2MandateExpectation(
        type="checkout",
        trusted_root_jwk="root.jwk",
        audience="merchant",
        nonce="nonce-1",
    )
    parts = (_part({"ap2.mandates.CheckoutMandateSdJwt": "invalid"}),)

    assert evaluate_ap2([expectation], parts, tmp_path) == [
        "AP2 checkout mandate verification failed: Checkout checkout_hash does not bind the "
        "signed checkout_jwt"
    ]


@pytest.mark.parametrize(
    ("sdk_updates", "message"),
    [
        (
            {"checkout_parse_error": ValueError("sensitive payload")},
            "checkout mandate payload is invalid",
        ),
        (
            {"checkout_violations": ValueError("sensitive payload")},
            "checkout mandate constraints could not be evaluated",
        ),
        (
            {"hash_error": ValueError("sensitive payload")},
            "checkout mandate constraints could not be evaluated",
        ),
    ],
)
def test_reports_checkout_validation_errors_without_response_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_updates: dict[str, Any],
    message: str,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}, **sdk_updates))
    expectation = AP2MandateExpectation(
        type="checkout",
        trusted_root_jwk="root.jwk",
        audience="merchant",
        nonce="nonce-1",
    )
    parts = (_part({"ap2.mandates.CheckoutMandateSdJwt": "invalid"}),)

    failure = evaluate_ap2([expectation], parts, tmp_path)[0]

    assert failure == f"AP2 checkout mandate verification failed: {message}"
    assert "sensitive payload" not in failure


def test_matches_any_valid_candidate_and_supports_custom_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    calls = 0

    def sdk() -> _SDK:
        nonlocal calls
        current = _sdk({})
        original = current.mandate_client.verify

        def verify(self: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return original(self, **kwargs)

        current.mandate_client.verify = verify
        return current

    monkeypatch.setattr(ap2_module, "_load_sdk", sdk)
    expectation = _expectation(path="/nested/1/token")
    parts = (
        _part({"nested": [{}, {"token": 42}]}),
        _part({"nested": [{}, {"token": "valid"}]}),
    )

    assert evaluate_ap2([expectation], parts, tmp_path) == []
    assert calls == 1


def test_reports_location_path_and_value_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))
    expectation = _expectation(
        source="message",
        media_type="application/json",
    )
    no_location = evaluate_ap2(
        [expectation],
        (_part({"ap2.mandates.PaymentMandateSdJwt": "token"}),),
        tmp_path,
    )
    wrong_artifact = evaluate_ap2(
        [_expectation(artifact_name="other")],
        (_part({"ap2.mandates.PaymentMandateSdJwt": "token"}),),
        tmp_path,
    )
    missing = evaluate_ap2(
        [_expectation()],
        (_part({"other": "token"}),),
        tmp_path,
    )
    wrong_type = evaluate_ap2(
        [_expectation()],
        (_part({"ap2.mandates.PaymentMandateSdJwt": 42}),),
        tmp_path,
    )

    assert no_location == ["no AP2 data matched source 'message', media type 'application/json'"]
    assert wrong_artifact == ["no AP2 data matched artifact 'other'"]
    assert missing == ["AP2 mandate path '/ap2.mandates.PaymentMandateSdJwt' was not found"]
    assert wrong_type == ["AP2 payment mandate verification failed: mandate value is not a string"]


def test_empty_expectations_do_not_load_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: pytest.fail("SDK should not be loaded"),
    )

    assert evaluate_ap2([], (), Path.cwd()) == []


def test_redacts_configured_mandates_without_mutating_response() -> None:
    original = _part(
        {
            "ap2.mandates.PaymentMandateSdJwt": "signed-chain",
            "ap2.PaymentReceipt": {"status": "succeeded"},
        }
    )

    redacted = redact_ap2(
        [_expectation(artifact_name="payment")],
        (original,),
    )

    assert redacted[0].value == {
        "ap2.mandates.PaymentMandateSdJwt": ap2_module.REDACTED_AP2_MANDATE,
        "ap2.PaymentReceipt": {"status": "succeeded"},
    }
    assert original.value == {
        "ap2.mandates.PaymentMandateSdJwt": "signed-chain",
        "ap2.PaymentReceipt": {"status": "succeeded"},
    }


def test_redacts_custom_array_and_root_paths() -> None:
    part = _part({"items": [{"token": "signed-chain"}]})
    nested = redact_ap2([_expectation(path="/items/0/token")], (part,))
    root = redact_ap2([_expectation(path="")], (part,))

    assert nested[0].value == {"items": [{"token": ap2_module.REDACTED_AP2_MANDATE}]}
    assert root[0].value == ap2_module.REDACTED_AP2_MANDATE


def test_leaves_unmatched_or_missing_mandates_unchanged() -> None:
    part = _part({"items": [{"token": "signed-chain"}]})
    expectations = [
        _expectation(artifact_name="other"),
        _expectation(path="/missing"),
        _expectation(path="/items/01"),
        _expectation(path="/items/1"),
        _expectation(path="/items/token"),
        _expectation(path="/items/0/missing"),
    ]

    assert redact_ap2(expectations, (part,)) == (part,)


def test_replace_pointer_stops_below_scalars() -> None:
    replaced, value = _replace_pointer({"token": "value"}, "/token/nested", "redacted")

    assert not replaced
    assert value == {"token": "value"}


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"trusted_root_jwk": "bad\0key"}, "contains a null byte"),
        ({"trusted_root_jwk": "/root.jwk"}, "must be relative"),
        ({"trusted_root_jwk": "C:\\root.jwk"}, "must be relative"),
        ({"path": "/bad~2escape"}, "RFC 6901 JSON Pointer"),
        (
            {"source": "message", "artifact_name": "payment"},
            "artifact_name cannot be used with source: message",
        ),
        (
            {"type": "checkout", "transaction_id": "tx"},
            "require an AP2 payment mandate",
        ),
        (
            {"type": "checkout", "open_checkout_hash": "hash"},
            "require an AP2 payment mandate",
        ),
        (
            {"type": "payment", "checkout_hash": "hash"},
            "requires an AP2 checkout mandate",
        ),
    ],
)
def test_rejects_invalid_ap2_expectation(updates: dict[str, Any], message: str) -> None:
    values = {
        "type": "payment",
        "trusted_root_jwk": "root.jwk",
        "audience": "merchant",
        "nonce": "nonce",
        **updates,
    }

    with pytest.raises(ValueError, match=message):
        AP2MandateExpectation.model_validate(values)


def test_detects_and_validates_configured_ap2_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "payment",
                    "message": "pay",
                    "expect": {"ap2": _expectation().model_dump()},
                }
            ],
        }
    )
    config.bind_contract_dir(tmp_path)
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        ap2_module,
        "_read_public_jwk",
        lambda path, root, _field="trusted_root_jwk": calls.append((path, root)) or {},
    )
    loads = 0
    sdk_calls: dict[str, Any] = {}

    def load() -> _SDK:
        nonlocal loads
        loads += 1
        return _sdk(sdk_calls)

    monkeypatch.setattr(ap2_module, "_load_sdk", load)

    assert has_ap2_expectations(config)
    validate_config_ap2(config)
    ensure_ap2_sdk(config)
    assert calls == [("root.jwk", tmp_path), ("root.jwk", tmp_path)]
    assert loads == 1
    assert sdk_calls["jwk"] == {}

    empty = config.model_copy(
        update={
            "scenarios": [
                config.scenarios[0].model_copy(
                    update={"expect": config.scenarios[0].expect.model_copy(update={"ap2": []})}
                )
            ]
        }
    )
    assert not has_ap2_expectations(empty)
    ensure_ap2_sdk(empty)
    assert loads == 1


def test_runtime_preflight_rejects_invalid_public_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "payment",
                    "message": "pay",
                    "expect": {"ap2": _expectation().model_dump()},
                }
            ],
        }
    )
    config.bind_contract_dir(tmp_path)
    monkeypatch.setattr(
        ap2_module,
        "_read_public_jwk",
        lambda _path, _root, _field="trusted_root_jwk": {
            "kty": "EC",
            "crv": "P-256",
            "x": "eA",
            "y": "eQ",
        },
    )
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, jwk_error=ValueError("invalid point")),
    )

    with pytest.raises(AP2Error, match="is not a valid public P-256 JWK"):
        ensure_ap2_sdk(config)


def test_loads_official_sdk_and_disables_its_file_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mandate = SimpleNamespace(MandateClient=object, LOG_FILE_PATH="original")
    modules = {
        "ap2.sdk.mandate": mandate,
        "ap2.sdk.checkout_mandate_chain": SimpleNamespace(CheckoutMandateChain=object),
        "ap2.sdk.payment_mandate_chain": SimpleNamespace(PaymentMandateChain=object),
        "ap2.sdk.generated.checkout_receipt": SimpleNamespace(CheckoutReceipt=object),
        "ap2.sdk.generated.payment_receipt": SimpleNamespace(PaymentReceipt=object),
        "ap2.sdk.jwt_helper": SimpleNamespace(verify_jwt=object()),
        "ap2.sdk.utils": SimpleNamespace(compute_sha256_b64url=object()),
        "jwcrypto.jwk": SimpleNamespace(JWK=object),
    }
    monkeypatch.setattr(ap2_module, "import_module", modules.__getitem__)

    sdk = _load_sdk()

    assert sdk.mandate_client is object
    assert ap2_module.os.devnull == mandate.LOG_FILE_PATH


def test_reports_missing_or_incompatible_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> Any:
        raise ImportError("missing")

    monkeypatch.setattr(ap2_module, "import_module", missing)

    with pytest.raises(AP2Error, match=r"official AP2 v0\.2\.0 SDK") as raised:
        _load_sdk()
    assert ap2_module.AP2_SDK_COMMIT in str(raised.value)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"not json", "is not valid JSON"),
        (b"[]", "must contain a JSON object"),
        (
            b'{"kty":"EC","crv":"P-256","x":"eA","y":"eQ","d":"private"}',
            "must contain a public key only",
        ),
        (b'{"kty":"RSA","crv":"P-256","x":"eA","y":"eQ"}', "public P-256 JWK"),
        (b'{"kty":"EC","crv":"P-256","x":"","y":"eQ"}', "public P-256 JWK"),
    ],
)
def test_rejects_invalid_trusted_jwk(tmp_path: Path, content: bytes, message: str) -> None:
    (tmp_path / "root.jwk").write_bytes(content)

    with pytest.raises(AP2Error, match=message):
        _read_public_jwk("root.jwk", tmp_path)


def test_confines_and_bounds_trusted_jwk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = _write_jwk(tmp_path.parent / "outside.jwk")
    with pytest.raises(AP2Error, match="escapes the contract directory"):
        _read_public_jwk(f"../{outside.name}", tmp_path)
    with pytest.raises(AP2Error, match="cannot access"):
        _read_public_jwk("missing.jwk", tmp_path)
    with pytest.raises(AP2Error, match="not a regular file"):
        _read_public_jwk(".", tmp_path)

    oversized = tmp_path / "oversized.jwk"
    oversized.write_bytes(b"x" * (ap2_module.MAX_JWK_BYTES + 1))
    with pytest.raises(AP2Error, match="exceeds"):
        _read_public_jwk("oversized.jwk", tmp_path)

    root = _write_jwk(tmp_path / "root.jwk")
    original_open = Path.open

    def denied(self: Path, mode: str = "r") -> Any:
        if self == root:
            raise OSError("denied")
        return original_open(self, mode)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(AP2Error, match=r"cannot read.*denied"):
        _read_public_jwk("root.jwk", tmp_path)


def test_reports_contract_root_resolution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_resolve = Path.resolve

    def denied(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self.name == "root.jwk":
            raise OSError("denied")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", denied)

    with pytest.raises(AP2Error, match=r"cannot access trusted_root_jwk.*denied"):
        _read_public_jwk("root.jwk", tmp_path)


def test_bounds_trusted_jwk_read_after_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    original_open = Path.open

    def grown(self: Path, mode: str = "r") -> Any:
        if self == root:
            return BytesIO(b"x" * (ap2_module.MAX_JWK_BYTES + 1))
        return original_open(self, mode)

    monkeypatch.setattr(Path, "open", grown)

    with pytest.raises(AP2Error, match="exceeds"):
        _read_public_jwk("root.jwk", tmp_path)


def test_resolves_json_pointer_strictly() -> None:
    value = {"items": [{"token": "first"}], "scalar": "value"}

    assert _resolve_pointer(value, "/items/0/token") == "first"
    assert isinstance(_resolve_pointer(value, "/items/01"), ap2_module._Missing)
    assert isinstance(_resolve_pointer(value, "/items/1"), ap2_module._Missing)
    assert isinstance(_resolve_pointer(value, "/items/token"), ap2_module._Missing)
    assert isinstance(_resolve_pointer(value, "/scalar/0"), ap2_module._Missing)


def test_bounds_and_normalizes_sdk_errors() -> None:
    assert _bounded_error(ValueError()) == "ValueError"
    assert _bounded_error(ValueError("first\n second")) == "first second"
    bounded = _bounded_error(ValueError("x" * (ap2_module.MAX_AP2_ERROR_CHARS + 1)))
    assert len(bounded) == ap2_module.MAX_AP2_ERROR_CHARS
    assert bounded.endswith("…")


def test_token_reader_enforces_the_exact_read_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream(BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            assert size == 5
            return super().read(size)

    monkeypatch.setattr(ap2_module, "MAX_AP2_TOKEN_BYTES", 4)

    assert read_ap2_token(Stream(b"1234")) == "1234"


@pytest.mark.parametrize(
    ("token", "message"),
    [
        ("", "AP2 mandate token is empty"),
        ("токен", "AP2 mandate token must be ASCII"),
        ("signed chain", "AP2 mandate token contains whitespace"),
    ],
)
def test_token_validator_reports_exact_errors(token: str, message: str) -> None:
    with pytest.raises(AP2Error) as raised:
        ap2_module._validate_token(token)

    assert str(raised.value) == message


def test_token_reader_reports_exact_encoding_error() -> None:
    with pytest.raises(AP2Error) as raised:
        read_ap2_token(BytesIO("токен".encode()))

    assert str(raised.value) == "AP2 mandate token must be ASCII"


def test_token_validator_accepts_the_exact_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ap2_module, "MAX_AP2_TOKEN_BYTES", 4)

    ap2_module._validate_token("1234")


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("audience", "audience"),
        ("nonce", "nonce"),
        ("transaction_id", "transaction ID"),
        ("open_checkout_hash", "open checkout hash"),
        ("checkout_hash", "checkout hash"),
    ],
)
def test_inspector_accepts_one_character_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    label: str,
) -> None:
    values: dict[str, Any] = {
        "token": "signed-chain",
        "trusted_root_jwk": tmp_path / "root.jwk",
        "audience": "a",
        "nonce": "n",
        field: "x",
    }
    monkeypatch.setattr(ap2_module, "MAX_AP2_EXPECTED_VALUE_CHARS", 1)
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: (_ for _ in ()).throw(AP2Error(f"accepted {label}")),
    )

    with pytest.raises(AP2Error) as raised:
        inspect_ap2(**values)

    assert str(raised.value) == f"accepted {label}"


def test_inspector_passes_every_verification_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "root.jwk"
    sdk = _sdk({})
    public_jwk = {"public": "jwk"}
    root_key = object()
    payloads = [{"vct": "open"}, {"vct": "mandate.payment.1"}]
    chain = object()
    calls: dict[str, Any] = {}
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: sdk)
    monkeypatch.setattr(
        ap2_module,
        "_read_public_jwk_file",
        lambda path: calls.setdefault("read", path) and public_jwk,
    )

    def parse(value: Any, label: Any, actual_sdk: Any) -> Any:
        calls["parse"] = (value, label, actual_sdk)
        return root_key

    def verify_signed(*args: Any) -> Any:
        calls["signed"] = args
        return payloads

    def verify_typed(*args: Any, **kwargs: Any) -> Any:
        calls["typed"] = (args, kwargs)
        return chain

    monkeypatch.setattr(ap2_module, "_parse_public_jwk", parse)
    monkeypatch.setattr(ap2_module, "_verify_signed_chain", verify_signed)
    monkeypatch.setattr(ap2_module, "_verify_typed_chain", verify_typed)
    monkeypatch.setattr(
        ap2_module,
        "_inspection_result",
        lambda *args: calls.setdefault("summary", args) or "unreachable",
    )

    result = inspect_ap2(
        "signed-chain",
        root_path,
        "merchant",
        "nonce",
        mandate_type="payment",
        transaction_id="transaction",
        open_checkout_hash="open-hash",
    )

    assert result == calls["summary"]
    assert calls["read"] == root_path
    assert calls["parse"] == (public_jwk, f"trusted root {root_path}", sdk)
    assert calls["signed"] == ("signed-chain", root_key, "merchant", "nonce", sdk)
    assert calls["typed"] == (
        ("payment", payloads, sdk),
        {
            "transaction_id": "transaction",
            "open_checkout_hash": "open-hash",
            "checkout_hash": None,
        },
    )
    assert calls["summary"] == ("payment", payloads, chain, "merchant")


def test_inspector_passes_checkout_hash_to_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    payloads = [{"vct": "open"}, {"vct": "mandate.checkout.1"}]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}, payloads=payloads))
    monkeypatch.setattr(
        ap2_module,
        "_verify_typed_chain",
        lambda *_args, **kwargs: calls.append(kwargs) or SimpleNamespace(closed_mandate=object()),
    )
    monkeypatch.setattr(
        ap2_module,
        "_inspection_result",
        lambda *_args: SimpleNamespace(),
    )

    inspect_ap2(
        "signed-chain",
        root,
        "merchant",
        "nonce",
        checkout_hash="checkout-hash",
    )

    assert calls == [
        {
            "transaction_id": None,
            "open_checkout_hash": None,
            "checkout_hash": "checkout-hash",
        }
    ]


def test_inspector_reports_exact_constraint_and_summary_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    checkout_payloads = [
        {"vct": "mandate.checkout.open.1"},
        {"vct": "mandate.checkout.1"},
    ]
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, payloads=checkout_payloads),
    )

    with pytest.raises(AP2Error) as payment_only:
        inspect_ap2("token", root, "merchant", "nonce", mandate_type="checkout", transaction_id="x")
    assert str(payment_only.value) == (
        "--transaction-id and --open-checkout-hash require a payment mandate"
    )

    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))
    with pytest.raises(AP2Error) as checkout_only:
        inspect_ap2("token", root, "merchant", "nonce", checkout_hash="x")
    assert str(checkout_only.value) == "--checkout-hash requires a checkout mandate"

    monkeypatch.setattr(
        ap2_module,
        "_inspection_result",
        lambda *_args: (_ for _ in ()).throw(ValueError("secret")),
    )
    with pytest.raises(AP2Error) as summary:
        inspect_ap2("token", root, "merchant", "nonce")
    assert str(summary.value) == "verified AP2 mandate could not be summarized"


def test_config_preflight_passes_the_key_and_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProofConfig.model_validate(
        {
            "version": 1,
            "agent": {"url": "https://example.com"},
            "scenarios": [
                {
                    "name": "payment",
                    "message": "pay",
                    "expect": {"ap2": [_expectation().model_dump()]},
                }
            ],
        }
    )
    config.bind_contract_dir(tmp_path)
    sdk = _sdk({})
    key = {"public": "key"}
    calls: list[tuple[Any, Any, Any]] = []
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: sdk)
    monkeypatch.setattr(ap2_module, "_read_public_jwk", lambda *_args: key)
    monkeypatch.setattr(
        ap2_module,
        "_parse_public_jwk",
        lambda *args: calls.append(args) or object(),
    )

    ensure_ap2_sdk(config)

    assert calls == [(key, "trusted_root_jwk 'root.jwk'", sdk)]


def test_evaluation_collects_failures_after_missing_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))
    parts = (_part({"other": "value"}),)
    no_location = _expectation(source="message")
    missing_path = _expectation(path="/missing")

    failures = evaluate_ap2([no_location, missing_path, missing_path], parts, tmp_path)

    assert failures == [
        "no AP2 data matched source 'message'",
        "AP2 mandate path '/missing' was not found",
        "AP2 mandate path '/missing' was not found",
    ]


def test_redaction_continues_after_an_unmatched_expectation() -> None:
    part = _part({"token": "signed-chain"})
    redacted = redact_ap2(
        [
            _expectation(artifact_name="other", path="/token"),
            _expectation(artifact_name="payment", path="/token"),
        ],
        (part,),
    )

    assert redacted[0].value == {"token": ap2_module.REDACTED_AP2_MANDATE}
    assert redact_ap2([_expectation(artifact_name="other")], (part,))[0] is part


def test_empty_candidate_verification_has_a_stable_failure(tmp_path: Path) -> None:
    reference, failure = ap2_module._verify_any(_expectation(), [], tmp_path, _sdk({}))

    assert reference is None
    assert failure == "AP2 payment mandate verification failed: no mandate value was available"


def test_contract_verifier_passes_every_security_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expectation = _expectation(
        transaction_id="transaction",
        open_checkout_hash="open-hash",
    )
    sdk = _sdk({})
    public_jwk = {"public": "jwk"}
    root_key = object()
    payloads = [{"vct": "open"}, {"vct": "mandate.payment.1"}]
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        ap2_module,
        "_read_public_jwk",
        lambda *args: calls.setdefault("read", args) and public_jwk,
    )

    def parse(*args: Any) -> Any:
        calls["parse"] = args
        return root_key

    def signed(*args: Any) -> Any:
        calls["signed"] = args
        return payloads

    def typed(*args: Any, **kwargs: Any) -> None:
        calls["typed"] = (args, kwargs)

    monkeypatch.setattr(ap2_module, "_parse_public_jwk", parse)
    monkeypatch.setattr(ap2_module, "_verify_signed_chain", signed)
    monkeypatch.setattr(ap2_module, "_verify_typed_chain", typed)

    ap2_module._verify(expectation, "signed-chain", tmp_path, sdk)

    assert calls["read"] == ("root.jwk", tmp_path)
    assert calls["parse"] == (public_jwk, "trusted_root_jwk 'root.jwk'", sdk)
    assert calls["signed"] == ("signed-chain", root_key, "merchant", "nonce-1", sdk)
    assert calls["typed"] == (
        ("payment", payloads, sdk),
        {
            "transaction_id": "transaction",
            "open_checkout_hash": "open-hash",
            "checkout_hash": None,
        },
    )


@pytest.mark.parametrize("payloads", [[], ["not-an-object"]])
def test_signed_chain_verifier_rejects_every_invalid_shape(payloads: Any) -> None:
    with pytest.raises(AP2VerificationError) as raised:
        _verify_signed_chain("token", object(), "merchant", "nonce", _sdk({}, payloads=payloads))

    assert str(raised.value) == "expected a signed mandate chain"


def test_typed_verifier_passes_payloads_to_both_chain_parsers() -> None:
    payloads = [{"vct": "open"}, {"vct": "mandate.payment.1"}]
    payment_calls: dict[str, Any] = {}
    checkout_calls: dict[str, Any] = {}

    _verify_typed_chain(
        "payment",
        payloads,
        _sdk(payment_calls),
        transaction_id=None,
        open_checkout_hash=None,
        checkout_hash=None,
    )
    _verify_typed_chain(
        "checkout",
        payloads,
        _sdk(checkout_calls),
        transaction_id=None,
        open_checkout_hash=None,
        checkout_hash=None,
    )

    assert payment_calls["payment_payloads"] is payloads
    assert checkout_calls["checkout_payloads"] is payloads


def test_resolves_type_from_the_final_payload() -> None:
    payloads = [
        {"vct": "mandate.payment.1"},
        {"vct": "mandate.checkout.1"},
        {"vct": "mandate.payment.1"},
    ]

    assert ap2_module._resolve_mandate_type(payloads, "auto") == "payment"
    with pytest.raises(AP2VerificationError) as raised:
        ap2_module._resolve_mandate_type([{"vct": "unknown"}], "auto")
    assert str(raised.value) == "signed chain does not contain a supported AP2 mandate type"


def test_checkout_summary_supports_plain_string_status() -> None:
    chain = SimpleNamespace(
        closed_mandate=SimpleNamespace(checkout_jwt="jwt", checkout_hash="hash"),
        extract_parsed_checkout_object=lambda _jwt: SimpleNamespace(
            id="checkout",
            merchant=None,
            status="completed",
            currency="USD",
            line_items=[],
        ),
    )

    result = ap2_module._inspection_result(
        "checkout",
        [{"vct": "open"}, {"vct": "mandate.checkout.1"}],
        chain,
        "merchant",
    )

    assert result.details["checkout"]["status"] == "completed"


def test_loads_every_official_sdk_component(monkeypatch: pytest.MonkeyPatch) -> None:
    components: dict[str, Any] = {
        name: object()
        for name in (
            "mandate",
            "checkout",
            "payment",
            "checkout_receipt",
            "payment_receipt",
            "verify_jwt",
            "jwk",
            "hash",
        )
    }
    mandate = SimpleNamespace(MandateClient=components["mandate"], LOG_FILE_PATH="original")
    modules = {
        "ap2.sdk.mandate": mandate,
        "ap2.sdk.checkout_mandate_chain": SimpleNamespace(
            CheckoutMandateChain=components["checkout"]
        ),
        "ap2.sdk.payment_mandate_chain": SimpleNamespace(PaymentMandateChain=components["payment"]),
        "ap2.sdk.generated.checkout_receipt": SimpleNamespace(
            CheckoutReceipt=components["checkout_receipt"]
        ),
        "ap2.sdk.generated.payment_receipt": SimpleNamespace(
            PaymentReceipt=components["payment_receipt"]
        ),
        "ap2.sdk.jwt_helper": SimpleNamespace(verify_jwt=components["verify_jwt"]),
        "ap2.sdk.utils": SimpleNamespace(compute_sha256_b64url=components["hash"]),
        "jwcrypto.jwk": SimpleNamespace(JWK=components["jwk"]),
    }
    monkeypatch.setattr(ap2_module, "import_module", modules.__getitem__)

    sdk = _load_sdk()

    assert sdk == _SDK(
        mandate_client=components["mandate"],
        checkout_chain=components["checkout"],
        payment_chain=components["payment"],
        checkout_receipt=components["checkout_receipt"],
        payment_receipt=components["payment_receipt"],
        jwk=components["jwk"],
        compute_sha256_b64url=components["hash"],
        verify_jwt=components["verify_jwt"],
    )


def test_missing_sdk_error_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ap2_module,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("missing")),
    )

    with pytest.raises(AP2Error) as raised:
        _load_sdk()

    assert str(raised.value) == (
        "AP2 support requires the official AP2 v0.2.0 SDK; install "
        f"{ap2_module.AP2_INSTALL_REQUIREMENT!r} alongside a2a-proof"
    )


def test_serializes_public_jwk_compactly() -> None:
    calls: list[str] = []

    class JWK:
        @classmethod
        def from_json(cls, value: str) -> object:
            calls.append(value)
            return object()

    base = _sdk({})
    sdk = _SDK(
        mandate_client=base.mandate_client,
        checkout_chain=base.checkout_chain,
        payment_chain=base.payment_chain,
        checkout_receipt=base.checkout_receipt,
        payment_receipt=base.payment_receipt,
        jwk=JWK,
        compute_sha256_b64url=base.compute_sha256_b64url,
        verify_jwt=base.verify_jwt,
    )

    ap2_module._parse_public_jwk({"kty": "EC", "x": "x"}, "root", sdk)

    assert calls == ['{"kty":"EC","x":"x"}']


def test_trusted_root_readers_pass_exact_paths_labels_and_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_jwk(tmp_path / "root.jwk")
    calls: list[tuple[Path, Any]] = []
    original = ap2_module._read_public_jwk_path
    monkeypatch.setattr(
        ap2_module,
        "_read_public_jwk_path",
        lambda path, label: calls.append((path, label)) or {},
    )

    _read_public_jwk_file(root)
    _read_public_jwk("root.jwk", tmp_path)

    assert calls == [
        (root.resolve(), f"trusted root {root}"),
        (root.resolve(), "trusted_root_jwk 'root.jwk'"),
    ]
    monkeypatch.setattr(ap2_module, "_read_public_jwk_path", original)

    content = json.dumps({"kty": "EC", "crv": "P-256", "x": "eA", "y": "eQ"}).encode()
    root.write_bytes(content)
    monkeypatch.setattr(ap2_module, "MAX_JWK_BYTES", len(content))
    reads: list[int | None] = []
    original_open = Path.open

    class Stream(BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            reads.append(size)
            return super().read(size)

    def recorded_open(self: Path, mode: str = "r") -> Any:
        if self == root:
            return Stream(content)
        return original_open(self, mode)

    monkeypatch.setattr(Path, "open", recorded_open)

    assert ap2_module._read_public_jwk_path(root, "root")["kty"] == "EC"
    assert reads == [len(content) + 1]


def test_standalone_trust_root_requires_strict_resolution() -> None:
    calls: list[bool] = []

    class Root:
        def resolve(self, *, strict: bool = False) -> Path:
            calls.append(strict)
            raise OSError("missing")

        def __str__(self) -> str:
            return "root.jwk"

    root: Any = Root()

    with pytest.raises(AP2Error):
        _read_public_jwk_file(root)

    assert calls == [True]


def test_location_filters_and_json_pointer_escaping() -> None:
    media_expectation = _expectation(media_type="application/json")
    assert not ap2_module._matches_location(
        media_expectation,
        DataPartResult(
            source="artifact",
            artifact_name="payment",
            media_type="text/plain",
            value={},
        ),
    )
    assert ap2_module._location(_expectation()) == "the expectation"

    value = {"a/b": {"m~n": "escaped"}, "items": list(range(11))}
    assert _resolve_pointer(value, "/a~1b/m~0n") == "escaped"
    assert _resolve_pointer(value, "/items/10") == 10
    assert isinstance(_resolve_pointer(value, "/items/010"), ap2_module._Missing)

    replaced, updated = _replace_pointer(value, "/a~1b/m~0n", "redacted")
    assert replaced
    assert updated["a/b"]["m~n"] == "redacted"
    untouched, original = _replace_pointer(value, "/items/010", "redacted")
    assert not untouched
    assert original is value


def test_error_at_exact_limit_is_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ap2_module, "MAX_AP2_ERROR_CHARS", 4)

    assert _bounded_error(ValueError("1234")) == "1234"


def test_receipt_expectation_requires_a_matching_unique_mandate_id() -> None:
    mandate = _expectation(id="payment")
    receipt = _receipt_expectation()

    assert Expectation(ap2=[mandate, receipt]).ap2 == [mandate, receipt]

    with pytest.raises(ValueError, match="must name a mandate"):
        Expectation(ap2=[receipt])
    with pytest.raises(ValueError, match="must be unique"):
        Expectation(ap2=[mandate, mandate])
    with pytest.raises(ValueError, match="cannot bind to checkout"):
        Expectation(
            ap2=[
                _expectation(id="payment", type="checkout", checkout_hash="hash"),
                receipt,
            ]
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"source": "message", "artifact_name": "payment"}, "artifact_name"),
        ({"type": "checkout", "payment_id": "pay-1"}, "payment_id"),
        ({"type": "payment", "order_id": "order-1"}, "order_id"),
        ({"status": "Success", "error": "declined"}, "successful"),
        ({"path": "/bad~2escape"}, "RFC 6901"),
        ({"trusted_issuer_jwk": "/issuer.jwk"}, "relative"),
        ({"trusted_issuer_jwk": "bad\0key"}, "null byte"),
    ],
)
def test_rejects_invalid_receipt_expectation_shape(
    updates: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _receipt_expectation(**updates)


def test_evaluates_and_redacts_receipt_bound_to_verified_mandate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    _write_jwk(tmp_path / "issuer.jwk")
    calls: dict[str, Any] = {}
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk(calls))
    mandate = _expectation(id="payment")
    receipt = _receipt_expectation(
        issuer="processor.example",
        status="Success",
        payment_id="pay-1",
    )
    token = _receipt_token()
    part = _part(
        {
            "ap2.mandates.PaymentMandateSdJwt": "signed-chain",
            "payment_receipt": token,
        }
    )

    assert evaluate_ap2([mandate, receipt], (part,), tmp_path) == []
    public_jwk = json.loads((tmp_path / "issuer.jwk").read_text(encoding="utf-8"))
    assert calls["closed_mandate"] == "signed-chain"
    assert calls["jwk"] == public_jwk
    assert calls["receipt_verify"] == (token, public_jwk)
    assert calls["receipt_model"][0] == "payment"
    assert calls["receipt_model"][2] is True
    assert redact_ap2([mandate, receipt], (part,))[0].value == {
        "ap2.mandates.PaymentMandateSdJwt": ap2_module.REDACTED_AP2_MANDATE,
        "payment_receipt": ap2_module.REDACTED_AP2_RECEIPT,
    }

    assert evaluate_ap2([receipt, mandate], (part,), tmp_path) == []


@pytest.mark.parametrize(
    ("expectation", "payload", "message"),
    [
        (
            _receipt_expectation(status="Error", payment_id="pay-2"),
            {
                "status": "Success",
                "iss": "processor.example",
                "iat": 1,
                "reference": "computed-hash",
                "payment_id": "pay-1",
            },
            "receipt status does not match the expected value; "
            "receipt payment_id does not match the expected value",
        ),
        (
            _receipt_expectation(
                type="checkout",
                path="/checkout_receipt",
                status="Error",
                order_id="order-2",
                error="other-error",
            ),
            {
                "status": "Error",
                "iss": "merchant.example",
                "iat": 1,
                "reference": "computed-hash",
                "order_id": "order-1",
                "error": "declined",
            },
            "receipt order_id does not match the expected value; "
            "receipt error does not match the expected value",
        ),
    ],
)
def test_contract_receipt_applies_every_expected_field(
    tmp_path: Path,
    expectation: AP2ReceiptExpectation,
    payload: dict[str, Any],
    message: str,
) -> None:
    _write_jwk(tmp_path / "issuer.jwk")

    with pytest.raises(AP2VerificationError) as raised:
        _verify_receipt(
            expectation,
            _receipt_token(),
            "computed-hash",
            tmp_path,
            _sdk({}, receipt_payload=payload),
        )

    assert str(raised.value) == message


@pytest.mark.parametrize(
    ("receipt_payload", "message"),
    [
        ({"status": "Success"}, "does not bind to the verified mandate"),
        (
            {
                "status": "Success",
                "iss": "other.example",
                "iat": 1,
                "reference": "computed-hash",
                "payment_id": "pay-1",
            },
            "receipt iss does not match",
        ),
    ],
)
def test_reports_receipt_constraint_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_payload: dict[str, Any],
    message: str,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    _write_jwk(tmp_path / "issuer.jwk")
    sdk = _sdk({}, receipt_payload=receipt_payload)
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: sdk)
    receipt = _receipt_expectation(issuer="processor.example")
    part = _part(
        {
            "ap2.mandates.PaymentMandateSdJwt": "signed-chain",
            "payment_receipt": _receipt_token(),
        }
    )

    failures = evaluate_ap2([_expectation(id="payment"), receipt], (part,), tmp_path)

    assert len(failures) == 1
    assert message in failures[0]


def test_receipt_evaluation_reports_missing_invalid_and_unbound_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    _write_jwk(tmp_path / "issuer.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))
    mandate = _expectation(id="payment")
    receipt = _receipt_expectation()

    assert evaluate_ap2([mandate, receipt], (_part({}),), tmp_path) == [
        "AP2 mandate path '/ap2.mandates.PaymentMandateSdJwt' was not found",
        "AP2 receipt path '/payment_receipt' was not found",
    ]
    non_string = _part(
        {
            "ap2.mandates.PaymentMandateSdJwt": "signed-chain",
            "payment_receipt": {},
        }
    )
    assert (
        "receipt value is not a string"
        in evaluate_ap2([mandate, receipt], (non_string,), tmp_path)[0]
    )
    failed_mandate = _part(
        {
            "ap2.mandates.PaymentMandateSdJwt": 1,
            "payment_receipt": _receipt_token(),
        }
    )
    assert (
        "bound mandate 'payment' did not verify"
        in evaluate_ap2([mandate, receipt], (failed_mandate,), tmp_path)[1]
    )


def test_receipt_evaluation_uses_later_valid_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    _write_jwk(tmp_path / "issuer.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))
    expectations = [_expectation(id="payment"), _receipt_expectation()]
    parts = (
        _part(
            {
                "ap2.mandates.PaymentMandateSdJwt": "signed-chain",
                "payment_receipt": {},
            }
        ),
        _part({"payment_receipt": _receipt_token()}),
    )

    assert evaluate_ap2(expectations, parts, tmp_path) == []


def test_receipt_evaluation_reports_every_missing_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))
    expectations = [
        _expectation(id="payment"),
        _receipt_expectation(path="/missing-one"),
        _receipt_expectation(path="/missing-two"),
    ]
    part = _part({"ap2.mandates.PaymentMandateSdJwt": "signed-chain"})

    assert evaluate_ap2(expectations, (part,), tmp_path) == [
        "AP2 receipt path '/missing-one' was not found",
        "AP2 receipt path '/missing-two' was not found",
    ]


def test_receipt_evaluation_reports_every_unbound_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_jwk(tmp_path / "root.jwk")
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk({}))
    expectations = [
        _expectation(id="payment"),
        _receipt_expectation(path="/receipt-one"),
        _receipt_expectation(path="/receipt-two"),
    ]
    part = _part(
        {
            "ap2.mandates.PaymentMandateSdJwt": 1,
            "receipt-one": _receipt_token(),
            "receipt-two": _receipt_token(),
        }
    )

    failures = evaluate_ap2(expectations, (part,), tmp_path)

    assert len(failures) == 3
    assert failures[1:] == [
        "AP2 payment receipt verification failed: bound mandate 'payment' did not verify",
        "AP2 payment receipt verification failed: bound mandate 'payment' did not verify",
    ]


@pytest.mark.parametrize(
    ("token", "message"),
    [
        ("not-a-jwt", "compact JWT"),
        ("%%%.payload.signature", "invalid protected header"),
        (_receipt_token("none"), "must use ES256"),
    ],
)
def test_requires_an_es256_receipt_header(token: str, message: str) -> None:
    with pytest.raises(AP2VerificationError) as raised:
        _require_es256_header(token)
    expected = {
        "compact JWT": "receipt is not a compact JWT",
        "invalid protected header": "receipt has an invalid protected header",
        "must use ES256": "receipt protected header must use ES256",
    }
    assert str(raised.value) == expected[message]


def test_decodes_a_padded_es256_header() -> None:
    header = urlsafe_b64encode(b'{"alg":"ES256","kid":"ab"}').decode().rstrip("=")

    _require_es256_header(f"{header}.payload.signature")


def test_receipt_verifier_rejects_signature_schema_and_reference_failures() -> None:
    token = _receipt_token()
    with pytest.raises(AP2VerificationError, match="signature or payload"):
        _verify_receipt_token(
            token,
            object(),
            "payment",
            "computed-hash",
            _sdk({}, receipt_error=ValueError("bad signature")),
            issuer=None,
            status=None,
            payment_id=None,
            order_id=None,
            error_code=None,
        )

    checkout_payload = {
        "status": "Success",
        "iss": "merchant.example",
        "iat": 1_700_000_001,
        "reference": "computed-hash",
        "order_id": "order-1",
    }
    with pytest.raises(AP2VerificationError) as raised:
        _verify_receipt_token(
            token,
            object(),
            "checkout",
            "computed-hash",
            _sdk({}, receipt_payload=checkout_payload),
            issuer=None,
            status=None,
            payment_id=None,
            order_id="order-2",
            error_code=None,
        )
    assert str(raised.value) == "receipt order_id does not match the expected value"

    with pytest.raises(AP2VerificationError, match="signature or payload"):
        _verify_receipt_token(
            token,
            object(),
            "payment",
            "computed-hash",
            _sdk({}, receipt_payload=[]),
            issuer=None,
            status=None,
            payment_id=None,
            order_id=None,
            error_code=None,
        )
    with pytest.raises(AP2VerificationError, match="signature or payload"):
        _verify_receipt_token(
            token,
            object(),
            "payment",
            "computed-hash",
            _sdk({}, receipt_model_error=ValueError("bad schema")),
            issuer=None,
            status=None,
            payment_id=None,
            order_id=None,
            error_code=None,
        )
    with pytest.raises(AP2VerificationError, match="does not bind to the verified mandate"):
        _verify_receipt_token(
            token,
            object(),
            "payment",
            "other-hash",
            _sdk({}),
            issuer=None,
            status=None,
            payment_id=None,
            order_id=None,
            error_code=None,
        )


def test_inspects_payment_and_checkout_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _write_jwk(tmp_path / "issuer.jwk")
    public_jwk = json.loads(key.read_text(encoding="utf-8"))
    payment_payload = {
        "status": "Success",
        "iss": "processor.example",
        "iat": 1_700_000_000,
        "reference": "computed-hash",
        "payment_id": "pay-1",
        "psp_confirmation_id": "psp-1",
        "network_confirmation_id": "network-1",
    }
    payment_calls: dict[str, Any] = {}
    payment_sdk = _sdk(payment_calls, receipt_payload=payment_payload)
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: payment_sdk)

    payment = inspect_ap2_receipt(
        _receipt_token(),
        key,
        "computed-hash",
        receipt_type="payment",
        issuer="processor.example",
        status="Success",
        payment_id="pay-1",
    )

    assert payment_calls["jwk"] == public_jwk
    assert payment_calls["receipt_verify"] == (_receipt_token(), public_jwk)
    assert payment_calls["receipt_model"] == ("payment", payment_payload, True)
    assert payment.as_dict() == {
        "valid": True,
        "kind": "receipt",
        "type": "payment",
        "issuer": "processor.example",
        "status": "Success",
        "reference": "computed-hash",
        "checks": ["es256_signature", "payload_schema", "mandate_reference"],
        "details": {
            "issued_at": 1_700_000_000,
            "error": None,
            "error_description": None,
            "payment_id": "pay-1",
            "psp_confirmation_id": "psp-1",
            "network_confirmation_id": "network-1",
        },
    }

    checkout_payload = {
        "status": "Error",
        "iss": "merchant.example",
        "iat": 1_700_000_001,
        "reference": "computed-hash",
        "error": "declined",
        "error_description": "Payment declined",
        "order_id": "order-1",
    }
    checkout_calls: dict[str, Any] = {}
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk(checkout_calls, receipt_payload=checkout_payload),
    )
    checkout = inspect_ap2_receipt(
        _receipt_token(),
        key,
        "computed-hash",
        receipt_type="checkout",
        issuer="merchant.example",
        status="Error",
        order_id="order-1",
        error_code="declined",
    )
    assert checkout_calls["jwk"] == public_jwk
    assert checkout_calls["receipt_verify"] == (_receipt_token(), public_jwk)
    assert checkout_calls["receipt_model"] == ("checkout", checkout_payload, True)
    assert checkout.as_dict() == {
        "valid": True,
        "kind": "receipt",
        "type": "checkout",
        "issuer": "merchant.example",
        "status": "Error",
        "reference": "computed-hash",
        "checks": ["es256_signature", "payload_schema", "mandate_reference"],
        "details": {
            "issued_at": 1_700_000_001,
            "error": "declined",
            "error_description": "Payment declined",
            "order_id": "order-1",
        },
    }


@pytest.mark.parametrize(
    ("receipt_type", "payload", "expected", "message"),
    [
        (
            "payment",
            {
                "status": "Success",
                "iss": "processor.example",
                "iat": 1,
                "reference": "computed-hash",
                "payment_id": "pay-1",
            },
            {
                "issuer": "other.example",
                "status": "Error",
                "payment_id": "pay-2",
            },
            "receipt iss does not match the expected value; "
            "receipt status does not match the expected value; "
            "receipt payment_id does not match the expected value",
        ),
        (
            "checkout",
            {
                "status": "Error",
                "iss": "merchant.example",
                "iat": 1,
                "reference": "computed-hash",
                "order_id": "order-1",
                "error": "declined",
            },
            {"order_id": "order-2", "error_code": "other-error"},
            "receipt order_id does not match the expected value; "
            "receipt error does not match the expected value",
        ),
    ],
)
def test_public_receipt_inspector_applies_every_expected_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_type: Any,
    payload: dict[str, Any],
    expected: dict[str, Any],
    message: str,
) -> None:
    key = _write_jwk(tmp_path / "issuer.jwk")
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, receipt_payload=payload),
    )

    with pytest.raises(AP2VerificationError) as raised:
        inspect_ap2_receipt(
            _receipt_token(),
            key,
            "computed-hash",
            receipt_type=receipt_type,
            **expected,
        )

    assert str(raised.value) == message


def test_receipt_input_and_reference_helpers_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert read_ap2_receipt_token(BytesIO(b" receipt.jwt.value\n")) == "receipt.jwt.value"
    calls: dict[str, Any] = {}
    monkeypatch.setattr(ap2_module, "_load_sdk", lambda: _sdk(calls))
    assert ap2_mandate_reference("signed-chain") == "computed-hash"
    assert calls["closed_mandate"] == "signed-chain"
    assert calls["hashed"] == "closed.jwt"
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, closed_mandate_jwt=ValueError("bad chain")),
    )
    with pytest.raises(AP2VerificationError, match="could not be computed"):
        ap2_mandate_reference("signed-chain")
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, closed_mandate_jwt=42),
    )
    with pytest.raises(AP2VerificationError, match="could not be computed"):
        ap2_mandate_reference("signed-chain")
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, hash_result=42),
    )
    with pytest.raises(AP2VerificationError, match="could not be computed"):
        ap2_mandate_reference("signed-chain")
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, closed_mandate_jwt="", hash_result=None),
    )
    with pytest.raises(AP2VerificationError, match="could not be computed"):
        ap2_mandate_reference("signed-chain")
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: _sdk({}, hash_result=None),
    )
    with pytest.raises(AP2VerificationError, match="could not be computed"):
        ap2_mandate_reference("signed-chain")


@pytest.mark.parametrize(
    ("receipt_type", "updates", "message"),
    [
        (
            "payment",
            {"reference": ""},
            "reference must contain between 1 and 1000 characters",
        ),
        (
            "payment",
            {"order_id": ""},
            "order ID must contain between 1 and 1000 characters",
        ),
        ("checkout", {"payment_id": "pay-1"}, "--payment-id requires a payment receipt"),
        ("payment", {"order_id": "order-1"}, "--order-id requires a checkout receipt"),
        (
            "payment",
            {"status": "Success", "error_code": "declined"},
            "--error cannot be asserted for a successful receipt",
        ),
    ],
)
def test_rejects_invalid_receipt_inspection_constraints(
    receipt_type: Any,
    updates: dict[str, Any],
    message: str,
) -> None:
    values = {
        "reference": "hash",
        "issuer": None,
        "status": None,
        "payment_id": None,
        "order_id": None,
        "error_code": None,
    }
    values.update(updates)
    with pytest.raises(AP2Error) as raised:
        _validate_receipt_inputs(receipt_type, **values)
    assert str(raised.value) == message


@pytest.mark.parametrize(
    (
        "receipt_type",
        "reference",
        "issuer",
        "status",
        "payment_id",
        "order_id",
        "error_code",
    ),
    [
        ("payment", "", None, None, None, None, None),
        ("payment", "hash", "", None, None, None, None),
        ("payment", "hash", None, "", None, None, None),
        ("checkout", "hash", None, None, "pay-1", None, None),
        ("payment", "hash", None, None, None, "order-1", None),
        ("payment", "hash", None, "Success", None, None, "declined"),
    ],
)
def test_public_receipt_inspector_validates_before_loading_sdk(
    monkeypatch: pytest.MonkeyPatch,
    receipt_type: Any,
    reference: str,
    issuer: str | None,
    status: Any,
    payment_id: str | None,
    order_id: str | None,
    error_code: str | None,
) -> None:
    monkeypatch.setattr(
        ap2_module,
        "_load_sdk",
        lambda: pytest.fail("invalid input reached the AP2 SDK"),
    )

    with pytest.raises(AP2Error):
        inspect_ap2_receipt(
            _receipt_token(),
            Path("unused.jwk"),
            reference,
            receipt_type=receipt_type,
            issuer=issuer,
            status=status,
            payment_id=payment_id,
            order_id=order_id,
            error_code=error_code,
        )
