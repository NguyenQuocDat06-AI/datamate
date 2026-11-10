"""Raw-source checks for stock, credit and phone ledgers in the next public batch."""

import calendar
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from openpyxl import load_workbook

from scripts.next50_common import (DATA, answer, rounded, clean, day, deduplicate, excel,
    grouped, invalid_status, number, plain, quantity, total, customer_counts, repeat_percentage)
from scripts.review_public_next20 import customer_name


def workbook_sales(bid, filename, skip=()):
    book = load_workbook(DATA / bid / filename, read_only=True, data_only=True)
    sheets = [s for s in book.sheetnames if s not in skip]
    book.close()
    rows = []
    for sheet in sheets:
        for s, line, r in deduplicate(grouped(excel(DATA / bid / filename, sheet, 2),
                "STT", "Hàng", ("Ngày", "Ghi chú"))):
            year = int(re.search(r"20\d{2}", sheet)[0])
            r["Ngày"] = day(r["Ngày"], year).isoformat()
            rows.append((s, line, r))
    return rows


def audit_dev004():
    path = DATA / "dev_004/SoBanHang.xlsx"
    book = load_workbook(path, read_only=True, data_only=True)
    sheets = book.sheetnames
    book.close()
    rows, invalid, cancelled = [], set(), set()
    for sheet in sheets:
        for s, line, r in deduplicate(grouped(excel(path, sheet, 2), "Ma don", "Hàng bán",
                ("Thời gian", "Trạng thái xử lý", "Khách hàng (tên)"))):
            r["Thời gian"] = day(r["Thời gian"], int(sheet[-4:])).isoformat()
            oid = clean(r["Ma don"])
            if invalid_status(r.get("Trạng thái xử lý")) or quantity(r["SL bán"]) < 0:
                invalid.add(oid)
            if any(t in plain(r.get("Trạng thái xử lý")) for t in ("cancel", "huy", "khong giao duoc")):
                cancelled.add(oid)
            rows.append((s, line, r))
    eligible = [r for r in rows if clean(r[2]["Ma don"]) not in invalid]
    counts = customer_counts(eligible, "Thời gian", "Ma don", "Khách hàng (tên)", "2025-06-01", "2025-07-01")
    purchase_dates = {}
    for _, _, r in rows:
        if quantity(r["SL bán"]) > 0:
            oid = clean(r["Ma don"])
            purchase_dates[oid] = min(purchase_dates.get(oid, r["Thời gian"]), r["Thời gian"])
    future_due = []
    for _, line, r in excel(DATA / "dev_004/SoHanNo.xlsx", "Sheet1"):
        oid = clean(r["Mã đơn hàng"])
        if oid in purchase_dates and oid not in cancelled and purchase_dates[oid] <= "2025-12-10" < day(r["Ngày phải trả"]).isoformat():
            future_due.append({"row": line, "order": oid, "purchase_date": purchase_dates[oid],
                               "due_date": day(r["Ngày phải trả"]).isoformat()})
    return {"q_169a123623e6": repeat_percentage(counts, 3),
        "q_10831a6f3a2b": answer(len({r["order"] for r in future_due}), contributions=future_due,
            interpretation="Mấy đơn = số đơn; chỉ lọc đơn bán chịu đã đặt, không huỷ, ngày phải trả sau cutoff. Câu không nêu điều kiện còn dư nợ.")}


