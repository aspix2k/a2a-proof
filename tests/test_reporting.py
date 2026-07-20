from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from rich.console import Console

from a2a_proof.ap2 import AP2Inspection, AP2ReceiptInspection
from a2a_proof.models import (
    CardResult,
    DiffCheck,
    DiffResult,
    LatencyResult,
    ScenarioResult,
    SuiteResult,
    TrialResult,
    TurnResult,
)
from a2a_proof.reporting import (
    _diagnostic,
    _duration,
    render_ap2_invalid,
    render_ap2_invalid_json,
    render_ap2_json,
    render_ap2_receipt_invalid,
    render_ap2_receipt_terminal,
    render_ap2_terminal,
    render_diff_json,
    render_diff_terminal,
    render_json,
    render_junit,
    render_terminal,
)


def _result(*, passed: bool) -> SuiteResult:
    turn = TurnResult(
        index=1,
        passed=passed,
        state="completed",
        duration_ms=5,
        text="response\x1b[31m",
        failures=[] if passed else ["missing\x1b[31m value"],
    )
    trial = TrialResult(index=1, passed=passed, duration_ms=5, turns=[turn])
    scenario = ScenarioResult(
        name="scenario\x1b[31m",
        passed=passed,
        passed_trials=int(passed),
        required_trials=1,
        trials=[trial],
    )
    return SuiteResult(passed=passed, duration_ms=5, scenarios=[scenario])


def test_renders_ap2_payment_in_terminal_and_json() -> None:
    result = AP2Inspection(
        type="payment",
        chain_length=2,
        audience="merchant\x1b[31m",
        checks=("signature", "nonce"),
        details={
            "transaction_id": "checkout-[red]hash",
            "payee": {"id": "shop-1", "name": "[red]Shop"},
            "amount": {"minor_units": 1_000, "currency": "USD"},
            "payment_instrument_type": "card",
        },
    )
    console = Console(record=True, color_system=None, width=100)

    render_ap2_terminal(result, console)

    output = console.export_text()
    assert "AP2 PAYMENT — VALID" in output
    assert "[red]Shop (shop-1)" in output
    assert "checkout-[red]hash" in output
    assert "\x1b" not in output
    assert json.loads(render_ap2_json(result))["details"]["transaction_id"] == (
        "checkout-[red]hash"
    )


def test_renders_ap2_checkout_without_merchant() -> None:
    result = AP2Inspection(
        type="checkout",
        chain_length=2,
        audience="merchant",
        checks=("signature", "checkout_hash_binding"),
        details={
            "checkout_hash": "hash",
            "checkout": {
                "id": "checkout-1",
                "merchant": None,
                "status": "completed",
                "currency": "USD",
                "line_items": 2,
            },
        },
    )
    console = Console(record=True, color_system=None, width=100)

    render_ap2_terminal(result, console)

    output = console.export_text()
    assert "AP2 CHECKOUT — VALID" in output
    assert "checkout-1" in output
    assert "Merchant" not in output
    assert "2" in output

    result.details["checkout"]["merchant"] = {"id": "shop-1", "name": "Shop"}
    merchant_console = Console(record=True, color_system=None, width=100)
    render_ap2_terminal(result, merchant_console)
    assert "Shop (shop-1)" in merchant_console.export_text()


def test_renders_ap2_invalid_without_control_characters() -> None:
    console = Console(record=True, color_system=None, width=100)

    render_ap2_invalid("bad\x1b[31m signature", console)

    assert console.export_text() == "AP2 MANDATE — INVALID\nReason: bad[31m signature\n"
    assert json.loads(render_ap2_invalid_json("bad\x1b[31m signature")) == {
        "valid": False,
        "error": "bad[31m signature",
    }


