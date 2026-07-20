from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, BinaryIO, Literal

import click
from a2a.client.errors import A2AClientError
from a2a.types import AgentCard
from pydantic import ValidationError
from rich.console import Console

from a2a_proof.a2a import discover_agent
from a2a_proof.ap2 import (
    AP2Error,
    AP2VerificationError,
    ap2_mandate_reference,
    ensure_ap2_sdk,
    inspect_ap2,
    inspect_ap2_receipt,
    read_ap2_receipt_token,
    read_ap2_token,
)
from a2a_proof.config import ConfigError, load_config, write_config
from a2a_proof.diffing import compare_results
from a2a_proof.evidence import EvidenceError, write_evidence
from a2a_proof.models import AgentConfig, ProofConfig, Scenario, SuiteResult
from a2a_proof.reporting import (
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
from a2a_proof.runner import run

DEFAULT_CONFIG = Path("a2a-proof.yaml")
MAX_GENERATED_SCENARIOS = 20
MAX_GENERATED_MESSAGE_CHARS = 100_000
TRANSPORT_CHOICES = ("auto", "JSONRPC", "HTTP+JSON", "GRPC")


class ProofCommandError(click.ClickException):
    exit_code = 2


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="a2a-proof")
def main() -> None:
    """Black-box contract tests for A2A agents."""


@main.group("ap2")
def ap2_command() -> None:
    """Inspect signed AP2 mandates and receipts."""


@ap2_command.command("inspect")
@click.argument(
    "token_file",
    type=click.File("rb"),
    default="-",
)
@click.option(
    "--trust-root",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Public P-256 JWK used to verify the root mandate.",
)
@click.option("--audience", required=True, help="Expected terminal audience.")
@click.option("--nonce", required=True, help="Expected terminal nonce.")
@click.option(
    "mandate_type",
    "--type",
    type=click.Choice(["auto", "checkout", "payment"]),
    default="auto",
    show_default=True,
)
@click.option("--transaction-id", help="Expected payment transaction ID.")
@click.option("--open-checkout-hash", help="Expected payment checkout reference.")
@click.option("--checkout-hash", help="Expected checkout hash.")
@click.option(
    "output_format",
    "--format",
    type=click.Choice(["terminal", "json"]),
    default="terminal",
)
def ap2_inspect_command(
    token_file: BinaryIO,
    trust_root: Path,
    audience: str,
    nonce: str,
    mandate_type: Literal["auto", "checkout", "payment"],
    transaction_id: str | None,
    open_checkout_hash: str | None,
    checkout_hash: str | None,
    output_format: str,
) -> None:
    """Verify an AP2 mandate token read from FILE or standard input."""
    try:
        result = inspect_ap2(
            read_ap2_token(token_file),
            trust_root,
            audience,
            nonce,
            mandate_type=mandate_type,
            transaction_id=transaction_id,
            open_checkout_hash=open_checkout_hash,
            checkout_hash=checkout_hash,
        )
    except AP2VerificationError as error:
        if output_format == "json":
            click.echo(render_ap2_invalid_json(str(error)))
        else:
            render_ap2_invalid(str(error), Console())
        raise click.exceptions.Exit(1) from error
    except (AP2Error, OSError) as error:
        raise ProofCommandError(str(error)) from error

    if output_format == "json":
        click.echo(render_ap2_json(result))
    else:
        render_ap2_terminal(result, Console())


