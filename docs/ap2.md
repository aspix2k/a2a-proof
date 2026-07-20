# AP2 mandate contracts

`a2a-proof` can inspect AP2 v0.2.0 mandates offline or verify them in A2A response contracts. Both
paths use Google's official SDK and an explicit public trust root supplied by the caller.

## Install

The official AP2 v0.2.0 Python SDK is not published on PyPI. Install the pinned source commit next
to `a2a-proof`:

```console
uv tool install a2a-proof \
  --with 'ap2 @ git+https://github.com/google-agentic-commerce/AP2.git@b4587ac1d055888a73b4b21750973cffba961793'
```

Do not substitute the unrelated `ap2` package from PyPI. `a2a-proof check` fails with the exact
requirement when the official SDK is absent.

## Inspect offline

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
value, echoed, or included in errors:

```console
cat payment-mandate.txt | a2a-proof ap2 inspect - \
  --trust-root user-public.jwk \
  --audience merchant \
  --nonce "$AP2_PAYMENT_NONCE" \
  --format json
```

A valid mandate exits `0`, a failed cryptographic or semantic verification exits `1`, and an input,
key, or runtime setup error exits `2`. JSON failures have the stable shape
`{"valid": false, "error": "..."}`.

## Response contracts

### Payment mandate

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

`transaction_id` and `open_checkout_hash` are optional expected values. The latter is required to
evaluate an AP2 `payment.reference` constraint when one is present.

### Checkout mandate

```yaml
expect:
  ap2:
    type: checkout
    source: artifact
    trusted_root_jwk: keys/user-public.jwk
    audience: merchant
    nonce: ${AP2_CHECKOUT_NONCE}
    checkout_hash: ${CHECKOUT_HASH}
```

Payment and checkout assertions default to the official
`/ap2.mandates.PaymentMandateSdJwt` and `/ap2.mandates.CheckoutMandateSdJwt` data paths. Set `path`
to another RFC 6901 JSON Pointer when an agent wraps the token differently. `source`,
`artifact_name`, and `media_type` narrow the matching response parts; one matching token must pass.

## Verification boundary

The assertion verifies chain signatures, delegation bindings, terminal `aud` and `nonce`, time
claims, AP2 payload types, and mandate constraints. Checkout assertions also recompute the hash of
the signed `checkout_jwt`. Payment assertions can bind the result to expected checkout identifiers.

`trusted_root_jwk` must point to a public P-256 JWK inside the contract directory. The file is
limited to 16 KiB; private key fields are rejected. Keys are never loaded from token headers,
Agent Cards, or remote URLs.

Selected mandate tokens are replaced with `[REDACTED: AP2 mandate]` before reports and evidence are
written. Other data in the same response part remains available for ordinary assertions and
diagnostics.