def test_renders_payment_and_error_checkout_receipts() -> None:
    payment = AP2ReceiptInspection(
        type="payment",
        issuer="processor\x1b[31m.example",
        status="Success",
        reference="mandate-hash",
        checks=("signature", "schema", "reference"),
        details={
            "issued_at": 1_700_000_000,
            "error": None,
            "error_description": None,
            "payment_id": "pay-1",
            "psp_confirmation_id": "psp-1",
            "network_confirmation_id": "network-1",
        },
    )
    console = Console(record=True, color_system=None, width=100)

    render_ap2_receipt_terminal(payment, console)

    output = console.export_text()
    assert "AP2 PAYMENT RECEIPT — VALID" in output
    assert "processor[31m.example" in output
    assert "pay-1" in output
    assert "network-1" in output
    assert "\x1b" not in output
    assert json.loads(render_ap2_json(payment))["reference"] == "mandate-hash"

    checkout = AP2ReceiptInspection(
        type="checkout",
        issuer="merchant.example",
        status="Error",
        reference="checkout-hash",
        checks=("signature", "schema", "reference"),
        details={
            "issued_at": 1_700_000_001,
            "error": "declined",
            "error_description": "Payment declined",
            "order_id": None,
        },
    )
    checkout_console = Console(record=True, color_system=None, width=100)
    render_ap2_receipt_terminal(checkout, checkout_console)
    assert "declined" in checkout_console.export_text()
    assert "Order" not in checkout_console.export_text()

    checkout.details["order_id"] = "order-1"
    order_console = Console(record=True, color_system=None, width=100)
    render_ap2_receipt_terminal(checkout, order_console)
    assert "order-1" in order_console.export_text()

    payment.details["psp_confirmation_id"] = None
    payment.details["network_confirmation_id"] = None
    no_confirmation_console = Console(record=True, color_system=None, width=100)
    render_ap2_receipt_terminal(payment, no_confirmation_console)
    assert "confirmation" not in no_confirmation_console.export_text()


def test_renders_invalid_receipt_without_control_characters() -> None:
    console = Console(record=True, color_system=None, width=100)

    render_ap2_receipt_invalid("bad\x1b[31m receipt", console)

    assert console.export_text() == "AP2 RECEIPT — INVALID\nReason: bad[31m receipt\n"
    assert json.loads(render_ap2_invalid_json("bad\x1b[31m receipt")) == {
        "valid": False,
        "error": "bad[31m receipt",
    }


def test_renders_terminal_without_control_characters() -> None:
    console = Console(record=True, color_system=None, width=100)

    render_terminal(_result(passed=False), console, verbose=True)

    output = console.export_text()
    assert "FAIL" in output
    assert "missing[31m value" in output
    assert "\x1b" not in output


def test_indents_multiline_verbose_responses() -> None:
    console = Console(record=True, color_system=None, width=100)
    result = _result(passed=False)
    result.scenarios[0].trials[0].turns[0].text = "first\nsecond"

    render_terminal(result, console, verbose=True)

    assert "  response: first\n            second" in console.export_text()


def test_renders_machine_readable_json() -> None:
    result = _result(passed=True)
    result.agent_card_sha256 = "private-evidence-metadata"

    rendered = render_json(result)

    assert '"passed": true' in rendered
    assert '"scenario' in rendered
    assert "private-evidence-metadata" not in rendered


def test_renders_diff_in_terminal_and_json() -> None:
    baseline = _result(passed=False)
    candidate = _result(passed=True)
    result = DiffResult(
        passed=True,
        baseline=baseline,
        candidate=candidate,
        checks=[
            DiffCheck(
                name="improved\x1b[31m",
                baseline="failed",
                candidate="passed",
                change="improvement",
            ),
            DiffCheck(
                name="regressed",
                baseline="passed",
                candidate="failed",
                change="regression",
            ),
            DiffCheck(
                name="blocked",
                baseline="failed",
                candidate="not_run",
                change="changed",
            ),
            DiffCheck(
                name="stable",
                baseline="passed",
                candidate="passed",
                change="unchanged",
            ),
        ],
    )
    console = Console(record=True, color_system=None, width=100)

    render_diff_terminal(result, console)

    output = console.export_text()
    assert "improved[31m" in output
    assert "Candidate passed; 1 regressions, 1 improvements." in output
    assert '"change": "improvement"' in render_diff_json(result)

    failed_candidate = _result(passed=False)
    failed_candidate.card = CardResult(passed=False, failures=["missing skill"])
    failed = result.model_copy(
        update={
            "passed": False,
            "candidate": failed_candidate,
        }
    )
    failed_console = Console(record=True, color_system=None, width=100)
    render_diff_terminal(failed, failed_console)
    failed_output = failed_console.export_text()
    assert "Candidate Agent Card" in failed_output
    assert "missing skill" in failed_output
    assert "missing[31m value" in failed_output
    assert "Candidate failed" in failed_output


def test_renders_success_and_trial_error() -> None:
    console = Console(record=True, color_system=None, width=100)
    success = _result(passed=True)
    render_terminal(success, console, verbose=False)

    failed = _result(passed=False)
    failed.scenarios[0].trials.append(
        TrialResult(index=2, passed=False, duration_ms=1, error="connection failed")
    )
    failed.scenarios[0].trials.append(TrialResult(index=3, passed=True, duration_ms=1))
    render_terminal(failed, console, verbose=False)

    output = console.export_text()
    assert "PASS" in output
    assert "connection failed" in output


