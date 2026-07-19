from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import a2a_proof.ap2 as ap2_module
from a2a_proof.ap2 import (
    _SDK,
    AP2Error,
    _bounded_error,
    _load_sdk,
    _read_public_jwk,
    _replace_pointer,
    _resolve_pointer,
    ensure_ap2_sdk,
    evaluate_ap2,
    has_ap2_expectations,
    redact_ap2,
    validate_config_ap2,
)
from a2a_proof.models import AP2MandateExpectation, DataPartResult, ProofConfig


def _write_jwk(path: Path) -> Path:
    path.write_text(
        json.dumps({"kty": "EC", "crv": "P-256", "x": "eA", "y": "eQ", "kid": "root"}),
        encoding="utf-8",
    )
    return path


def _expectation(**updates: Any) -> AP2MandateExpectation:
    return AP2MandateExpectation(
        type="payment",
        trusted_root_jwk="root.jwk",
        audience="merchant",
        nonce="nonce-1",
        **updates,
    )


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
    jwk_error: Exception | None = None,
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
            return [{"open": True}, {"closed": True}] if payloads is None else payloads

    class PaymentChain:
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

    def compute_hash(value: str) -> str:
        if hash_error is not None:
            raise hash_error
        calls.setdefault("hashed", value)
        return "computed-hash"

    return _SDK(
        mandate_client=MandateClient,
        checkout_chain=CheckoutChain,
        payment_chain=PaymentChain,
        jwk=JWK,
        compute_sha256_b64url=compute_hash,
    )


def _part(value: Any, **updates: Any) -> DataPartResult:
    return DataPartResult(
        source="artifact",
        artifact_name="payment",
        media_type="application/json",
        value=value,
        **updates,
    )


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

    assert message in failure
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
        lambda path, root: calls.append((path, root)) or {},
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
        lambda _path, _root: {"kty": "EC", "crv": "P-256", "x": "eA", "y": "eQ"},
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
