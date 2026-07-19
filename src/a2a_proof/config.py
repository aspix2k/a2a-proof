from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from a2a_proof.models import ProofConfig

MAX_CONFIG_BYTES = 1_000_000
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    pass


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> ProofConfig:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror or error}") from error
    if len(content) > MAX_CONFIG_BYTES:
        raise ConfigError(f"configuration exceeds {MAX_CONFIG_BYTES} bytes")

    try:
        raw = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise ConfigError(f"cannot parse {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    try:
        expanded = _expand_environment(raw, environ or os.environ)
        return ProofConfig.model_validate(expanded)
    except (ConfigError, ValidationError) as error:
        raise ConfigError(str(error)) from error


def write_config(path: Path, data: Mapping[str, Any], *, force: bool = False) -> None:
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists; pass --force to replace it")
    if not path.parent.is_dir():
        raise ConfigError(f"parent directory does not exist: {path.parent}")

    content = yaml.safe_dump(
        dict(data),
        allow_unicode=True,
        sort_keys=False,
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ConfigError(f"cannot write {path}: {error.strerror or error}") from error


def _expand_environment(value: Any, environ: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        missing: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = environ.get(name)
            if resolved is None:
                missing.add(name)
                return match.group(0)
            return resolved

        expanded = ENV_REFERENCE.sub(replace, value)
        if missing:
            names = ", ".join(sorted(missing))
            raise ConfigError(f"missing environment variable(s): {names}")
        return expanded
    if isinstance(value, Mapping):
        return {key: _expand_environment(item, environ) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_expand_environment(item, environ) for item in value]
    return value