def test_truncates_diagnostics_and_formats_seconds() -> None:
    assert _diagnostic("x" * 2_001).endswith("…")
    assert _duration(1_500) == "1.50s"


def test_renders_junit_failures_errors_and_trials() -> None:
    result = _result(passed=False)
    result.scenarios[0].trials.append(
        TrialResult(index=2, passed=False, duration_ms=2, error="connection failed")
    )

    root = ET.fromstring(render_junit(result))

    assert root.attrib == {
        "name": "a2a-proof",
        "tests": "2",
        "failures": "1",
        "errors": "1",
        "time": "0.005",
    }
    cases = root.findall("testcase")
    assert cases[0].attrib["name"] == "scenario[31m [trial 1]"
    assert cases[0].find("failure") is not None
    error = cases[1].find("error")
    assert error is not None
    assert error.attrib["message"] == "connection failed"

    passing = ET.fromstring(render_junit(_result(passed=True)))
    passing_case = passing.find("testcase")
    assert passing_case is not None
    assert passing_case.attrib["name"] == "scenario[31m"
    assert passing_case.find("failure") is None


def test_renders_failed_agent_card_in_all_formats() -> None:
    result = SuiteResult(
        passed=False,
        duration_ms=2,
        card=CardResult(
            passed=False,
            failures=["Agent Card does not contain skill ID 'summarize'"],
        ),
        scenarios=[],
    )
    console = Console(record=True, color_system=None, width=100)

    render_terminal(result, console, verbose=False)
    root = ET.fromstring(render_junit(result))

    output = console.export_text()
    assert "Agent Card" in output
    assert "Agent Card and 0 scenarios failed" in output
    assert '"card"' in render_json(result)
    assert root.attrib["tests"] == "1"
    assert root.attrib["failures"] == "1"
    case = root.find("testcase")
    assert case is not None
    assert case.attrib["name"] == "Agent Card"
    assert case.find("failure") is not None

    passing = SuiteResult(
        passed=True,
        duration_ms=1,
        card=CardResult(passed=True),
        scenarios=[],
    )
    passing_case = ET.fromstring(render_junit(passing)).find("testcase")
    assert passing_case is not None
    assert passing_case.find("failure") is None


def test_renders_aggregate_latency_contract() -> None:
    result = _result(passed=True)
    result.passed = False
    scenario = result.scenarios[0]
    scenario.passed = False
    scenario.latency = LatencyResult(
        passed=False,
        samples=3,
        p50_ms=100,
        p95_ms=920,
        failures=["expected p95 trial latency at most 0.9s, got 0.920s"],
    )
    console = Console(record=True, color_system=None, width=100)

    render_terminal(result, console, verbose=False)
    root = ET.fromstring(render_junit(result))

    assert "latency: expected p95" in console.export_text()
    assert root.attrib["tests"] == "2"
    assert root.attrib["failures"] == "1"
    latency = root.findall("testcase")[1]
    assert latency.attrib["name"] == "scenario[31m [latency]"
    assert latency.find("failure") is not None

    result.passed = True
    scenario.passed = True
    scenario.latency = LatencyResult(
        passed=True,
        samples=3,
        p50_ms=100,
        p95_ms=200,
    )
    passing_latency = ET.fromstring(render_junit(result)).findall("testcase")[1]
    assert passing_latency.find("failure") is None


def test_junit_marks_failures_within_pass_rate_as_skipped() -> None:
    result = _result(passed=True)
    scenario = result.scenarios[0]
    scenario.required_trials = 1
    scenario.trials.extend(
        [
            TrialResult(
                index=2,
                passed=False,
                duration_ms=2,
                turns=[
                    TurnResult(
                        index=1,
                        passed=False,
                        state="completed",
                        duration_ms=2,
                        text="wrong",
                        failures=["wrong answer"],
                    )
                ],
            ),
            TrialResult(index=3, passed=False, duration_ms=1, error="connection failed"),
        ]
    )

    root = ET.fromstring(render_junit(result))

    assert root.attrib["tests"] == "3"
    assert root.attrib["failures"] == "0"
    assert root.attrib["errors"] == "0"
    assert root.attrib["skipped"] == "2"
    skipped = [case.find("skipped") for case in root.findall("testcase")[1:]]
    assert all(item is not None for item in skipped)
