from __future__ import annotations

import json
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from a2a_proof.ap2 import (
    AP2VerificationError,
    evaluate_ap2,
    inspect_ap2,
    inspect_ap2_receipt,
)
from a2a_proof.cli import main
from a2a_proof.models import AP2MandateExpectation, AP2ReceiptExpectation, DataPartResult


def _tamper_jwt_signature(token: str) -> str:
    header, payload, signature = token.split(".")
    decoded = bytearray(urlsafe_b64decode(f"{signature}==="))
    decoded[0] ^= 1
    tampered = urlsafe_b64encode(decoded).decode().rstrip("=")
    return f"{header}.{payload}.{tampered}"


def _verify_payment_receipt(
    tmp_path: Path,
    jwk_module: Any,
    utils_module: Any,
    client: Any,
    payment_chain: str,
    payment_expectation: AP2MandateExpectation,
) -> None:
    receipt_module = import_module("ap2.sdk.generated.payment_receipt")
    jwt_module = import_module("ap2.sdk.jwt_helper")
    processor_key = jwk_module.JWK.generate(kty="EC", crv="P-256")
    processor_public = tmp_path / "processor.jwk"
    processor_public.write_text(processor_key.export_public(), encoding="utf-8")
    reference = utils_module.compute_sha256_b64url(client.get_closed_mandate_jwt(payment_chain))
    receipt = receipt_module.PaymentReceipt(
        status="Success",
        iss="processor.example",
        iat=1_700_000_000,
        reference=reference,
        payment_id="payment-1",
        psp_confirmation_id="psp-1",
        network_confirmation_id="network-1",
    )
    receipt_jwt = jwt_module.create_jwt(
        {"alg": "ES256"},
        receipt.model_dump(),
        processor_key,
    )
    receipt_expectation = AP2ReceiptExpectation(
        kind="receipt",
        type="payment",
        binds_to="payment",
        trusted_issuer_jwk="processor.jwk",
        path="/payment_receipt",
        issuer="processor.example",
        status="Success",
        payment_id="payment-1",
    )
    part = DataPartResult(
        source="artifact",
        value={
            "ap2.mandates.PaymentMandateSdJwt": payment_chain,
            "payment_receipt": receipt_jwt,
        },
    )

    assert evaluate_ap2([payment_expectation, receipt_expectation], (part,), tmp_path) == []
    inspection = inspect_ap2_receipt(
        receipt_jwt,
        processor_public,
        reference,
        receipt_type="payment",
        issuer="processor.example",
        status="Success",
        payment_id="payment-1",
    )
    assert inspection.details["psp_confirmation_id"] == "psp-1"


def _verify_checkout_receipt(
    tmp_path: Path,
    jwk_module: Any,
    utils_module: Any,
    client: Any,
    checkout_chain: str,
    checkout_expectation: AP2MandateExpectation,
) -> None:
    receipt_module = import_module("ap2.sdk.generated.checkout_receipt")
    jwt_module = import_module("ap2.sdk.jwt_helper")
    merchant_key = jwk_module.JWK.generate(kty="EC", crv="P-256")
    (tmp_path / "merchant.jwk").write_text(merchant_key.export_public(), encoding="utf-8")
    reference = utils_module.compute_sha256_b64url(client.get_closed_mandate_jwt(checkout_chain))
    receipt = receipt_module.CheckoutReceipt(
        status="Error",
        iss="merchant.example",
        iat=1_700_000_001,
        reference=reference,
        error="payment_declined",
        error_description="Payment declined",
    )
    receipt_jwt = jwt_module.create_jwt(
        {"alg": "ES256"},
        receipt.model_dump(),
        merchant_key,
    )
    receipt_expectation = AP2ReceiptExpectation(
        kind="receipt",
        type="checkout",
        binds_to="checkout",
        trusted_issuer_jwk="merchant.jwk",
        path="/checkout_receipt",
        issuer="merchant.example",
        status="Error",
        error="payment_declined",
    )
    value = {
        "ap2.mandates.CheckoutMandateSdJwt": checkout_chain,
        "checkout_receipt": receipt_jwt,
    }

    assert (
        evaluate_ap2(
            [checkout_expectation, receipt_expectation],
            (DataPartResult(source="artifact", value=value),),
            tmp_path,
        )
        == []
    )
    value["checkout_receipt"] = _tamper_jwt_signature(receipt_jwt)
    assert (
        "signature or payload is invalid"
        in evaluate_ap2(
            [checkout_expectation, receipt_expectation],
            (DataPartResult(source="artifact", value=value),),
            tmp_path,
        )[0]
    )


