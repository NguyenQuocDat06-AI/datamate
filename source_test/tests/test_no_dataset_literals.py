"""Guard: the semantic pipeline must not embed anything learned from a particular dataset."""

from __future__ import annotations

import ast
import glob
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A worktree has no cache of its own; set DATASET_CACHE_ROOT to scan table names from a real one.
CACHE_ROOT = Path(os.environ.get("DATASET_CACHE_ROOT", ROOT))
SEMANTIC = ROOT / "bizreason" / "semantic"
# The ontology taken from the rulebook is the only place allowed to hold domain words.
ALLOWED_FILES = {"vocabulary.py"}

RULES = {
    "year_or_date": re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)"),
    "question_or_business_id": re.compile(r"\b(?:q_[0-9a-f]{8,}|biz_\d+|dev_\d+)\b", re.I),
    "vietnamese_text": re.compile(r"[À-ỹĐđ]"),
}


def table_stems() -> set[str]:
    """Table-name stems seen in any built catalog; empty when no cache is available."""
    stems: set[str] = set()
    for path in glob.glob(str(CACHE_ROOT / "artifacts" / "cache" / "*" / "*" / "catalog.json")):
        for table in json.loads(Path(path).read_text(encoding="utf-8"))["tables"]:
            name = re.sub(r"(_sheet\d*)?(_[0-9a-f]{6,12})?$", "", table["name"])
            name = re.sub(r"_(t?\d{1,2}_\d{4}|\d{4})$", "", name)
            if len(name) >= 6:
                stems.add(name.lower())
    return stems


def string_constants(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def violations(path: Path, stems: set[str]) -> list[str]:
    found = []
    for line, value in string_constants(path.read_text(encoding="utf-8")):
        for rule, pattern in RULES.items():
            if pattern.search(value):
                found.append(f"{path.name}:{line}: {rule}: {value[:70]!r}")
        lowered = value.lower()
        for stem in stems:
            if stem in lowered:
                found.append(f"{path.name}:{line}: table_name: {stem}")
                break
    return found


def test_semantic_package_has_no_dataset_literals() -> None:
    stems = table_stems()
    found = []
    for path in sorted(SEMANTIC.glob("**/*.py")) if SEMANTIC.exists() else []:
        if path.name not in ALLOWED_FILES:
            found.extend(violations(path, stems))
    assert not found, "\n".join(found[:50])