@ap2_command.command("inspect-receipt")
@click.argument(
    "receipt_file",
    type=click.File("rb"),
    default="-",
)
@click.option(
    "--issuer-key",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="Public P-256 JWK used to verify the receipt issuer.",
)
@click.option(
    "receipt_type",
    "--type",
    required=True,
    type=click.Choice(["checkout", "payment"]),
)
@click.option(
    "--mandate",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Mandate chain whose closed JWT the receipt must reference.",
)
@click.option("--reference", help="Expected closed-mandate reference hash.")
@click.option("--issuer", help="Expected receipt issuer.")
@click.option("--status", type=click.Choice(["Success", "Error"]))
@click.option("--payment-id", help="Expected payment ID.")
@click.option("--order-id", help="Expected order ID.")
@click.option("error_code", "--error", help="Expected receipt error code.")
@click.option(
    "output_format",
    "--format",
    type=click.Choice(["terminal", "json"]),
    default="terminal",
)
def ap2_inspect_receipt_command(
    receipt_file: BinaryIO,
    issuer_key: Path,
    receipt_type: Literal["checkout", "payment"],
    mandate: Path | None,
    reference: str | None,
    issuer: str | None,
    status: Literal["Success", "Error"] | None,
    payment_id: str | None,
    order_id: str | None,
    error_code: str | None,
    output_format: str,
) -> None:
    """Verify a signed AP2 receipt read from FILE or standard input."""
    try:
        if reference is not None:
            if mandate is not None:
                raise ProofCommandError("provide exactly one of --mandate or --reference")
            expected_reference = reference
        elif mandate is not None:
            with mandate.open("rb") as stream:
                expected_reference = ap2_mandate_reference(read_ap2_token(stream))
        else:
            raise ProofCommandError("provide exactly one of --mandate or --reference")
        result = inspect_ap2_receipt(
            read_ap2_receipt_token(receipt_file),
            issuer_key,
            expected_reference,
            receipt_type=receipt_type,
            issuer=issuer,
            status=status,
            payment_id=payment_id,
            order_id=order_id,
            error_code=error_code,
        )
    except AP2VerificationError as error:
        if output_format == "json":
            click.echo(render_ap2_invalid_json(str(error)))
        else:
            render_ap2_receipt_invalid(str(error), Console())
        raise click.exceptions.Exit(1) from error
    except (AP2Error, OSError) as error:
        raise ProofCommandError(str(error)) from error

    if output_format == "json":
        click.echo(render_ap2_json(result))
    else:
        render_ap2_receipt_terminal(result, Console())


@main.command("init")
@click.argument("url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path, dir_okay=False),
    default=DEFAULT_CONFIG,
    show_default=True,
)
@click.option("--timeout", type=click.FloatRange(min=0.1, max=600), default=30.0, show_default=True)
@click.option("--card-path", help="Custom path to the Agent Card.")
@click.option(
    "--allow-cross-origin",
    is_flag=True,
    help="Allow Agent Card interfaces on a different origin.",
)
@click.option(
    "--header-env",
    multiple=True,
    metavar="HEADER=ENV_VAR",
    help="Read an HTTP header value from an environment variable.",
)
@click.option("--force", is_flag=True, help="Replace an existing configuration file.")
def init_command(
    url: str,
    output: Path,
    timeout: float,
    card_path: str | None,
    allow_cross_origin: bool,
    header_env: tuple[str, ...],
    force: bool,
) -> None:
    """Create a configuration from an agent's public Agent Card."""
    references, headers = _header_environment(header_env)
    try:
        agent = AgentConfig(
            url=url,
            timeout=timeout,
            card_path=card_path,
            allow_cross_origin_interfaces=allow_cross_origin,
            headers=headers,
        )
        card = asyncio.run(discover_agent(agent))
        scenarios = _scenarios_from_card(card)
        agent_data: dict[str, Any] = {
            "url": url,
            "timeout": timeout,
        }
        data: dict[str, Any] = {
            "version": 1,
            "agent": agent_data,
            "scenarios": scenarios,
        }
        if card_path is not None:
            agent_data["card_path"] = card_path
        if allow_cross_origin:
            agent_data["allow_cross_origin_interfaces"] = True
        if references:
            agent_data["headers"] = references
        required_extensions = list(
            dict.fromkeys(
                extension.uri for extension in card.capabilities.extensions if extension.required
            )
        )
        if required_extensions:
            agent_data["extensions"] = required_extensions
        ProofConfig.model_validate(data)
        write_config(output, data, force=force)
    except (A2AClientError, ConfigError, ValidationError, OSError, RuntimeError) as error:
        raise ProofCommandError(str(error)) from error

    click.echo(f"Created {output} with {_scenario_count(len(scenarios))}.")


