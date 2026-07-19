from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG

from a2a_proof.models import FileInput, ProofConfig

MAX_FILE_BYTES = 10_000_000
MAX_TURN_FILE_BYTES = 20_000_000


class FileInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedFile:
    content: bytes
    filename: str
    media_type: str


def validate_config_files(config: ProofConfig) -> None:
    for scenario in config.scenarios:
        for turn in scenario.resolved_turns():
            _resolve_files(turn.files, config.contract_dir)


def prepare_files(files: list[FileInput], contract_dir: Path) -> list[PreparedFile]:
    resolved = _resolve_files(files, contract_dir)
    prepared: list[PreparedFile] = []
    total = 0
    for item, path in zip(files, resolved, strict=True):
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
        except OSError as error:
            raise FileInputError(f"cannot read input file {item.path!r}: {error}") from error
        if len(content) > MAX_FILE_BYTES:
            raise FileInputError(f"input file {item.path!r} exceeds {MAX_FILE_BYTES} bytes")
        total += len(content)
        if total > MAX_TURN_FILE_BYTES:
            raise FileInputError(f"input files exceed {MAX_TURN_FILE_BYTES} bytes per turn")
        media_type = item.media_type or mimetypes.guess_type(path.name)[0]
        prepared.append(
            PreparedFile(
                content=content,
                filename=path.name,
                media_type=media_type or "application/octet-stream",
            )
        )
    return prepared


def _resolve_files(files: list[FileInput], contract_dir: Path) -> list[Path]:
    resolved: list[Path] = []
    total = 0
    root = contract_dir.resolve()
    for item in files:
        try:
            path = (root / item.path).resolve()
            stat = path.stat()
        except OSError as error:
            raise FileInputError(f"cannot access input file {item.path!r}: {error}") from error
        if not path.is_relative_to(root):
            raise FileInputError(f"input file {item.path!r} escapes the contract directory")
        if not S_ISREG(stat.st_mode):
            raise FileInputError(f"input file {item.path!r} is not a regular file")
        if stat.st_size > MAX_FILE_BYTES:
            raise FileInputError(f"input file {item.path!r} exceeds {MAX_FILE_BYTES} bytes")
        total += stat.st_size
        if total > MAX_TURN_FILE_BYTES:
            raise FileInputError(f"input files exceed {MAX_TURN_FILE_BYTES} bytes per turn")
        resolved.append(path)
    return resolved
