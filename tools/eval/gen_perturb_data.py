#!/usr/bin/env python3
"""Measurement 3 ("invariance under data perturbation") generator.

Writes meaning-preserving layout perturbations of shop folders:

  <OUT_ROOT>/<op>/data/<biz>/   same file names, README.md copied unchanged,
                                manifest.json describing every change
  <OUT_ROOT>/<op>/              test root: questions.jsonl (questions of the
                                perturbed shops), rulebook.md and calendar.csv
                                symlinks, so it can be passed to run_v2 --data

Operators never change a data value (number, date, name). They change only
layout and labels. No network, no model calls, deterministic given --seed.
"""
from __future__ import annotations

import argparse
import codecs
import csv
import datetime as dt
import io
import json
import random
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path("/home/ubuntu/datamate/source_test/data/train")
SRC_DATA = SRC_ROOT / "data"
QUESTIONS = Path("/home/ubuntu/datamate_review_data/train_full_root/questions.jsonl")
OUT_ROOT = Path("/home/ubuntu/datamate_review_data/perturb_data")

OPS = ["rename_headers", "reorder_columns", "split_sheets_by_month",
       "add_title_rows", "add_subtotal_rows"]
SUBTOTAL_LABEL = "Tổng cộng"
SUBTOTAL_BLOCK = 20
DATE_SHARE = 0.8        # share of non-empty cells that must parse as dates
NUMERIC_SHARE = 0.8     # share of non-empty cells that must parse as numbers
TITLE_PREFIX = "Báo cáo"

# Synonym variants for known headers (chosen deterministically per seed).
HEADER_VARIANTS = {
    "Mã hóa đơn": ["Ma HD", "Số hoá đơn"],
    "Ngày": ["Ngày tạo"],
    "Thành tiền": ["Tổng tiền dòng"],
    "Số lượng": ["SL"],
    "Khách hàng": ["Tên KH"],
    "Đơn giá": ["Giá bán"],
    "Mã hàng": ["Mã HH", "MÃ HÀNG"],
    "Tên hàng": ["Ten hang", "Tên HH"],
    "Đơn vị tính": ["ĐVT"],
    "Số lượng quy đổi": ["SL quy đổi"],
    "Mã khách hàng": ["Mã KH"],
    "Mã nhập hàng": ["Mã NH"],
    "Tổng tiền hàng": ["Tiền hàng"],
}


def strip_accents(s: str) -> str:
    s = s.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


GENERIC_RULES = [
    ("strip_accents", strip_accents),
    ("lower", str.lower),
    ("underscore", lambda s: re.sub(r"\s+", "_", s.strip())),
]

EMPTY = (None, None)  # (value, number_format)


@dataclass
class Sheet:
    name: str
    rows: list  # list of rows; each row is a list of (value, number_format)


@dataclass
class CsvMeta:
    encoding: str
    delimiter: str
    newline: str


# ---------------------------------------------------------------- helpers

def is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def trim(rows):
    """Drop trailing empty rows and columns, pad rows to a rectangle."""
    rows = [list(r) for r in rows]
    while rows and all(is_empty(c[0]) for c in rows[-1]):
        rows.pop()
    n = max((len(r) for r in rows), default=0)
    rows = [r + [EMPTY] * (n - len(r)) for r in rows]
    while n and all(is_empty(r[n - 1][0]) for r in rows):
        n -= 1
        rows = [r[:n] for r in rows]
    return rows


NUM_RE = re.compile(r"^-?(?:0|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)(?:\.\d+)?$")