def audit_dev006():
    db = sqlite3.connect((DATA / "dev_006/pos.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    orders = {(r["IDCuaHang"], r["MaHoaDon"]): dict(r) for r in db.execute("SELECT * FROM don_hang")}
    returns = {(r["IDCuaHang"], r["MaHoaDon"]) for r in db.execute("SELECT * FROM tra_hang")}
    returns |= {(r["IDCuaHang"], r["MaHoaDon"]) for r in db.execute("SELECT * FROM chi_tiet_don_lo") if r["SLTraQuyDoi"] > 0}
    eligible = {oid for oid, r in orders.items() if not invalid_status(r["TinhTrang"]) and oid not in returns}
    counts = defaultdict(set)
    for oid in eligible:
        r = orders[oid]
        if r["MaKhachHang"] and "2025-12-01" <= day(r["NgayLap"]).isoformat() < "2026-01-01":
            counts[(oid[0], r["MaKhachHang"])].add(oid)
    revenues, seen = defaultdict(Decimal), set()
    for r in db.execute("SELECT * FROM chi_tiet_don"):
        if tuple(r) in seen:
            continue
        seen.add(tuple(r))
        oid = r["IDCuaHang"], r["MaHoaDon"]
        if oid in eligible:
            d = day(orders[oid]["NgayLap"])
            if d.year == 2025 and d.month in (7, 8):
                revenues[d.month] += number(r["TriGia"])
    cutoff = date(2026, 8, 8)
    last = cutoff + timedelta(days=90)
    receipts = {(r["IDCuaHang"], r["MaPhieuNhap"]): dict(r) for r in db.execute("SELECT * FROM phieu_nhap")}
    lots = {r["MaLo"]: dict(r) for r in db.execute("SELECT * FROM lo_hang")}
    assert len(lots) == db.execute("SELECT COUNT(*) FROM lo_hang").fetchone()[0]
    remaining = {}
    for code, lot in lots.items():
        receipt = receipts[(lot["IDCuaHang"], lot["MaPhieuNhap"])]
        if not invalid_status(receipt["TinhTrang"]) and day(receipt["NgayNhapKho"]) <= cutoff:
            remaining[code] = quantity(lot["SLNhapQuyDoi"])
    returned_at = defaultdict(list)
    for r in db.execute("SELECT * FROM tra_hang"):
        returned_at[(r["IDCuaHang"], r["MaHoaDon"])].append(day(r["NgayGioTra"]))
    seen.clear()
    for r in db.execute("SELECT * FROM chi_tiet_don_lo"):
        if tuple(r) in seen:
            continue
        seen.add(tuple(r))
        oid, lot = (r["IDCuaHang"], r["MaHoaDon"]), r["MaLo"]
        order = orders[oid]
        if lot not in remaining or day(order["NgayLap"]) > cutoff or "cancel" in plain(order["TinhTrang"]):
            continue
        remaining[lot] -= quantity(r["SLQuyDoi"])
        if r["SLTraQuyDoi"] > 0:
            dates = returned_at[oid]
            assert dates and not (min(dates) <= cutoff < max(dates)), "Mixed return dates require per-event lot allocations"
            if max(dates) <= cutoff:
                remaining[lot] += quantity(r["SLTraQuyDoi"])
    names = {r["MaHangHoa"]: r["TenGoi"] for r in db.execute("SELECT * FROM san_pham")}
    qualifying, earliest = [], {}
    for lot, balance in remaining.items():
        record = lots[lot]
        expires = day(record["HanSuDung"])
        if balance > 0 and cutoff <= expires <= last:
            code = record["MaHangHoa"]
            earliest[code] = min(earliest.get(code, expires), expires)
            qualifying.append({"lot": lot, "product": code, "expiry": expires.isoformat(), "remaining": str(balance)})
    ranking = sorted(earliest, key=lambda code: (earliest[code], code))
    db.close()
    return {
        "q_0ca19cd317cb": answer([names[c] for c in ranking], "list", codes=ranking,
            list_aliases=[[names[c], c] for c in ranking], cutoff=cutoff.isoformat(), end_inclusive=last.isoformat(),
            ordering="earliest qualifying expiry then code", qualifying_lots=qualifying),
        "q_113d86c3eb6a": answer(rounded((revenues[8] - revenues[7]) * 100 / revenues[7], 2), "percent", july=str(revenues[7]), august=str(revenues[8])),
        "q_169bfdcce0ae": repeat_percentage(counts, 6),
    }


def audit_dev011():
    raw = list(deduplicate(grouped(excel(DATA / "dev_011/SoBanHang.xlsx", "Sổ bán hàng"),
        "STT", "Hàng", ("Ngày", "Ghi chú"))))
    bad = {clean(r["STT"]) for _, _, r in raw if invalid_status(r.get("Ghi chú"))}
    aliases = {plain(r["Ghi tắt"]): r["Tên hàng"] for _, _, r in excel(DATA / "dev_011/SoBanHang.xlsx", "Ghi tắt")}
    tradeins, phones, labels = defaultdict(set), Decimal(0), defaultdict(Decimal)
    for _, _, r in raw:
        oid = clean(r["STT"])
        if oid in bad or not "2026-07-01" <= day(r["Ngày"]).isoformat() < "2026-08-01":
            continue
        if r.get("Thu máy cũ") is not None:
            tradeins[oid].add(number(r["Thu máy cũ"]))
        name = aliases.get(plain(r["Hàng"]), clean(r["Hàng"]))
        family = re.sub(r"\s+\d+\s*gb\b.*$", "", plain(name)).strip()
        if family == "xiaomi redmi note 14":
            phones += quantity(r["SL"])
            labels[name] += quantity(r["SL"])
    assert all(len(v) == 1 for v in tradeins.values())
    return {"q_1040545087b6": answer(rounded(sum((next(iter(v)) for v in tradeins.values()), Decimal(0))), orders=len(tradeins)),
        "q_12712b3cc328": answer(rounded(phones), product_quantities={k: str(v) for k, v in labels.items()})}


def due_dates(purchased, due_day, terms):
    result = []
    for offset in range(1, terms + 1):
        month_index = purchased.year * 12 + purchased.month - 1 + offset
        year, month = divmod(month_index, 12)
        month += 1
        result.append(date(year, month, min(due_day, calendar.monthrange(year, month)[1])))
    return result


def audit_dev014():
    raw = workbook_sales("dev_014", "SoBanHang.xlsx", skip=("Ghi tắt",))
    bad = {clean(r["STT"]) for _, _, r in raw if invalid_status(r.get("Ghi chú"))}
    cancelled = {clean(r["STT"]) for _, _, r in raw if any(t in plain(r.get("Ghi chú")) for t in ("cancel", "huy"))}
    rows = [r for r in raw if clean(r[2]["STT"]) not in bad]
    november = total(rows, "Ngày", "Thành tiền", "2025-11-01", "2025-11-09")
    october = total(rows, "Ngày", "Thành tiền", "2025-10-01", "2025-10-09")
    dates = {clean(r["STT"]): r["Ngày"] for _, _, r in raw}
    tradeins = []
    for _, line, r in excel(DATA / "dev_014/ThuMayCu.xlsx", "Sheet1"):
        oid = clean(r["Trừ vào (STT sổ bán)"])
        if oid not in bad and "2026-02-01" <= dates[oid] < "2026-03-01":
            tradeins.append({"row": line, "order": oid, "amount": str(number(r["Giá thu"]))})
    contracts, payments = {}, defaultdict(list)
    for _, _, r in excel(DATA / "dev_014/SoNo.xlsx", "Trả góp"):
        oid = clean(r["STT"])
        if clean(r.get("Ngày")):
            contracts[oid] = {"customer": customer_name(r["Khách"]), "purchased": day(r["Ngày"]),
                "due_day": int(r["Hạn góp (ngày)"]), "terms": int(r["Số kỳ"]),
                "monthly": number(r["Góp/tháng"]), "principal": number(r["Giá"]) - number(r["Trả trước"])}
        if clean(r.get("Ngày góp")):
            payments[oid].append((day(r["Ngày góp"]), number(r["Đã góp"])))
    due_count, due_evidence = 0, []
    overdue, evidence, residual_alternative = set(), [], set()
    for oid, c in contracts.items():
        if oid in cancelled:
            continue
        schedule = due_dates(c["purchased"], c["due_day"], c["terms"])
        if plain(c["customer"]) == "ta dung long" and c["purchased"] <= date(2026, 9, 19):
            count = sum(d <= date(2026, 9, 19) for d in schedule)
            due_count += count
            due_evidence.append({"order": oid, "due_dates_inclusive": [d.isoformat() for d in schedule if d <= date(2026, 9, 19)], "count": count})
        cutoff = date(2026, 3, 13)
        if c["purchased"] > cutoff:
            continue
        periods = sum(d < cutoff for d in schedule)
        paid = sum((amount for when, amount in payments[oid] if when <= cutoff), Decimal(0))
        due = c["monthly"] * periods
        if paid < due:
            overdue.add(c["customer"])
            evidence.append({"order": oid, "customer": c["customer"], "overdue_periods": periods,
                "scheduled_due": str(due), "paid_at_cutoff": str(paid)})
        alternative = c["principal"] if periods == c["terms"] else due
        if paid < alternative:
            residual_alternative.add(c["customer"])
    assert due_evidence, "Target customer not found"
    return {
        "q_0d23cc65cc7a": answer(rounded((november - october) * 100 / october, 2), "percent", november=str(november), october=str(october)),
        "q_1019063a351e": answer(rounded(sum((Decimal(r["amount"]) for r in tradeins), Decimal(0))), contributions=tradeins),
        "q_101a2b744e56": answer(due_count, contributions=due_evidence, includes_due_today=True),
        "q_16852a1ac5f7": answer(len(overdue), contributions=evidence,
            paid_allocated_to_earliest_due=True, overdue_strictly_before_cutoff=True,
            nominal_schedule_count=len(overdue), principal_residual_alternative=len(residual_alternative),
            schedule_policy="Use recorded Góp/tháng for each scheduled term; do not invent a different last-term amount."),
    }
