from __future__ import annotations

import json
import os
from importlib import import_module
from pathlib import Path

import pytest
from click.testing import CliRunner

from a2a_proof.ap2 import AP2VerificationError, evaluate_ap2, inspect_ap2
from a2a_proof.cli import main
from a2a_proof.models import AP2MandateExpectation, DataPartResult


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
