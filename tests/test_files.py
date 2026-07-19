from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

import a2a_proof.files as files_module
from a2a_proof.files import FileInputError, _resolve_files, prepare_files
from a2a_proof.models import FileInput


def test_prepares_files_with_explicit_and_inferred_media_types(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_bytes(b"hello")
    (tmp_path / "unknown").write_bytes(b"data")

    prepared = prepare_files(
        [
            FileInput(path="note.txt", media_type="text/custom"),
            FileInput(path="unknown"),
        ],
        tmp_path,
    )

    assert prepared[0].content == b"hello"
    assert prepared[0].filename == "note.txt"
    assert prepared[0].media_type == "text/custom"
    assert prepared[1].media_type == "application/octet-stream"


def test_rejects_missing_directories_and_escape_paths(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_bytes(b"secret")

    with pytest.raises(FileInputError, match=r"cannot access input file 'missing\.txt'"):
        prepare_files([FileInput(path="missing.txt")], tmp_path)
    with pytest.raises(FileInputError, match="is not a regular file"):
        prepare_files([FileInput(path=".")], tmp_path)
    with pytest.raises(FileInputError, match="escapes the contract directory"):
        prepare_files([FileInput(path="../outside.txt")], tmp_path)


def test_rejects_symlinks_that_escape_the_contract_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_bytes(b"secret")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available")

    with pytest.raises(FileInputError, match="escapes the contract directory"):
        prepare_files([FileInput(path="link.txt")], tmp_path)


def test_enforces_individual_and_aggregate_file_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "one.bin").write_bytes(b"abc")
    (tmp_path / "two.bin").write_bytes(b"de")
    monkeypatch.setattr(files_module, "MAX_FILE_BYTES", 2)

    with pytest.raises(FileInputError, match=r"one\.bin.*exceeds 2 bytes"):
        prepare_files([FileInput(path="one.bin")], tmp_path)

    monkeypatch.setattr(files_module, "MAX_FILE_BYTES", 3)
    monkeypatch.setattr(files_module, "MAX_TURN_FILE_BYTES", 5)
    assert (
        len(
            prepare_files(
                [FileInput(path="one.bin"), FileInput(path="two.bin")],
                tmp_path,
            )
        )
        == 2
    )

    monkeypatch.setattr(files_module, "MAX_TURN_FILE_BYTES", 4)
    with pytest.raises(FileInputError, match="input files exceed 4 bytes per turn"):
        prepare_files(
            [FileInput(path="one.bin"), FileInput(path="two.bin")],
            tmp_path,
        )

    with pytest.raises(FileInputError, match="input files exceed 4 bytes per turn"):
        _resolve_files(
            [FileInput(path="one.bin"), FileInput(path="two.bin")],
            tmp_path,
        )


def test_bounds_file_reads_without_loading_the_remainder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bounded.bin"
    path.write_bytes(b"a")
    original_open = Path.open

    class BoundedStream(BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            assert size == files_module.MAX_FILE_BYTES + 1
            return super().read(size)

    def bounded(self: Path, mode: str = "r"):
        if self == path:
            return BoundedStream(b"a")
        return original_open(self, mode)

    monkeypatch.setattr(Path, "open", bounded)

    assert prepare_files([FileInput(path="bounded.bin")], tmp_path)[0].content == b"a"


def test_bounds_each_file_read_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "changing.bin"
    path.write_bytes(b"a")
    monkeypatch.setattr(files_module, "MAX_FILE_BYTES", 1)
    original_open = Path.open

    def grow_before_open(self: Path, mode: str = "r"):
        if self == path:
            with original_open(path, "wb") as stream:
                stream.write(b"ab")
        return original_open(self, mode)

    monkeypatch.setattr(Path, "open", grow_before_open)

    with pytest.raises(FileInputError, match=r"changing\.bin.*exceeds 1 bytes"):
        prepare_files([FileInput(path="changing.bin")], tmp_path)


def test_bounds_aggregate_bytes_after_files_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    monkeypatch.setattr(files_module, "MAX_FILE_BYTES", 2)
    monkeypatch.setattr(files_module, "MAX_TURN_FILE_BYTES", 2)
    original_open = Path.open

    def grow_second(self: Path, mode: str = "r"):
        if self == second:
            with original_open(second, "wb") as stream:
                stream.write(b"bc")
        return original_open(self, mode)

    monkeypatch.setattr(Path, "open", grow_second)

    with pytest.raises(FileInputError, match="input files exceed 2 bytes per turn"):
        prepare_files(
            [FileInput(path="first.bin"), FileInput(path="second.bin")],
            tmp_path,
        )


def test_reports_file_read_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "denied.bin"
    path.write_bytes(b"a")
    original_open = Path.open

    def deny(self: Path, mode: str = "r"):
        if self == path:
            raise OSError("denied")
        return original_open(self, mode)

    monkeypatch.setattr(Path, "open", deny)

    with pytest.raises(FileInputError, match=r"cannot read input file 'denied\.bin': denied"):
        prepare_files([FileInput(path="denied.bin")], tmp_path)