@main.command("check")
@click.argument(
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=DEFAULT_CONFIG,
)
def check_command(config_path: Path) -> None:
    """Validate a configuration without contacting the agent."""
    try:
        config = load_config(config_path)
        ensure_ap2_sdk(config)
    except (AP2Error, ConfigError) as error:
        raise ProofCommandError(str(error)) from error
    click.echo(f"Valid: {_scenario_count(len(config.scenarios))}.")


@main.command("run")
@click.argument(
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=DEFAULT_CONFIG,
)
@click.option(
    "output_format",
    "--format",
    type=click.Choice(["terminal", "json", "junit"]),
    default="terminal",
)
@click.option("--output", "-o", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--evidence",
    "evidence_dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Write a redacted run evidence bundle.",
)
@click.option(
    "--jobs",
    type=click.IntRange(min=1, max=32),
    default=1,
    show_default=True,
    help="Maximum concurrent trials within one scenario.",
)
@click.option("--verbose", "-v", is_flag=True, help="Show failed agent responses.")
@click.option(
    "scenario_names",
    "--scenario",
    multiple=True,
    metavar="NAME",
    help="Run only this scenario. Repeat to select more than one.",
)
@click.option(
    "--transport",
    type=click.Choice(TRANSPORT_CHOICES),
    help="Override the configured transport for this run.",
)
def run_command(
    config_path: Path,
    output_format: str,
    output: Path | None,
    evidence_dir: Path | None,
    jobs: int,
    verbose: bool,
    scenario_names: tuple[str, ...],
    transport: str | None,
) -> None:
    """Run the configured scenarios against the agent."""
    if output is not None and output_format == "terminal":
        raise click.UsageError("--output requires --format json or junit")
    try:
        config = load_config(config_path)
        config = _with_transport(config, transport)
        if scenario_names:
            config = config.model_copy(
                update={"scenarios": _select_scenarios(config.scenarios, scenario_names)}
            )
        result = asyncio.run(run(config, max_parallel_trials=jobs))
        if evidence_dir is not None:
            write_evidence(evidence_dir, config, result, max_parallel_trials=jobs)
    except (A2AClientError, ConfigError, EvidenceError, OSError, RuntimeError) as error:
        raise ProofCommandError(str(error)) from error

    if output_format in {"json", "junit"}:
        rendered = render_json(result) if output_format == "json" else render_junit(result)
        rendered = f"{rendered}\n"
        if output is None:
            click.echo(rendered, nl=False)
        else:
            try:
                output.write_text(rendered, encoding="utf-8")
            except OSError as error:
                raise ProofCommandError(
                    f"cannot write {output}: {error.strerror or error}"
                ) from error
    else:
        render_terminal(result, Console(), verbose=verbose)
    if not result.passed:
        raise click.exceptions.Exit(1)


