from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import click
from a2a.client.errors import A2AClientError
from a2a.types import AgentCard
from pydantic import ValidationError
from rich.console import Console

from a2a_proof.a2a import discover_agent
from a2a_proof.config import ConfigError, load_config, write_config
from a2a_proof.models import AgentConfig, ProofConfig
from a2a_proof.reporting import render_json, render_junit, render_terminal
from a2a_proof.runner import run

DEFAULT_CONFIG = Path("a2a-proof.yaml")
MAX_GENERATED_SCENARIOS = 20
MAX_GENERATED_MESSAGE_CHARS = 100_000


class ProofCommandError(click.ClickException):
    exit_code = 2


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="a2a-proof")
def main() -> None:
    """Black-box contract tests for A2A agents."""


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
    except ConfigError as error:
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
@click.option("--verbose", "-v", is_flag=True, help="Show failed agent responses.")
def run_command(
    config_path: Path,
    output_format: str,
    output: Path | None,
    verbose: bool,
) -> None:
    """Run the configured scenarios against the agent."""
    if output is not None and output_format == "terminal":
        raise click.UsageError("--output requires --format json or junit")
    try:
        config = load_config(config_path)
        result = asyncio.run(run(config))
    except (A2AClientError, ConfigError, OSError, RuntimeError) as error:
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