def parse_number(v):
    """Return (number, kind) or None. kind is 'str' for text such as '560,000'."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v, "num"
    if isinstance(v, str):
        s = v.strip()
        if NUM_RE.match(s):
            clean = s.replace(",", "")
            return (float(clean) if "." in clean else int(clean)), "str"
    return None


DATE_PATTERNS = [
    (re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?:[ T].*)?$"), "dmy"),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$"), "ymd"),
    (re.compile(r"^(\d{1,2})\s+thg\s+(\d{1,2})\s+(\d{4})(?:\s.*)?$", re.I), "dmy"),
]


def parse_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip()
    for rx, order in DATE_PATTERNS:
        m = rx.match(s)
        if m:
            a, b, c = (int(x) for x in m.groups())
            d, mo, y = (a, b, c) if order == "dmy" else (c, b, a)
            try:
                return dt.date(y, mo, d)
            except ValueError:
                return None
    return None


def data_rows(sheets, inserted=0) -> int:
    """Original data rows: all rows minus one header per sheet minus inserted rows."""
    return sum(max(len(sh.rows) - 1, 0) for sh in sheets) - inserted


# ---------------------------------------------------------------- readers / writers

def read_xlsx(path: Path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets:
            rows = [[(None if is_empty(c.value) else c.value, c.number_format) for c in row]
                    for row in ws.iter_rows()]
            sheets.append(Sheet(ws.title, trim(rows)))
    finally:
        wb.close()
    return sheets


def sniff_delimiter(text: str) -> str:
    head = text[:20000]
    best, best_width = ",", 1
    for d in [",", ";", "\t", "|"]:
        rows = list(csv.reader(io.StringIO(head, newline=""), delimiter=d))[:50]
        if len(rows) > 2:
            rows = rows[:-1]  # last line may be cut by the 20k head
        if not rows:
            continue
        width = len(rows[0])
        consistent = sum(len(r) == width for r in rows) / len(rows)
        if width > best_width and consistent >= 0.9:
            best, best_width = d, width
    return best


def read_csv(path: Path):
    raw = path.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        enc = "utf-8-sig"
    else:
        try:
            raw.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError:
            enc = "cp1258"
    text = raw.decode(enc)
    newline = "\r\n" if "\r\n" in text else "\n"
    delim = sniff_delimiter(text)
    raw_rows = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    rows = [[(None if c == "" else c, None) for c in r] for r in raw_rows]
    return [Sheet(path.stem, trim(rows))], CsvMeta(enc, delim, newline)


def unique_sheet_name(name: str, used: set) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "-", name).strip()[:31] or "Sheet"
    cand, i = base, 2
    while cand in used:
        suffix = f"_{i}"
        cand = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(cand)
    return cand


def unique_file(name: str, used: set) -> str:
    p = Path(name)
    cand, i = name, 2
    while cand in used:
        cand = f"{p.stem}_{i}{p.suffix}"
        i += 1
    used.add(cand)
    return cand


def write_xlsx(path: Path, sheets):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used = set()
    for sh in sheets or [Sheet("Sheet1", [])]:
        ws = wb.create_sheet(unique_sheet_name(sh.name, used))
        for i, row in enumerate(sh.rows, 1):
            for j, (v, f) in enumerate(row, 1):
                if v is None:
                    continue
                c = ws.cell(row=i, column=j, value=v)
                if isinstance(v, str):
                    c.data_type = "s"  # never let a text value become a formula
                if f and f != "General":
                    c.number_format = f
    wb.save(path)
    wb.close()


def write_csv(path: Path, sheet: Sheet, meta: CsvMeta):
    with open(path, "w", encoding=meta.encoding, newline="") as fh:
        w = csv.writer(fh, delimiter=meta.delimiter, lineterminator=meta.newline)
        for row in sheet.rows:
            w.writerow(["" if v is None else v for v, _ in row])


# ---------------------------------------------------------------- operators
# Each operator: (sheets, base_name, rng) -> (sheets, info, changed)
# info may contain "inserted_rows" (rows added by the op, excluded from counts).

def _variant(text: str, rng: random.Random):
    key = text.strip()
    if key in HEADER_VARIANTS:
        return rng.choice(HEADER_VARIANTS[key]), "table"
    start = rng.randrange(len(GENERIC_RULES))
    for k in range(len(GENERIC_RULES)):
        name, fn = GENERIC_RULES[(start + k) % len(GENERIC_RULES)]
        out = fn(text)
        if out != text and not is_empty(out):
            return out, name
    return text, None


def op_rename_headers(sheets, base, rng):
    changes = []
    for sh in sheets:
        if not sh.rows:
            continue
        header = sh.rows[0]
        for j, (v, f) in enumerate(header):
            if not isinstance(v, str) or is_empty(v):
                continue
            new, rule = _variant(v, rng)
            if new != v:
                header[j] = (new, f)
                changes.append({"sheet": sh.name, "col": j, "before": v,
                                "after": new, "rule": rule})
    return sheets, {"header_changes": changes}, bool(changes)


def op_reorder_columns(sheets, base, rng):
    perms, changed = {}, False
    for sh in sheets:
        n = len(sh.rows[0]) if sh.rows else 0
        if n < 2:
            continue
        perm = list(range(n))
        rng.shuffle(perm)
        if perm == list(range(n)):
            perm = perm[1:] + perm[:1]
        sh.rows = [[row[p] for p in perm] for row in sh.rows]
        perms[sh.name] = perm  # new column k holds original column perm[k]
        changed = True
    return sheets, {"column_permutation": perms}, changed


def op_add_title_rows(sheets, base, rng):
    title = f"{TITLE_PREFIX} {base}"
    inserted = 0
    for sh in sheets:
        if not sh.rows:
            continue
        n = len(sh.rows[0])
        head = [(title, None)] + [EMPTY] * (n - 1)
        sh.rows = [head, [EMPTY] * n] + sh.rows
        inserted += 2
    return sheets, {"title_text": title, "inserted_rows": inserted}, inserted > 0


def _date_column(data, n):
    for j in range(n):
        vals = [row[j][0] for row in data if not is_empty(row[j][0])]
        if not vals:
            continue
        ok = sum(parse_date(v) is not None for v in vals)
        if ok > 0 and ok / len(vals) >= DATE_SHARE:
            return j
    return None


def op_split_sheets_by_month(sheets, base, rng):
    out, details, changed = [], {}, False
    for sh in sheets:
        if len(sh.rows) < 2:
            out.append(sh)
            continue
        header, data = sh.rows[0], sh.rows[1:]
        col = _date_column(data, len(header))
        if col is None:
            out.append(sh)
            details[sh.name] = {"skipped": "no_date_column"}
            continue
        keys = [(d.year, d.month) if (d := parse_date(row[col][0])) else None
                for row in data]
        cur = next(k for k in keys if k)  # guaranteed by _date_column
        filled = []
        for k in keys:  # undated rows follow the previous dated row
            if k is None:
                filled.append(cur)
            else:
                cur = k
                filled.append(k)
        months = sorted(set(filled))
        if len(months) < 2:
            out.append(sh)
            details[sh.name] = {"skipped": "single_month"}
            continue
        groups = {m: [] for m in months}
        for row, k in zip(data, filled):
            groups[k].append(row)
        counts = {}
        for m in months:
            name = f"T{m[1]:02d}-{m[0]}"
            out.append(Sheet(name, [list(header)] + groups[m]))
            counts[name] = len(groups[m])
        details[sh.name] = {"date_column": header[col][0],
                            "unparsed_date_rows": sum(k is None for k in keys),
                            "sheets": counts}
        changed = True
    return out, {"split": details, "split_outputs": True}, changed


def _numeric_columns(data, n):
    """Map column index -> (kind, number_format) for columns that are mostly numeric."""
    found = {}
    for j in range(1, n):
        cells = [c for c in (row[j] for row in data) if not is_empty(c[0])]
        if not cells:
            continue
        parsed = [parse_number(c[0]) for c in cells]
        ok = [i for i, p in enumerate(parsed) if p is not None]
        if ok and len(ok) / len(cells) >= NUMERIC_SHARE:
            kind = "str" if any(parsed[i][1] == "str" for i in ok) else "num"
            found[j] = (kind, cells[ok[0]][1])
    return found


def _sum_column(block, j, kind, fmt):
    total, count = 0, 0
    for row in block:
        p = parse_number(row[j][0])
        if p is not None:
            total += p[0]
            count += 1
    if count == 0:
        return None
    integral = float(total).is_integer()
    if kind == "str":  # keep the text style of the column ("1,234,000")
        return (f"{int(total):,}" if integral else f"{total:,.2f}"), None
    return (int(total) if integral else round(total, 2)), fmt


def op_add_subtotal_rows(sheets, base, rng):
    inserted, details = 0, {}
    for sh in sheets:
        if len(sh.rows) < 2:
            continue
        header, data = sh.rows[0], sh.rows[1:]
        n = len(header)
        numeric = _numeric_columns(data, n)
        new, blocks = [header], []
        for b, start in enumerate(range(0, len(data), SUBTOTAL_BLOCK), 1):
            block = data[start:start + SUBTOTAL_BLOCK]
            new.extend(block)
            sub = [EMPTY] * n
            sub[0] = (SUBTOTAL_LABEL, None)
            sums = {}
            for j, (kind, fmt) in numeric.items():
                res = _sum_column(block, j, kind, fmt)
                if res is not None:
                    sub[j] = res
                    sums[str(header[j][0])] = res[0]
            new.append(sub)
            inserted += 1
            blocks.append({"block": b, "data_rows": len(block), "sums": sums})
        sh.rows = new
        details[sh.name] = blocks
    return sheets, {"subtotal_blocks": details, "inserted_rows": inserted}, inserted > 0


OPS_FUNCS = {
    "rename_headers": op_rename_headers,
    "reorder_columns": op_reorder_columns,
    "split_sheets_by_month": op_split_sheets_by_month,
    "add_title_rows": op_add_title_rows,
    "add_subtotal_rows": op_add_subtotal_rows,
}


# ---------------------------------------------------------------- pipeline

def process_file(src: Path, dst_dir: Path, op: str, biz: str, seed: int) -> dict:
    name, ext = src.name, src.suffix.lower()
    entry = {"file": name}
    if ext not in (".xlsx", ".xls", ".csv"):
        shutil.copy2(src, dst_dir / name)
        entry.update(status="copied_unchanged", reason="not a table file")
        return entry
    if ext == ".xls":
        entry.update(status="skipped", reason="xls needs pandas/xlrd, not in venv")
        return entry
    try:
        if ext == ".xlsx":
            sheets, meta = read_xlsx(src), None
        else:
            sheets, meta = read_csv(src)
    except Exception as exc:  # unreadable file: record and move on
        entry.update(status="skipped", reason=f"read error: {exc}")
        return entry

    before = data_rows(sheets)
    rng = random.Random(f"{seed}|{op}|{biz}|{name}")
    new_sheets, info, changed = OPS_FUNCS[op](sheets, src.stem, rng)
    entry.update(info)
    entry["data_rows_before"] = before

    if not changed:
        shutil.copy2(src, dst_dir / name)
        entry.update(status="unchanged", outputs=[name],
                     reason="operator not applicable to this file")
        return entry

    if ext == ".xlsx":
        write_xlsx(dst_dir / name, new_sheets)
        outputs = [name]
    else:
        if len(new_sheets) == 1 and not info.get("split_outputs"):
            outs = [(name, new_sheets[0])]
        else:
            used, outs = set(), []
            for sh in new_sheets:
                fname = unique_file(f"{src.stem}_{sh.name}{src.suffix}", used)
                outs.append((fname, sh))
        for fname, sh in outs:
            write_csv(dst_dir / fname, sh, meta)
        outputs = [o[0] for o in outs]

    entry.update(status="split" if info.get("split_outputs") else "changed",
                 outputs=outputs,
                 data_rows_after=data_rows(new_sheets, info.get("inserted_rows", 0)),
                 sheets_after=[s.name for s in new_sheets])
    return entry


def process_shop(biz: str, op: str, seed: int, src_data: Path, out_root: Path) -> dict:
    src_dir = src_data / biz
    dst_dir = out_root / op / "data" / biz
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True)
    files = {}
    for src in sorted(p for p in src_dir.iterdir() if p.is_file()):
        files[src.name] = process_file(src, dst_dir, op, biz, seed)
    manifest = {"shop": biz, "op": op, "seed": seed, "files": files}
    (dst_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    return summarize(manifest, dst_dir)


def summarize(manifest: dict, dst_dir: Path) -> dict:
    statuses = [f["status"] for f in manifest["files"].values()]
    examples = []
    for f in manifest["files"].values():
        for c in f.get("header_changes", []):
            if len(examples) < 3:
                examples.append(f"{c['before']} -> {c['after']}")
    table = [f for f in manifest["files"].values() if "data_rows_before" in f]
    return {
        "shop": manifest["shop"],
        "op": manifest["op"],
        "changed": sum(s in ("changed", "split") for s in statuses),
        "unchanged": sum(s == "unchanged" for s in statuses),
        "skipped": sum(s == "skipped" for s in statuses),
        "copied": sum(s == "copied_unchanged" for s in statuses),
        "data_rows_before": sum(f["data_rows_before"] for f in table),
        "data_rows_after": sum(f.get("data_rows_after", f["data_rows_before"]) for f in table),
        "examples": examples,
        "size_bytes": sum(p.stat().st_size for p in dst_dir.rglob("*") if p.is_file()),
    }


def default_shops(src_data: Path):
    ids = sorted(d.name for d in src_data.iterdir() if d.is_dir())
    return ids[::4]


def write_test_root(op: str, out_root: Path, src_root: Path, questions_path: Path) -> int:
    root = out_root / op
    shops = {d.name for d in (root / "data").iterdir() if d.is_dir()}
    with open(questions_path, encoding="utf-8") as fin, \
            open(root / "questions.jsonl", "w", encoding="utf-8") as fout:
        for line in fin:
            if line.strip() and json.loads(line).get("business_id") in shops:
                fout.write(line if line.endswith("\n") else line + "\n")
    for name in ("rulebook.md", "calendar.csv"):
        link = root / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(src_root / name)
    return len(shops)


def generate(op, shops, seed, src_data=SRC_DATA, out_root=OUT_ROOT,
             questions_path=QUESTIONS, src_root=SRC_ROOT, make_root=True):
    shops = shops or default_shops(src_data)
    summaries = []
    for biz in shops:
        if not (src_data / biz).is_dir():
            raise SystemExit(f"unknown shop: {biz}")
        summaries.append(process_shop(biz, op, seed, src_data, out_root))
    if make_root:
        write_test_root(op, out_root, src_root, questions_path)
    return summaries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--op", choices=OPS)
    ap.add_argument("--all-ops", action="store_true")
    ap.add_argument("--shops", nargs="*", help="default: every 4th shop of sorted ids")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    if not args.op and not args.all_ops:
        ap.error("give --op or --all-ops")
    for op in (OPS if args.all_ops else [args.op]):
        for s in generate(op, args.shops or None, args.seed):
            print(f"{s['op']} {s['shop']}: changed={s['changed']} unchanged={s['unchanged']} "
                  f"skipped={s['skipped']} copied={s['copied']} "
                  f"data_rows {s['data_rows_before']}->{s['data_rows_after']} "
                  f"size={s['size_bytes'] / 1e6:.2f}MB examples={s['examples']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
