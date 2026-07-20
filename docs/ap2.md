# AP2 contracts

`a2a-proof` verifies AP2 v0.2.0 mandate chains and signed payment or checkout receipts. Both paths
use Google's official SDK and caller-supplied public trust keys.

## Install

The official AP2 v0.2.0 Python SDK is not published on PyPI. Install the pinned source commit next
to `a2a-proof`:

```console
uv tool install a2a-proof \
  --with 'ap2 @ git+https://github.com/google-agentic-commerce/AP2.git@b4587ac1d055888a73b4b21750973cffba961793'
```

Do not substitute the unrelated `ap2` package from PyPI. `a2a-proof check` fails with the exact
requirement when the official SDK is absent.

## Inspect a mandate

The repository includes a synthetic payment mandate signed by a discarded demo key:

```console
$ a2a-proof ap2 inspect examples/ap2/payment-mandate.txt \
    --trust-root examples/ap2/payment-root.jwk \
    --audience merchant.example \
    --nonce demo-payment-nonce \
    --transaction-id demo-checkout-hash
AP2 PAYMENT — VALID
  Chain          2 signed mandates
  Audience       merchant.example
  Checks         7 passed
  Payee          Demo Shop (demo-shop)
  Amount         1999 USD
  Transaction    demo-checkout-hash
  Instrument     card
```

Use `-` or omit the file to read from standard input. The token is never accepted as a command-line
value, echoed, or included in errors.

## Inspect a receipt

The synthetic payment receipt is signed by another discarded demo key. The command verifies its
ES256 signature, AP2 payload type, expected fields, and reference to the closed mandate above:

```console
$ a2a-proof ap2 inspect-receipt examples/ap2/payment-receipt.txt \
    --issuer-key examples/ap2/processor.jwk \
    --type payment \
    --mandate examples/ap2/payment-mandate.txt \
    --issuer processor.example \
    --status Success \
    --payment-id demo-payment-1
AP2 PAYMENT RECEIPT — VALID
  Issuer                processor.example
  Status                Success
  Reference             6wleZJEJwlObITPI7WXciJg77mVx2eXpJEw9mDGY9LU
  Checks                3 passed
  Payment               demo-payment-1
  PSP confirmation      demo-psp-1
  Network confirmation  demo-network-1
```

Use `--reference HASH` instead of `--mandate FILE` when the expected reference is already known.
Exactly one is required. Receipt tokens follow the same bounded stdin/file and no-echo rules as
mandates.

A valid token exits `0`, a failed cryptographic or semantic verification exits `1`, and an input,
key, or runtime setup error exits `2`. JSON failures have the stable shape
`{"valid": false, "error": "..."}`.

## Response contracts

### Mandates

```yaml
agent:
  url: https://agent.example.com
  extensions:
    - https://github.com/google-agentic-commerce/ap2/v1

scenarios:
  - name: authorized payment
    message: Complete the checkout
    expect:
      ap2:
        type: payment
        source: artifact
        trusted_root_jwk: keys/user-public.jwk
        audience: merchant
        nonce: ${AP2_PAYMENT_NONCE}
        transaction_id: ${CHECKOUT_HASH}
        open_checkout_hash: ${OPEN_CHECKOUT_HASH}
```

Payment assertions may check `transaction_id` and `open_checkout_hash`. Checkout assertions use
`type: checkout` and may check `checkout_hash`. Both default to the official
`/ap2.mandates.PaymentMandateSdJwt` and `/ap2.mandates.CheckoutMandateSdJwt` data paths.

### Receipt bound to a mandate

Give the mandate assertion an `id`, then bind the receipt to it in the same response turn:

```yaml
expect:
  ap2:
    - id: payment
      type: payment
      trusted_root_jwk: keys/user-public.jwk
      audience: merchant
      nonce: ${AP2_PAYMENT_NONCE}
    - kind: receipt
      type: payment
      binds_to: payment
      trusted_issuer_jwk: keys/processor-public.jwk
      path: /payment_receipt
      issuer: processor.example
      status: Success
```

The receipt is accepted only when the referenced mandate assertion verifies first. `a2a-proof`
extracts the closed JWT from that exact chain, computes its AP2 reference, and compares it with the
signed receipt. No hash needs to be copied into the contract.

`path` is required because implementations may wrap signed receipts differently. `source`,
`artifact_name`, and `media_type` narrow the matching response parts. Payment receipts may assert
`payment_id`; checkout receipts may assert `order_id`; error receipts may assert `error`.

## Verification boundary

Mandate assertions verify chain signatures, delegation bindings, terminal `aud` and `nonce`, time
claims, AP2 payload types, and constraints. Checkout assertions also recompute the hash of the
signed `checkout_jwt`.

Receipt assertions require an ES256 protected header, verify the issuer signature and official AP2
success/error payload type, and bind `reference` to a verified closed mandate. Optional issuer,
status, and identifier checks compare exact values without exposing mismatched receipt contents in
diagnostics.

`trusted_root_jwk` and `trusted_issuer_jwk` must point to public P-256 JWKs inside the contract
directory. Files are limited to 16 KiB; private key fields are rejected. Keys are never loaded from
token headers, Agent Cards, or remote URLs.

Selected mandate and receipt tokens are replaced before reports and evidence are written. Other
data in the same response part remains available for ordinary assertions and diagnostics.
