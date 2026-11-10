from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from openpyxl import load_workbook

from bizreason.utils import quote_identifier, writable_path


@dataclass
class RawSheet:
    path: Path
    name: str
    rows: Iterator[tuple[Any, ...]]
    declared_columns: list[str] | None = None


def sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def read_sheets(path: Path) -> Iterator[RawSheet]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        raw = path.read_bytes()
        text = None
        for encoding in ["utf-8-sig", "cp1258", "cp1252"]:
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("CSV encoding could not be determined")
        try:
            dialect = csv.Sniffer().sniff(text[:16384], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        yield RawSheet(
            path, path.stem, (tuple(row) for row in csv.reader(io.StringIO(text), dialect))
        )
    elif suffix == ".xlsx":
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet in workbook:
                sheet.reset_dimensions()  # Exporters sometimes declare incorrect dimensions.
                yield RawSheet(path, sheet.title, sheet.iter_rows(values_only=True))
        finally:
            workbook.close()
    elif suffix in {".sqlite", ".sqlite3", ".db"}:
        connection = sqlite_readonly(path)
        try:
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ):
                cursor = connection.execute(f"SELECT * FROM {quote_identifier(name)}")
                yield RawSheet(
                    path, name, iter(cursor), [column[0] for column in cursor.description]
                )
        finally:
            connection.close()


def unpack_dataset(archive: Path, destination: Path, *, max_bytes: int = 2_000_000_000) -> Path:
    destination = writable_path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if sum(entry.file_size for entry in entries) > max_bytes:
            raise ValueError("Archive exceeds extraction size limit")
        # Validate the entire archive before writing any member.
        for entry in entries:
            relative = PurePosixPath(entry.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or ":" in entry.filename:
                raise ValueError("Unsafe archive path")
            target = writable_path(destination.joinpath(*relative.parts))
            if (
                not target.is_relative_to(destination)
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Archive symlinks and escaping paths are forbidden")
            if target.exists() and not entry.is_dir():
                raise FileExistsError("Refusing to overwrite an extracted file")
        package.extractall(destination)
    return destination
