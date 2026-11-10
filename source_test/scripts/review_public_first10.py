"""Independent, question-specific raw-source audit; never changes solver outputs.

This audit intentionally does not import the solver, its parsers, or its SQL.
The ten questions are frozen in artifacts/public_first10/selection.json.
Run from source_test with: python -X utf8 -u -m scripts.review_public_first10
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/public_test/data"
OUT = ROOT / "artifacts/public_first10"


def clean(value) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


def key(value) -> str:
    return clean(value).casefold()


def plain(value) -> str:
    text = key(value).replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def number(value, *, scaled=False) -> Decimal:
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = plain(value).replace(" ", "").replace("vnd", "").replace("d", "")
    match = re.fullmatch(r"(-?[\d.,]+)tr(\d+)?", text)
    if match:
        whole = number(match[1], scaled=True) * 1_000_000
        return whole + (Decimal("0." + match[2]) * 1_000_000 if match[2] else 0)
    if text.endswith("k"):
        return number(text[:-1], scaled=True) * 1000
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in text or "." in text:
        sep = "," if "," in text else "."
        parts = text.split(sep)
        if not scaled and all(len(part) == 3 for part in parts[1:]):
            text = "".join(parts)
        else:
            text = text.replace(sep, ".")
    return Decimal(text)


def quantity(value) -> Decimal:
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    match = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", str(value))
    if not match:
        raise ValueError(f"Cannot parse quantity {value!r}")
    return Decimal(match[1].replace(",", "."))


def day(value, year=None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean(value)
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{5}(?:\.\d+)?", text):
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    if re.match(r"^\d{4}-\d\d-\d\d", text):
        return date.fromisoformat(text[:10])
    m = re.fullmatch(r"(\d{1,2})\s+thg\s+(\d{1,2})\s+(\d{4})", text)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    parts = re.split(r"[/.-]", text)
    if len(parts) in (2, 3):
        y = int(parts[2]) if len(parts) == 3 else year
        if y is None:
            raise ValueError(f"Year missing: {value!r}")
        return date(y + 2000 if y < 100 else y, int(parts[1]), int(parts[0]))
    raise ValueError(f"Cannot parse date {value!r}")


def invalid_status(value) -> bool:
    text = plain(value)
    return any(s in text for s in (
        "huy", "cancel", "khong giao duoc", "tra hang", "khach tra",
        "tra lai", "doi tra", "hang tra ve", "hoan hang", "hoan tra", "returned", "return",
    ))


def excel(path: Path, sheet=None, header=1):
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = [book[sheet]] if sheet else book.worksheets
        for ws in sheets:
            rows = iter(ws.values)
            for _ in range(header):
                headings = next(rows)
            headings = [clean(x) for x in headings]
            for line, values in enumerate(rows, start=header + 1):
                record = dict(zip(headings, (*values, *([None] * len(headings)))))
                yield ws.title, line, record
    finally:
        book.close()


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for line, row in enumerate(reader, start=2):
            yield path.stem, line, row


def grouped(rows, id_col, product_col, inherit):
    """Fill only declared group fields, reset carry at a new nonempty order ID."""
    active = None
    carry = {}
    for sheet, line, row in rows:
        if not clean(row.get(product_col)):
            continue  # blank, footer, and amount-only total rows
        if key(row.get(product_col)) == key(product_col) or key(row.get(id_col)) == key(id_col):
            continue  # repeated export header inside the worksheet
        order = clean(row.get(id_col))
        if order and order != active:
            carry = {}
            active = order
        if not active:
            continue
        row[id_col] = order or active
        for col in inherit:
            if clean(row.get(col)):
                carry[col] = row[col]
            elif col in carry:
                row[col] = carry[col]
        yield sheet, line, row


def deduplicate(rows):
    seen = set()
    for sheet, line, row in rows:
        identity = tuple((name, clean(value)) for name, value in row.items())
        if identity not in seen:
            seen.add(identity)
            yield sheet, line, row


def source_ref(path, sheet, line):
    return {"file": str(path.relative_to(ROOT)), "sheet": sheet, "row": line}


def monetary_case(bid, filenames, sheet, header, id_col, product_col, date_col,
                  status_col, amount_col, start, end, *, target=None, time_col=None,
                  qty_col=None, average=False, inherit=()):
    """Scan every specified source for whole-order cancellations/returns first."""
    invalid = set()
    selected = []
    statuses = Counter()
    scanned = 0
    for filename in filenames:
        path = DATA / bid / filename
        iterator = csv_rows(path) if path.suffix == ".csv" else excel(path, sheet, header)
        rows = grouped(iterator, id_col, product_col, (date_col, status_col, *inherit))
        for tab, line, row in deduplicate(rows):
            scanned += 1
            oid = clean(row[id_col])
            statuses[clean(row.get(status_col))] += 1
            bad = invalid_status(row.get(status_col))
            if qty_col and clean(row.get(qty_col)):
                bad |= quantity(row[qty_col]) < 0
            if bad:
                invalid.add(oid)
            # Year comes from sheet for day/month-only records.
            y = re.search(r"20\d{2}", tab)
            when = day(row[date_col], int(y[0]) if y else None)
            if not (date.fromisoformat(start) <= when < date.fromisoformat(end)):
                continue
            if target and key(row[product_col]) != key(target):
                continue
            if time_col:
                value = row[time_col]
                hour = value.hour if isinstance(value, datetime) else int(clean(value)[11:13])
                if not 14 <= hour < 22:
                    continue
            selected.append({"order": oid, "amount": number(row[amount_col]),
                             "product": clean(row[product_col]),
                             "source": source_ref(path, tab, line),
                             "raw_amount": row[amount_col]})
    kept = [row for row in selected if row["order"] not in invalid]
    total = sum((row["amount"] for row in kept), Decimal(0))
    orders = len({row["order"] for row in kept})
    value = total / orders if average and orders else total
    return {"independent_answer": int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            "total_revenue": str(total), "valid_orders": orders,
            "valid_rows": len(kept), "excluded_rows_in_window": len(selected) - len(kept),
            "scanned_deduplicated_rows": scanned, "raw_status_counts": dict(statuses),
            "window": [start, end], "window_end_exclusive": True,
            "contributions": kept}


def audit_dev002():
    result = monetary_case("dev_002", ["BanHang_T3_2025.xlsx"], "DonHang_ChiTiet", 4,
        "Mã đơn hàng", "Tên sản phẩm", "Ngày đặt hàng", "Trạng thái đơn hàng", "Tổng tiền",
        "2025-03-04", "2025-03-05", inherit=("Tên khách hàng",))
    schemas = []
    directory = DATA / "dev_002"
    for path in sorted(directory.glob("*.xlsx")):
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in book:
                header = 1 if path.name == "DanhSachQuyDoi.xlsx" else 4
                rows = iter(ws.values)
                for _ in range(header):
                    headings = next(rows)
                schemas.append({"file": path.name, "sheet": ws.title,
                                "headers": [clean(v) for v in headings]})
        finally:
            book.close()
    assert not any("nhóm" in key(h) for s in schemas for h in s["headers"])
    return {"q_00bf8df3ea0b": result,
            "q_008fdebb6ec9": {"independent_type": "unanswerable", "independent_answer": None,
                "reason": "Không có nhóm hàng trong 17 file bán hàng hoặc bảng quy đổi; rulebook §3 và §7 không cho suy nhóm trà từ tên món.",
                "schema_evidence": schemas,
                "files_in_package": sorted(p.name for p in directory.iterdir())}}


def audit_dev001():
    return {"q_01d1fc600b26": monetary_case("dev_001", ["So_chi_tiet_ban_hang.xlsx"],
        "Sheet1", 4, "Mã đơn hàng", "Tên hàng hóa", "Ngày", "Trạng thái", "Thành tiền",
        "2025-07-01", "2025-08-01", target="Bia Heineken 330ml", inherit=("Khách hàng",))}


def audit_dev003():
    return {"q_01ac0fc9f2a2": monetary_case("dev_003",
        ["BanHang_TongHop_2025.xlsx", "BanHang_TongHop_2026.xlsx"], "BanHang", 1,
        "Đơn số", "Hàng hóa", "Giờ tạo đơn", "Trạng thái giao", "Thành tiền (đ)",
        "2026-08-10", "2026-08-17", average=True,
        inherit=("Khách hàng (tên)", "Mã khách", "SĐT"))}


def audit_dev004():
    return {"q_013b4ff35811": monetary_case("dev_004", ["SoBanHang.xlsx"], None, 2,
        "Ma don", "Hàng bán", "Thời gian", "Trạng thái xử lý", "Amount",
        "2025-11-10", "2025-11-17", qty_col="SL bán", inherit=("Khách hàng (tên)",))}


def audit_dev007():
    return {"q_014cdcc1da6e": monetary_case("dev_007", ["chi_tiet_hoa_don.csv"], None, 1,
        "Mã bill", "Mặt hàng", "Ngày giao dịch", "Tiến độ", "Thành tiền",
        "2026-08-01", "2026-09-01", time_col="Giờ bán", inherit=("Họ tên khách",))}


def audit_dev009():
    path = DATA / "dev_009/danh_sach_hoa_don.csv"
    orders = {}
    all_states = Counter()
    for tab, line, row in csv_rows(path):
        oid = clean(row["Mã hóa đơn"])
        if not oid:
            continue
        when = day(row["Ngày tháng"])
        if when != date(2026, 2, 25):
            continue
        all_states[clean(row["Trạng thái đơn"]) ] += 1
        if not invalid_status(row["Trạng thái đơn"]):
            orders[oid] = source_ref(path, tab, line)
    return {"q_0329c9711c55": {"independent_answer": len(orders),
        "raw_status_counts_in_window": dict(all_states), "orders": orders,
        "window": ["2026-02-25", "2026-02-26"]}}


def audit_dev006():
    path = DATA / "dev_006/pos.sqlite"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    products = {(clean(r["IDCuaHang"]), clean(r["MaHangHoa"])): dict(r)
                for r in db.execute("SELECT * FROM san_pham")}
    orders = {}
    bad = set()
    for row in db.execute("SELECT * FROM don_hang"):
        identity = clean(row["IDCuaHang"]), clean(row["MaHoaDon"])
        if invalid_status(row["TinhTrang"]):
            bad.add(identity)
        if date(2026, 6, 22) <= day(row["NgayLap"]) < date(2026, 6, 26):
            orders[identity] = dict(row)
    # Explicit return records are a second independent whole-order check.
    for row in db.execute("SELECT * FROM tra_hang"):
        bad.add((clean(row["IDCuaHang"]), clean(row["MaHoaDon"])))
    contributions = []
    seen = set()
    for row in db.execute("SELECT rowid AS source_rowid, * FROM chi_tiet_don"):
        order = clean(row["IDCuaHang"]), clean(row["MaHoaDon"])
        if order not in orders or order in bad:
            continue
        p = products[(order[0], clean(row["MaHangHoa"]))]
        if key(p["DonViCoSo"]) != "gói":
            continue
        signature = tuple(row)[1:]
        if signature in seen:
            continue
        seen.add(signature)
        contributions.append({"order": order, "product_code": row["MaHangHoa"],
            "name": p["TenGoi"], "quantity_base": row["SLQuyDoi"],
            "source_rowid": row["source_rowid"]})
    db.close()
    return {"q_033e37dde4d5": {"independent_answer": sum(r["quantity_base"] for r in contributions),
        "window": ["2026-06-22", "2026-06-26"], "contributions": contributions}}


def audit_dev013():
    base = DATA / "dev_013"
    groups = {}
    for _, _, row in excel(base / "QuanLy.xlsx", "DanhMuc"):
        if key(row.get("Nhóm")) == "đồ dùng":
            names = [row["Tên hàng"]] + str(row.get("Tên gọi khác") or "").split(",")
            for name in names:
                if clean(name):
                    groups[key(name)] = clean(row["Tên hàng"])
    contributions = []
    bad = set()
    statuses = Counter()
    sales = grouped(excel(base / "BanHang_2026.xlsx", "T06-2026", 2),
                    "HĐ", "Tên hàng", ("Ngày", "Ghi chú", "Khách", "Kênh"))
    for tab, line, row in deduplicate(sales):
        oid = clean(row["HĐ"])
        statuses[clean(row.get("Ghi chú"))] += 1
        if invalid_status(row.get("Ghi chú")):
            bad.add(oid)
        if key(row["Tên hàng"]) in groups:
            assert date(2026, 6, 1) <= day(row["Ngày"], 2026) < date(2026, 7, 1)
            contributions.append({"order": oid, "product": row["Tên hàng"],
                "amount": number(row["TT"]), "source": source_ref(base / "BanHang_2026.xlsx", tab, line)})
    kept = [r for r in contributions if r["order"] not in bad]
    return {"q_00c4e24fa228": {"independent_answer": int(sum((r["amount"] for r in kept), Decimal(0))),
        "valid_rows": len(kept), "excluded_rows_in_window": len(contributions)-len(kept),
        "group_product_names": sorted(set(groups.values())),
        "raw_status_counts": dict(statuses), "contributions": kept,
        "window": ["2026-06-01", "2026-07-01"]}}


def audit_dev008():
    bid = "dev_008"
    selected = []
    bad = set()
    coverage = {}
    statuses = Counter()
    for name in ("ChiTietHoaDon_1.xlsx", "ChiTietHoaDon_2.xlsx"):
        path = DATA / bid / name
        low, high = None, None
        rows = grouped(excel(path, "Sheet1"), "Bill", "Tên hàng", ("Ngày xuất", "TT đơn"))
        for tab, line, row in deduplicate(rows):
            oid = clean(row["Bill"])
            statuses[clean(row.get("TT đơn"))] += 1
            if invalid_status(row.get("TT đơn")):
                bad.add(oid)
            when = day(row["Ngày xuất"])
            low = min(low, when) if low else when
            high = max(high, when) if high else when
            if date(2025, 11, 3) <= when < date(2025, 11, 10) and key(row["Tên hàng"]) != "phí phục vụ":
                selected.append({"order": oid, "product_code": clean(row["Ma hang"]),
                    "name": clean(row["Tên hàng"]), "qty": quantity(row["SL lẻ"]),
                    "source": source_ref(path, tab, line)})
        coverage[name] = [str(low), str(high)]
    totals = defaultdict(Decimal)
    names = defaultdict(Counter)
    for row in selected:
        if row["order"] not in bad:
            totals[row["product_code"]] += row["qty"]
            names[row["product_code"]][row["name"]] += 1
    ranking = [{"code": code, "quantity_base": str(totals[code]),
                "names": dict(names[code]), "representative": names[code].most_common(1)[0][0]}
               for code in sorted(totals, key=lambda code: (-totals[code], code))]
    return {"q_01ba91ff94c6": {"independent_answer": [r["representative"] for r in ranking[:5]],
        "ranking_by_product_code": ranking, "source_date_coverage": coverage,
        "raw_status_counts": dict(statuses),
        "window": ["2025-11-03", "2025-11-10"],
        "contributions": [r for r in selected if r["order"] not in bad]}}


def main():
    OUT.mkdir(exist_ok=True)
    tasks = [audit_dev001, audit_dev002, audit_dev003, audit_dev004, audit_dev006,
             audit_dev007, audit_dev008, audit_dev009, audit_dev013]
    results = {}
    with ProcessPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(task): task.__name__ for task in tasks}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                results.update(result)
                (OUT / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                print(name, {k: v["independent_answer"] for k, v in result.items()}, flush=True)
            except Exception as exc:
                print(name, type(exc).__name__, str(exc), flush=True)
                results[name] = {"error": repr(exc)}
    source_paths = [p for bid in ("dev_001", "dev_002", "dev_003", "dev_004", "dev_006", "dev_007", "dev_008", "dev_009", "dev_013")
                    for p in (DATA / bid).iterdir() if p.is_file()]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    (OUT / "raw_audit.json").write_text(json.dumps({"results": results, "source_hashes": hashes,
        "official_gold_available": False, "audit_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
