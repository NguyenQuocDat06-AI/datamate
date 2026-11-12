"""Fast tests for gen_perturb_data.py (synthetic fixtures in tmp_path)."""
import csv
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import openpyxl
import pytest

MOD_PATH = Path("/home/ubuntu/datamate_review_data/gen_perturb_data.py")
spec = importlib.util.spec_from_file_location("gen_perturb_data", MOD_PATH)
gp = importlib.util.module_from_spec(spec)
sys.modules["gen_perturb_data"] = gp
spec.loader.exec_module(gp)

HEADER = ["Mã hóa đơn", "Ngày", "Khách hàng", "Số lượng", "Thành tiền", "Đơn giá", "Ghi chú"]
N = 45  # 20 + 20 + 5 rows -> three subtotal blocks


def _xlsx_rows():
    rows = []
    for i in range(N):
        d = dt.datetime(2025, 1 + i % 3, 1 + i % 27)
        rows.append([f"HD{i:04d}", d, f"Khách {i % 5}", i % 4 + 1, (i + 1) * 1000, 1000 + i, f"ghi {i}"])
    return rows


def _csv_rows():
    out = []
    for r in _xlsx_rows():
        d = r[1]
        out.append([r[0], f"{d.day:02d}/{d.month:02d}/{d.year}", r[2], str(r[3]),
                    f"{r[4]:,}", f"{r[5]:,}", r[6]])
    return out


