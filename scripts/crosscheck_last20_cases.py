"""Second raw-reader checks for the wrong vehicle count and two abstentions."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/public_test/data"
OUT = ROOT / "artifacts/public_last20_review"


def raw_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{5}(?:\.\d+)?", str(value)):
        return from_excel(float(value)).date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unexpected raw date {value!r}")


def raw_money(value):
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = re.sub(r"\s", "", str(value).lower()).replace("đ", "").replace("vnd", "")
    if "tr" in text:
        whole, fraction = text.split("tr")
        result = Decimal(whole.replace(",", ".")) * 1_000_000
        return result + (Decimal("0." + fraction) * 1_000_000 if fraction else 0)
    if text.endswith("k"):
        return Decimal(text[:-1].replace(",", ".")) * 1000
    return Decimal(text.replace(".", "").replace(",", ""))


def vehicle_check():
    book = load_workbook(DATA / "dev_012/SoSuaXe.xlsx", read_only=True, data_only=True)
    by_plate, labels = defaultdict(list), set()
    for line, values in enumerate(book["T8-2026"].iter_rows(min_row=3, values_only=True), 3):
        if len(values) < 2 or not values[1]:
            continue
        label = str(values[1]).strip()
        if not re.match(r"^sh\s*mode\s", label, flags=re.I):
            continue
        match = re.search(r"\b(\d{2}[A-Z]\d)-(\d{3})\.(\d{2})$", label, flags=re.I)
        assert match, label
        plate = "".join(match.groups()).upper()
        by_plate[plate].append({"row": line, "raw_vehicle": label})
        labels.add(label)
    book.close()
    return {"raw_distinct_vehicle_strings": len(labels), "distinct_plates": len(by_plate),
        "duplicate_plates": {p: rows for p, rows in by_plate.items() if len(rows) > 1}}


def pos_debt_check():
    db = sqlite3.connect((DATA / "dev_006/pos.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    db.create_function("audit_day", 1, lambda v: raw_date(v).isoformat())
    db.create_function("audit_money", 1, lambda v: int(raw_money(v)))
    customer, = db.execute("SELECT MaKhachHang FROM khach_hang WHERE TenGoi = ?", ("Lê Sơn Trung",)).fetchone()
    incurred, = db.execute("""
        WITH entries AS (
          SELECT o.IDCuaHang, o.MaHoaDon, o.TongThanhToan,
            COALESCE(SUM(CASE WHEN substr(p.NgayGioThu,1,10) = audit_day(o.NgayLap)
                        THEN audit_money(p.SoTienThu) ELSE 0 END),0) AS paid_on_purchase_day
          FROM don_hang o LEFT JOIN thanh_toan p
            ON p.IDCuaHang=o.IDCuaHang AND p.MaHoaDon=o.MaHoaDon
          WHERE o.MaKhachHang=? AND o.PhuongThucTT='debt' AND o.TinhTrang NOT LIKE 'cancel%'
            AND audit_day(o.NgayLap)>='2026-07-01' AND audit_day(o.NgayLap)<'2026-08-01'
          GROUP BY o.IDCuaHang,o.MaHoaDon,o.TongThanhToan
        ) SELECT COALESCE(SUM(MAX(TongThanhToan-paid_on_purchase_day,0)),0) FROM entries
    """, (customer,)).fetchone()
    repaid, = db.execute("""
        SELECT COALESCE(SUM(audit_money(p.SoTienThu)),0)
        FROM thanh_toan p JOIN don_hang o
          ON p.IDCuaHang=o.IDCuaHang AND p.MaHoaDon=o.MaHoaDon
        WHERE o.MaKhachHang=? AND o.PhuongThucTT='debt' AND o.TinhTrang NOT LIKE 'cancel%'
          AND substr(p.NgayGioThu,1,10)>='2026-07-01' AND substr(p.NgayGioThu,1,10)<'2026-08-01'
          AND substr(p.NgayGioThu,1,10)>audit_day(o.NgayLap)
    """, (customer,)).fetchone()
    db.close()
    return {"expected_type": "list", "expected": [incurred, repaid], "customer_code": customer}


def installment_check():
    contracts, payments = {}, defaultdict(list)
    book = load_workbook(DATA / "dev_014/SoNo.xlsx", read_only=True, data_only=True)
    for row in book["Trả góp"].iter_rows(min_row=2, values_only=True):
        if len(row) < 12 or row[1] is None:
            continue
        oid = int(row[1])
        if row[0] is not None:
            assert oid not in contracts
            contracts[oid] = {"purchase_date": raw_date(row[0]),
                "principal": raw_money(row[5]) - raw_money(row[6])}
        if row[10] is not None:
            payments[oid].append((raw_date(row[10]), raw_money(row[11])))
    book.close()
    cancelled = set()
    book = load_workbook(DATA / "dev_014/SoBanHang.xlsx", read_only=True, data_only=True)
    for sheet in book.worksheets:
        if sheet.title == "Ghi tắt":
            continue
        active = None
        for row in sheet.iter_rows(min_row=3, values_only=True):
            if len(row) < 4 or not row[3]:
                continue
            if row[1] is not None:
                active = int(row[1])
            comment = str(row[10] or "") if len(row) > 10 else ""
            text = "".join(c for c in unicodedata.normalize("NFD", comment.lower()) if not unicodedata.combining(c))
            if "huy" in text or "cancel" in text:
                cancelled.add(active)
    book.close()
    cutoff = date(2025, 10, 18)
    included = {oid: c for oid, c in contracts.items() if c["purchase_date"] <= cutoff and oid not in cancelled}
    principal = sum((c["principal"] for c in included.values()), Decimal(0))
    paid = {oid: sum((value for when, value in payments[oid] if when <= cutoff), Decimal(0)) for oid in included}
    remaining = sum((max(c["principal"] - paid[oid], Decimal(0)) for oid, c in included.items()), Decimal(0))
    return {"expected": int(remaining), "contracts_before_cutoff": len(included),
        "principal": str(principal), "payments_at_cutoff": str(sum(paid.values(), Decimal(0))),
        "purchase_and_payment_grouping": "Direct STT keys; no row-order inference"}


def main():
    reference = json.loads((OUT / "raw_audit.json").read_text(encoding="utf-8"))
    vehicles, debt, installments = vehicle_check(), pos_debt_check(), installment_check()
    assert vehicles["distinct_plates"] == reference["q_07e2508ad671"]["expected"]
    assert debt["expected"] == reference["q_0a9819711554"]["expected"]
    assert installments["expected"] == reference["q_0594583b3d46"]["expected"]
    result = {"vehicle_count": vehicles, "customer_debt": debt, "installments": installments,
        "all_three_checks_match_raw_audit": True}
    (OUT / "crosschecks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
