from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from bizreason.contracts import ColumnMeta, TableMeta
from bizreason.data.identities import canonicalize_customers

ROOT = Path(__file__).resolve().parent.parent

CUSTOMER_TABLE = TableMeta(
    name="customers",
    sources=[],
    columns=[
        ColumnMeta(name="cid", original_name="cid", dtype="TEXT", semantic_role="customer_id"),
        ColumnMeta(name="cname", original_name="cname", dtype="TEXT", semantic_role="customer_name"),
    ],
    row_count=0,
    raw_row_count=0,
)


def _canonical_for(rows: list[tuple[str, str]]) -> dict[str, str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE customers (cid TEXT, cname TEXT)")
        connection.executemany("INSERT INTO customers (cid, cname) VALUES (?, ?)", rows)
        return canonicalize_customers(connection, [CUSTOMER_TABLE])
    finally:
        connection.close()


def test_diacritic_spelling_wins_regardless_of_row_order():
    accented, plain = "Ngô Hồng Duy", "Ngo Hong Duy"
    forward = _canonical_for([("1", accented), ("1", plain)])
    reverse = _canonical_for([("1", plain), ("1", accented)])
    assert forward == {"1": accented}
    assert reverse == {"1": accented}


def test_mixed_case_beats_all_upper_with_diacritics():
    mixed = "Ngô Hồng Duy"
    upper = "NGÔ HỒNG DUY"
    assert _canonical_for([("1", upper), ("1", mixed)]) == {"1": mixed}
    assert _canonical_for([("1", mixed), ("1", upper)]) == {"1": mixed}


def test_choice_is_independent_of_pythonhashseed():
    script = (
        "import json, sqlite3, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from bizreason.contracts import ColumnMeta, TableMeta\n"
        "from bizreason.data.identities import canonicalize_customers\n"
        "table = TableMeta(name='customers', sources=[], row_count=0, raw_row_count=0, columns=["
        "ColumnMeta(name='cid', original_name='cid', dtype='TEXT', semantic_role='customer_id'),"
        "ColumnMeta(name='cname', original_name='cname', dtype='TEXT', semantic_role='customer_name')])\n"
        "con = sqlite3.connect(':memory:')\n"
        "con.execute('CREATE TABLE customers (cid TEXT, cname TEXT)')\n"
        "con.executemany('INSERT INTO customers VALUES (?, ?)', "
        "[('1', 'Ngo Hong Duy'), ('1', 'Ngô Hồng Duy'), ('2', 'Le Van A'), ('2', 'Lê Văn A')])\n"
        "print(json.dumps(canonicalize_customers(con, [table]), sort_keys=True, ensure_ascii=False))\n"
    )
    outputs = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            cwd=str(ROOT),
        )
        outputs.append(json.loads(result.stdout.strip().splitlines()[-1]))
    assert outputs[0] == outputs[1]
    assert outputs[0] == {"1": "Ngô Hồng Duy", "2": "Lê Văn A"}