def test_official_ap2_v020_payment_and_checkout_chains(tmp_path: Path) -> None:
    mandate_module = pytest.importorskip("ap2.sdk.mandate")
    mandate_module.LOG_FILE_PATH = os.devnull
    jwk_module = import_module("jwcrypto.jwk")
    open_payment_module = import_module("ap2.sdk.generated.open_payment_mandate")
    payment_module = import_module("ap2.sdk.generated.payment_mandate")
    open_checkout_module = import_module("ap2.sdk.generated.open_checkout_mandate")
    checkout_module = import_module("ap2.sdk.generated.checkout_mandate")
    amount_module = import_module("ap2.sdk.generated.types.amount")
    merchant_module = import_module("ap2.sdk.generated.types.merchant")
    instrument_module = import_module("ap2.sdk.generated.types.payment_instrument")
    utils_module = import_module("ap2.sdk.utils")

    root_key = jwk_module.JWK.generate(kty="EC", crv="P-256")
    holder_key = jwk_module.JWK.generate(kty="EC", crv="P-256")
    trusted_root = tmp_path / "root.jwk"
    trusted_root.write_text(root_key.export_public(), encoding="utf-8")
    cnf = {"jwk": json.loads(holder_key.export_public())}
    client = mandate_module.MandateClient()

    open_payment = open_payment_module.OpenPaymentMandate(
        constraints=[open_payment_module.AmountRange(currency="USD", max=2_000)],
        cnf=cnf,
    )
    payment_root = client.create(payloads=[open_payment], issuer_key=root_key)
    payment_chain = client.present(
        holder_key=holder_key,
        mandate_token=payment_root,
        payloads=[
            payment_module.PaymentMandate(
                transaction_id="checkout-hash",
                payee=merchant_module.Merchant(id="shop-1", name="Shop"),
                payment_amount=amount_module.Amount(amount=1_000, currency="USD"),
                payment_instrument=instrument_module.PaymentInstrument(
                    id="card-1",
                    type="card",
                ),
            )
        ],
        aud="merchant",
        nonce="payment-nonce",
    )
    payment_expectation = AP2MandateExpectation(
        id="payment",
        type="payment",
        trusted_root_jwk="root.jwk",
        audience="merchant",
        nonce="payment-nonce",
        transaction_id="checkout-hash",
    )
    payment_part = DataPartResult(
        source="artifact",
        value={"ap2.mandates.PaymentMandateSdJwt": payment_chain},
    )

    assert evaluate_ap2([payment_expectation], (payment_part,), tmp_path) == []
    payment_inspection = inspect_ap2(
        payment_chain,
        trusted_root,
        "merchant",
        "payment-nonce",
        transaction_id="checkout-hash",
    )
    assert payment_inspection.type == "payment"
    assert payment_inspection.details["payee"] == {"id": "shop-1", "name": "Shop"}

    _verify_payment_receipt(
        tmp_path,
        jwk_module,
        utils_module,
        client,
        payment_chain,
        payment_expectation,
    )

    tampered = f"{payment_chain[:-1]}{'A' if payment_chain[-1] != 'A' else 'B'}"
    tampered_part = payment_part.model_copy(
        update={"value": {"ap2.mandates.PaymentMandateSdJwt": tampered}}
    )
    assert evaluate_ap2([payment_expectation], (tampered_part,), tmp_path)[0].startswith(
        "AP2 payment mandate verification failed:"
    )
    with pytest.raises(AP2VerificationError, match="failed signature"):
        inspect_ap2(tampered, trusted_root, "merchant", "payment-nonce")

    checkout_payload = {
        "id": "checkout-1",
        "merchant": {"id": "shop-1", "name": "Shop"},
        "line_items": [],
        "status": "incomplete",
        "currency": "USD",
        "totals": [],
        "links": [],
    }
    encoded = utils_module.b64url_encode(
        json.dumps(checkout_payload, separators=(",", ":")).encode()
    )
    checkout_jwt = f"header.{encoded}.signature"
    checkout_hash = utils_module.compute_sha256_b64url(checkout_jwt)
    open_checkout = open_checkout_module.OpenCheckoutMandate(constraints=[], cnf=cnf)
    checkout_root = client.create(payloads=[open_checkout], issuer_key=root_key)
    checkout_chain = client.present(
        holder_key=holder_key,
        mandate_token=checkout_root,
        payloads=[
            checkout_module.CheckoutMandate(
                checkout_jwt=checkout_jwt,
                checkout_hash=checkout_hash,
            )
        ],
        aud="merchant",
        nonce="checkout-nonce",
    )
    checkout_expectation = AP2MandateExpectation(
        id="checkout",
        type="checkout",
        trusted_root_jwk="root.jwk",
        audience="merchant",
        nonce="checkout-nonce",
        checkout_hash=checkout_hash,
    )
    checkout_part = DataPartResult(
        source="artifact",
        value={"ap2.mandates.CheckoutMandateSdJwt": checkout_chain},
    )

    assert evaluate_ap2([checkout_expectation], (checkout_part,), tmp_path) == []
    checkout_inspection = inspect_ap2(
        checkout_chain,
        trusted_root,
        "merchant",
        "checkout-nonce",
        checkout_hash=checkout_hash,
    )
    assert checkout_inspection.type == "checkout"
    assert checkout_inspection.details["checkout"]["id"] == "checkout-1"

    _verify_checkout_receipt(
        tmp_path,
        jwk_module,
        utils_module,
        client,
        checkout_chain,
        checkout_expectation,
    )


def test_documented_ap2_inspector_example() -> None:
    pytest.importorskip("ap2.sdk.mandate")
    root = Path(__file__).parents[1]

    result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect",
            str(root / "examples/ap2/payment-mandate.txt"),
            "--trust-root",
            str(root / "examples/ap2/payment-root.jwk"),
            "--audience",
            "merchant.example",
            "--nonce",
            "demo-payment-nonce",
            "--transaction-id",
            "demo-checkout-hash",
        ],
    )

    assert result.exit_code == 0
    assert "AP2 PAYMENT — VALID" in result.output
    assert "Demo Shop (demo-shop)" in result.output
    assert "1999 USD" in result.output

    receipt_result = CliRunner().invoke(
        main,
        [
            "ap2",
            "inspect-receipt",
            str(root / "examples/ap2/payment-receipt.txt"),
            "--issuer-key",
            str(root / "examples/ap2/processor.jwk"),
            "--type",
            "payment",
            "--mandate",
            str(root / "examples/ap2/payment-mandate.txt"),
            "--issuer",
            "processor.example",
            "--status",
            "Success",
            "--payment-id",
            "demo-payment-1",
        ],
    )

    assert receipt_result.exit_code == 0
    assert "AP2 PAYMENT RECEIPT — VALID" in receipt_result.output
    assert "demo-payment-1" in receipt_result.output