@main.command("diff")
@click.argument(
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=DEFAULT_CONFIG,
)
@click.option("--against", required=True, metavar="URL", help="Candidate agent discovery URL.")
@click.option(
    "output_format",
    "--format",
    type=click.Choice(["terminal", "json"]),
    default="terminal",
)
@click.option("--output", "-o", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--jobs",
    type=click.IntRange(min=1, max=32),
    default=1,
    show_default=True,
    help="Maximum concurrent trials within one scenario.",
)
@click.option(
    "scenario_names",
    "--scenario",
    multiple=True,
    metavar="NAME",
    help="Compare only this scenario. Repeat to select more than one.",
)
@click.option(
    "--transport",
    type=click.Choice(TRANSPORT_CHOICES),
    help="Override the configured transport for both deployments.",
)
def diff_command(
    config_path: Path,
    against: str,
    output_format: str,
    output: Path | None,
    jobs: int,
    scenario_names: tuple[str, ...],
    transport: str | None,
) -> None:
    """Compare one contract against baseline and candidate agents."""
    if output is not None and output_format == "terminal":
        raise click.UsageError("--output requires --format json")
    try:
        config = load_config(config_path)
        config = _with_transport(config, transport)
        if scenario_names:
            config = config.model_copy(
                update={"scenarios": _select_scenarios(config.scenarios, scenario_names)}
            )
        candidate_agent = AgentConfig.model_validate({**config.agent.model_dump(), "url": against})
        candidate_config = config.model_copy(update={"agent": candidate_agent})
        baseline_result, candidate_result = asyncio.run(
            _run_pair(config, candidate_config, max_parallel_trials=jobs)
        )
        result = compare_results(
            baseline_result,
            candidate_result,
            [scenario.name for scenario in config.scenarios],
        )
    except (A2AClientError, ConfigError, ValidationError, OSError, RuntimeError) as error:
        raise ProofCommandError(str(error)) from error

    if output_format == "json":
        rendered = f"{render_diff_json(result)}\n"
        if output is None:
            click.echo(rendered, nl=False)
        else:
            try:
                output.write_text(rendered, encoding="utf-8")
            except OSError as error:
                raise ProofCommandError(
                    f"cannot write {output}: {error.strerror or error}"
                ) from error
    else:
        render_diff_terminal(result, Console())
    if not result.passed:
        raise click.exceptions.Exit(1)


async def _run_pair(
    baseline: ProofConfig,
    candidate: ProofConfig,
    *,
    max_parallel_trials: int,
) -> tuple[SuiteResult, SuiteResult]:
    baseline_result = await run(baseline, max_parallel_trials=max_parallel_trials)
    candidate_result = await run(candidate, max_parallel_trials=max_parallel_trials)
    return baseline_result, candidate_result


def _with_transport(config: ProofConfig, transport: str | None) -> ProofConfig:
    if transport is None:
        return config
    agent = AgentConfig.model_validate({**config.agent.model_dump(), "transport": transport})
    return config.model_copy(update={"agent": agent})


def _header_environment(values: tuple[str, ...]) -> tuple[dict[str, str], dict[str, str]]:
    references: dict[str, str] = {}
    resolved: dict[str, str] = {}
    for value in values:
        name, separator, variable = value.partition("=")
        if not separator or not name or not variable:
            raise ProofCommandError(f"invalid --header-env {value!r}; expected HEADER=ENV_VAR")
        if variable not in os.environ:
            raise ProofCommandError(f"environment variable {variable!r} is not set")
        references[name] = f"${{{variable}}}"
        resolved[name] = os.environ[variable]
    return references, resolved


def _scenarios_from_card(card: AgentCard) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for skill in card.skills:
        if not skill.examples:
            continue
        example = skill.examples[0].strip()
        if not example or len(example) > MAX_GENERATED_MESSAGE_CHARS:
            continue
        base_name = (skill.name or skill.id or "scenario").strip()[:200]
        name = _unique_name(base_name or "scenario", used_names)
        scenarios.append({"name": name, "message": example})
        if len(scenarios) == MAX_GENERATED_SCENARIOS:
            break
    if not scenarios:
        scenarios.append({"name": "smoke", "message": "Hello"})
    return scenarios


def _unique_name(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        marker = f" {suffix}"
        candidate = f"{base[: 200 - len(marker)]}{marker}"
        suffix += 1
    used.add(candidate)
    return candidate


def _scenario_count(count: int) -> str:
    return f"{count} scenario{'s' if count != 1 else ''}"


def _select_scenarios(
    scenarios: list[Scenario],
    names: tuple[str, ...],
) -> list[Scenario]:
    requested = set(names)
    missing = sorted(requested - {scenario.name for scenario in scenarios})
    if missing:
        label = "scenario" if len(missing) == 1 else "scenarios"
        raise ProofCommandError(f"unknown {label}: {', '.join(missing)}")
    return [scenario for scenario in scenarios if scenario.name in requested]