@pytest.fixture
def env(tmp_path):
    src = tmp_path / "src" / "data"
    shop = src / "biz_t"
    shop.mkdir(parents=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(HEADER)
    for r in _xlsx_rows():
        ws.append(r)
    wb.save(shop / "don_hang.xlsx")
    with open(shop / "ban_hang.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(HEADER)
        w.writerows(_csv_rows())
    (shop / "README.md").write_text("# readme\nfixture\n", encoding="utf-8")
    (shop / "cu.xls").write_bytes(b"\xd0\xcf\x11\xe0 not really xls")
    q = tmp_path / "q.jsonl"
    q.write_text('{"id":"1","business_id":"biz_t"}\n{"id":"2","business_id":"biz_other"}\n',
                 encoding="utf-8")
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "rulebook.md").write_text("rb", encoding="utf-8")
    (tmp_path / "root" / "calendar.csv").write_text("d\n", encoding="utf-8")
    return {"src": tmp_path / "src" / "data", "out": tmp_path / "out",
            "q": q, "root": tmp_path / "root", "tmp": tmp_path}


def _run(env, op, seed=0, make_root=False):
    return gp.generate(op, ["biz_t"], seed, src_data=env["src"], out_root=env["out"],
                       questions_path=env["q"], src_root=env["root"], make_root=make_root)


def _shop_out(env, op):
    return env["out"] / op / "data" / "biz_t"


def _xlsx_values(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def _csv_values(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.reader(f, delimiter=";")]


def _manifest(env, op):
    return json.loads((_shop_out(env, op) / "manifest.json").read_text(encoding="utf-8"))


def _col_multiset(rows, j):
    return sorted(str(r[j]) for r in rows if r[j] not in (None, ""))


def test_rename_headers_keeps_values(env):
    _run(env, "rename_headers")
    out = _xlsx_values(_shop_out(env, "rename_headers") / "don_hang.xlsx")["Sheet1"]
    orig = [HEADER] + [list(r) for r in _xlsx_rows()]
    assert out[0][0] in gp.HEADER_VARIANTS["Mã hóa đơn"]
    assert out[0][4] in gp.HEADER_VARIANTS["Thành tiền"]
    assert len(out) == len(orig)
    for j in range(len(HEADER)):
        assert _col_multiset(out[1:], j) == _col_multiset(orig[1:], j)
    csv_out = _csv_values(_shop_out(env, "rename_headers") / "ban_hang.csv")
    assert csv_out[0] != HEADER
    assert sorted(r[4] for r in csv_out[1:]) == sorted(r[4] for r in _csv_rows())
    changes = _manifest(env, "rename_headers")["files"]["don_hang.xlsx"]["header_changes"]
    assert changes and all(c["before"] != c["after"] for c in changes)


def test_reorder_columns_keeps_each_column(env):
    _run(env, "reorder_columns")
    man = _manifest(env, "reorder_columns")["files"]["don_hang.xlsx"]
    perm = man["column_permutation"]["Sheet1"]
    assert perm != list(range(len(HEADER)))
    out = _xlsx_values(_shop_out(env, "reorder_columns") / "don_hang.xlsx")["Sheet1"]
    orig = [HEADER] + [list(r) for r in _xlsx_rows()]
    for o_row, n_row in zip(orig, out):
        assert [n_row[k] for k in range(len(perm))] == [o_row[p] for p in perm]


@pytest.mark.parametrize("fname", ["don_hang.xlsx", "ban_hang.csv"])
def test_split_by_month_counts(env, fname):
    _run(env, "split_sheets_by_month")
    out_dir = _shop_out(env, "split_sheets_by_month")
    if fname.endswith(".xlsx"):
        sheets = _xlsx_values(out_dir / fname)
        assert sorted(sheets) == ["T01-2025", "T02-2025", "T03-2025"]
        per = {k: v[1:] for k, v in sheets.items()}
        for k, v in sheets.items():
            assert v[0] == HEADER
    else:
        names = sorted(p.name for p in out_dir.glob("ban_hang_T*.csv"))
        assert names == ["ban_hang_T01-2025.csv", "ban_hang_T02-2025.csv", "ban_hang_T03-2025.csv"]
        per = {}
        for n in names:
            rows = _csv_values(out_dir / n)
            assert rows[0] == HEADER
            per[n] = rows[1:]
    assert sum(len(v) for v in per.values()) == N
    assert sorted(str(r) for v in per.values() for r in v) == sorted(
        str(r) for r in (_xlsx_rows() if fname.endswith(".xlsx") else _csv_rows()))


def test_title_rows_inserted(env):
    _run(env, "add_title_rows")
    out = _xlsx_values(_shop_out(env, "add_title_rows") / "don_hang.xlsx")["Sheet1"]
    assert out[0][0].startswith(gp.TITLE_PREFIX) and "don_hang" in out[0][0]
    assert all(v is None for v in out[1])
    assert out[2] == HEADER
    assert len(out) == N + 3
    csv_out = _csv_values(_shop_out(env, "add_title_rows") / "ban_hang.csv")
    assert csv_out[0][0].startswith(gp.TITLE_PREFIX)
    assert all(v == "" for v in csv_out[1])
    assert csv_out[2] == HEADER


def test_subtotal_rows_sums(env):
    _run(env, "add_subtotal_rows")
    out = _xlsx_values(_shop_out(env, "add_subtotal_rows") / "don_hang.xlsx")["Sheet1"]
    subs = [i for i, r in enumerate(out) if r[0] == gp.SUBTOTAL_LABEL]
    assert subs == [21, 42, 48]  # after rows 1-20, 21-40 and the final partial block 41-45
    assert [out[i][4] for i in subs] == [210000, 610000, 215000]
    # text / date columns stay empty; numeric columns (Số lượng, Đơn giá) get block sums
    assert all(out[i][j] is None for i in subs for j in (1, 2, 6))
    assert [out[i][3] for i in subs] == [sum(r[3] for r in _xlsx_rows()[a:b])
                                         for a, b in ((0, 20), (20, 40), (40, 45))]
    # removing subtotal rows restores the original data exactly
    data = [r for r in out[1:] if r[0] != gp.SUBTOTAL_LABEL]
    assert [list(r) for r in data] == [list(r) for r in _xlsx_rows()]
    csv_out = _csv_values(_shop_out(env, "add_subtotal_rows") / "ban_hang.csv")
    csv_subs = [r for r in csv_out if r[0] == gp.SUBTOTAL_LABEL]
    assert len(csv_subs) == 3
    assert csv_subs[0][4] == "210,000" and csv_subs[1][4] == "610,000"


def test_determinism_manifest_and_skips(env):
    _run(env, "rename_headers", seed=7)
    a = (_shop_out(env, "rename_headers") / "manifest.json").read_bytes()
    a_csv = (_shop_out(env, "rename_headers") / "ban_hang.csv").read_bytes()
    _run(env, "rename_headers", seed=7)
    assert (_shop_out(env, "rename_headers") / "manifest.json").read_bytes() == a
    assert (_shop_out(env, "rename_headers") / "ban_hang.csv").read_bytes() == a_csv
    man = _manifest(env, "rename_headers")
    assert man["seed"] == 7 and man["op"] == "rename_headers"
    assert man["files"]["cu.xls"]["status"] == "skipped"
    assert (_shop_out(env, "rename_headers") / "README.md").read_bytes() == \
        (env["src"] / "biz_t" / "README.md").read_bytes()
    _run(env, "rename_headers", seed=8)
    assert (_shop_out(env, "rename_headers") / "manifest.json").read_bytes() != a


def test_csv_encoding_and_delimiter_preserved(env):
    _run(env, "add_title_rows")
    raw = (_shop_out(env, "add_title_rows") / "ban_hang.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM kept
    lines = raw.decode("utf-8-sig").split("\r\n")
    assert lines[0].startswith("Báo cáo")
    assert lines[2].split(";")[0] == HEADER[0]  # ';' delimiter kept


def test_test_root_files(env):
    gp.generate("reorder_columns", ["biz_t"], 0, src_data=env["src"], out_root=env["out"],
                questions_path=env["q"], src_root=env["root"], make_root=True)
    root = env["out"] / "reorder_columns"
    qs = [json.loads(l) for l in (root / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [q["business_id"] for q in qs] == ["biz_t"]
    for name in ("rulebook.md", "calendar.csv"):
        assert (root / name).is_symlink()
        assert (root / name).resolve() == (env["root"] / name).resolve()


def test_default_shops_every_fourth(tmp_path):
    for i in range(30):
        (tmp_path / f"biz_{101 + i}").mkdir()
    picked = gp.default_shops(tmp_path)
    assert len(picked) == 8
    assert picked[0] == "biz_101" and picked[1] == "biz_105"
