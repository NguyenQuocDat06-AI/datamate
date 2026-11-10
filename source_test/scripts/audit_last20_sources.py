"""Question-specific raw-source calculations; no solver/cache/LLM imports.

These functions are an audit of public questions 31--50, not inference recipes.
Frozen predictions are read only by the separate report script after calculation.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from openpyxl import load_workbook

from scripts.review_public_first10 import (
    clean, csv_rows, day, deduplicate, excel, grouped, invalid_status, number, plain,
    quantity,
)
from scripts.review_public_next20 import customer_name, worker_alias

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/public_test/data"


def rounded(value, places=0):
    result = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return int(result) if places == 0 else float(result)


def answer(value, kind="number", **evidence):
    return {"expected_type": kind, "expected": value, **evidence}


def sale_rows(bid, filename, sheet, header, oid, item, inherited):
    source = DATA / bid / filename
    iterator = csv_rows(source) if source.suffix == ".csv" else excel(source, sheet, header)
    return list(deduplicate(grouped(iterator, oid, item, inherited)))


def audit_dev001():
    rows = sale_rows("dev_001", "So_chi_tiet_ban_hang.xlsx", "Sheet1", 4,
                     "Mã đơn hàng", "Tên hàng hóa", ("Ngày", "Trạng thái", "Khách hàng"))
    bad = {clean(r["Mã đơn hàng"]) for _, _, r in rows if invalid_status(r["Trạng thái"])}
    selected = [r for _, _, r in rows if clean(r["Mã đơn hàng"]) not in bad
                and "2025-05-01" <= day(r["Ngày"]).isoformat() < "2025-06-01"]
    total = sum((number(r["Thành tiền"]) for r in selected), Decimal(0))
    count = len({clean(r["Mã đơn hàng"]) for r in selected})
    return {"q_09e3efc66c68": answer(rounded(total / count), revenue=str(total), orders=count)}


def audit_dev002():
    rows = sale_rows("dev_002", "BanHang_T2_2026.xlsx", "DonHang_ChiTiet", 4,
                     "Mã đơn hàng", "Tên sản phẩm", ("Ngày đặt hàng", "Trạng thái đơn hàng"))
    orders, cancelled = set(), set()
    statuses = Counter()
    for _, _, r in rows:
        if not "2026-02-01" <= day(r["Ngày đặt hàng"]).isoformat() < "2026-03-01":
            continue
        oid = clean(r["Mã đơn hàng"])
        orders.add(oid)
        text = plain(r["Trạng thái đơn hàng"])
        statuses[text] += 1
        if any(token in text for token in ("huy", "cancel", "khong giao duoc")):
            cancelled.add(oid)
    return {"q_0ac818a0f5f2": answer(rounded(Decimal(len(cancelled)) * 100 / len(orders), 2),
            "percent", cancelled_orders=len(cancelled), all_orders=len(orders), statuses=dict(statuses))}


def audit_dev003():
    rows = sale_rows("dev_003", "BanHang_TongHop_2026.xlsx", "BanHang", 1,
                     "Đơn số", "Hàng hóa", ("Giờ tạo đơn", "Trạng thái giao"))
    bad = {clean(r["Đơn số"]) for _, _, r in rows if invalid_status(r["Trạng thái giao"])}
    profit, labels = defaultdict(Decimal), defaultdict(Counter)
    week, combined = [], []
    for _, line, r in rows:
        if clean(r["Đơn số"]) in bad:
            continue
        when = day(r["Giờ tạo đơn"]).isoformat()
        if "2026-04-01" <= when < "2026-05-01":
            if "+" in clean(r["Hàng hóa"]):
                combined.append({"row": line, "name": r["Hàng hóa"], "code": r["Barcode"]})
            if plain(r["Hàng hóa"]) != "phi phuc vu":
                code = clean(r["Barcode"])
                profit[code] += number(r["Thành tiền (đ)"]) - quantity(r["S.L"]) * number(r["Giá vốn lúc bán"])
                labels[code][clean(r["Hàng hóa"])] += 1
        if "2026-03-23" <= when < "2026-03-30":
            week.append(r)
    assert not combined, f"Combined toppings require separate allocation: {combined[:3]}"
    ranking = sorted(profit, key=lambda code: (-profit[code], code))
    total = sum((number(r["Thành tiền (đ)"]) for r in week), Decimal(0))
    orders = len({clean(r["Đơn số"]) for r in week})
    return {
        "q_07ceefc30333": answer(labels[ranking[0]].most_common(1)[0][0], "string",
            winner_code=ranking[0], aliases=list(labels[ranking[0]]),
            ranking=[{"code": c, "profit": str(profit[c]), "names": dict(labels[c])} for c in ranking[:10]]),
        "q_09542f67d04a": answer(rounded(total / orders), revenue=str(total), orders=orders,
            window=["2026-03-23", "2026-03-30"]),
    }


def audit_dev004():
    path = DATA / "dev_004/SoBanHang.xlsx"
    book = load_workbook(path, read_only=True, data_only=True)
    sheets = book.sheetnames
    book.close()
    bad, selected = set(), []
    for sheet in sheets:
        rows = sale_rows("dev_004", path.name, sheet, 2, "Ma don", "Hàng bán",
                         ("Thời gian", "Trạng thái xử lý", "Khách hàng (tên)"))
        for _, line, r in rows:
            oid = clean(r["Ma don"])
            if invalid_status(r.get("Trạng thái xử lý")) or quantity(r["SL bán"]) < 0:
                bad.add(oid)
            if sheet in {"T05-2026", "T03-2026", "T06-2025"}:
                selected.append((sheet, line, r))
    result = {}
    for qid, target, start, end in [
        ("q_009187cd2960", "ong pvc binh minh d50 cay 4m", "2026-05-01", "2026-06-01"),
        ("q_02feb595d6b7", "ong pvc binh minh d32 cay 4m", "2026-03-01", "2026-04-01"),
    ]:
        contributions = []
        for sheet, line, r in selected:
            when = day(r["Thời gian"], int(sheet[-4:])).isoformat()
            if clean(r["Ma don"]) in bad or not start <= when < end or plain(r["Hàng bán"]) != target:
                continue
            unit = plain(r["dvt"])
            assert unit in {"bo", "cay"}, unit
            factor = 10 if unit == "bo" else 1  # Explicit dev_004 README convention.
            contributions.append({"sheet": sheet, "row": line, "order": r["Ma don"],
                "quantity": str(quantity(r["SL bán"])), "unit": r["dvt"], "factor": factor,
                "base_quantity": str(quantity(r["SL bán"]) * factor)})
        result[qid] = answer(rounded(sum((Decimal(r["base_quantity"]) for r in contributions), Decimal(0))),
            contributions=contributions, invalid_orders_scanned_all_sheets=len(bad))
    customers, variants = defaultdict(set), defaultdict(set)
    for sheet, _, r in selected:
        when = day(r["Thời gian"], int(sheet[-4:])).isoformat()
        if clean(r["Ma don"]) in bad or not "2025-06-01" <= when < "2025-07-01":
            continue
        name = customer_name(r["Khách hàng (tên)"])
        if plain(name) in {"", "le", "kl", "khach le", "khach vang lai", "vang lai", "khachle"}:
            continue
        customers[name].add(clean(r["Ma don"]))
        variants[name].add(clean(r["Khách hàng (tên)"]))
    loyal = sum(len(ids) >= 4 for ids in customers.values())
    result["q_03622bae312b"] = answer(rounded(Decimal(loyal) * 100 / len(customers), 2), "percent",
        qualifying_customers=loyal, buying_customers=len(customers),
        customers={name: {"orders": len(ids), "aliases": sorted(variants[name])} for name, ids in customers.items()})
    return result


def audit_dev005():
    bad = {clean(r["Mã đơn hàng"]) for _, _, r in excel(DATA / "dev_005/DonHang_Lo.xlsx", "Sheet1")
           if r.get("Số lượng trả") is not None and quantity(r["Số lượng trả"]) > 0}
    selected = []
    for month in (2, 3):
        rows = sale_rows("dev_005", f"BanHang_Thang{month:02d}_2025.xlsx", "DonHang_ChiTiet", 4,
                         "Mã đơn hàng", "Tên sản phẩm", ("Ngày đặt hàng", "Trạng thái đơn hàng"))
        for _, _, r in rows:
            if invalid_status(r["Trạng thái đơn hàng"]):
                bad.add(clean(r["Mã đơn hàng"]))
            selected.append(r)
    totals = defaultdict(Decimal)
    for r in selected:
        d = day(r["Ngày đặt hàng"])
        if clean(r["Mã đơn hàng"]) not in bad and d.day <= 18:
            totals[d.month] += number(r["Tổng tiền"])
    return {"q_01d1099489a0": answer(rounded((totals[3] - totals[2]) * 100 / totals[2], 2),
        "percent", march_1_to_18=str(totals[3]), february_1_to_18=str(totals[2]))}


def audit_dev006():
    db = sqlite3.connect((DATA / "dev_006/pos.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    customers = [dict(r) for r in db.execute("SELECT * FROM khach_hang") if plain(r["TenGoi"]) == "le son trung"]
    assert len(customers) == 1
    target = customers[0]["MaKhachHang"]
    orders = {(r["IDCuaHang"], r["MaHoaDon"]): dict(r) for r in db.execute("SELECT * FROM don_hang")}
    payments = defaultdict(list)
    for r in db.execute("SELECT * FROM thanh_toan"):
        payments[(r["IDCuaHang"], r["MaHoaDon"])].append(dict(r))
    new_debt, repayments = Decimal(0), Decimal(0)
    debt_evidence = []
    for oid, r in orders.items():
        if r["MaKhachHang"] != target or plain(r["PhuongThucTT"]) != "debt":
            continue
        if any(token in plain(r["TinhTrang"]) for token in ("cancel", "huy")):
            continue
        bought = day(r["NgayLap"])
        upfront = sum((number(p["SoTienThu"]) for p in payments[oid] if day(p["NgayGioThu"]) == bought), Decimal(0))
        incurred = max(number(r["TongThanhToan"]) - upfront, Decimal(0)) if date(2026, 7, 1) <= bought < date(2026, 8, 1) else Decimal(0)
        repaid = sum((number(p["SoTienThu"]) for p in payments[oid]
            if date(2026, 7, 1) <= day(p["NgayGioThu"]) < date(2026, 8, 1)
            and day(p["NgayGioThu"]) > bought), Decimal(0))
        new_debt += incurred
        repayments += repaid
        if incurred or repaid:
            debt_evidence.append({"order": oid[1], "bought": bought.isoformat(),
                "status": r["TinhTrang"], "new_debt": str(incurred), "repaid": str(repaid)})
    returned = {(r["IDCuaHang"], r["MaHoaDon"]) for r in db.execute("SELECT * FROM tra_hang")}
    returned |= {(r["IDCuaHang"], r["MaHoaDon"]) for r in db.execute("SELECT * FROM chi_tiet_don_lo") if r["SLTraQuyDoi"] > 0}
    with (ROOT / "data/public_test/calendar.csv").open(encoding="utf-8-sig", newline="") as stream:
        calendar = list(csv.DictReader(stream))
    start = next(r["date"] for r in calendar if (r["lunar_year"], r["lunar_month"], r["lunar_day"]) == ("2025", "12", "23"))
    end = next(r["date"] for r in calendar if (r["lunar_year"], r["lunar_month"], r["lunar_day"]) == ("2026", "1", "10"))
    eligible = {oid for oid, r in orders.items() if not invalid_status(r["TinhTrang"])
                and oid not in returned and start <= day(r["NgayLap"]).isoformat() <= end}
    quantities, seen = defaultdict(Decimal), set()
    for r in db.execute("SELECT * FROM chi_tiet_don"):
        if tuple(r) in seen:
            continue
        seen.add(tuple(r))
        if (r["IDCuaHang"], r["MaHoaDon"]) in eligible:
            quantities[r["MaHangHoa"]] += quantity(r["SLQuyDoi"])
    products = {r["MaHangHoa"]: r["TenGoi"] for r in db.execute("SELECT * FROM san_pham")}
    ranking = sorted(quantities, key=lambda c: (-quantities[c], c))
    db.close()
    return {
        "q_0a9819711554": answer([rounded(new_debt), rounded(repayments)], "list",
            customer_code=target, customer_name=customers[0]["TenGoi"], contributions=debt_evidence),
        "q_0b78b53675c1": answer(products[ranking[0]], "string", winner_code=ranking[0],
            window_inclusive=[start, end], ranking=[{"code": c, "name": products[c], "base_quantity": str(quantities[c])} for c in ranking[:10]]),
    }


def audit_dev007():
    rows = sale_rows("dev_007", "chi_tiet_hoa_don.csv", None, 1,
                     "Mã bill", "Mặt hàng", ("Ngày giao dịch", "Tiến độ"))
    bad = {clean(r["Mã bill"]) for _, _, r in rows if invalid_status(r["Tiến độ"])}
    totals = defaultdict(Decimal)
    for _, _, r in rows:
        d = day(r["Ngày giao dịch"])
        if clean(r["Mã bill"]) not in bad and d.year == 2026 and d.month in (3, 4):
            totals[d.month] += number(r["Thành tiền"])
    return {"q_0acdf488056e": answer(rounded((totals[4] - totals[3]) * 100 / totals[3], 2), "percent",
            march=str(totals[3]), april=str(totals[4]))}


def audit_dev009():
    rows = list(deduplicate(csv_rows(DATA / "dev_009/danh_sach_hoa_don.csv")))
    selected = [r for _, _, r in rows if clean(r["Mã hóa đơn"]) and not invalid_status(r["Trạng thái đơn"])
                and day(r["Ngày tháng"]).isoformat() == "2025-02-25"]
    return {"q_0b0081426a17": answer(rounded(sum((number(r["Giá trị hàng"]) for r in selected), Decimal(0))),
            orders=len(selected), source_measure="Giá trị hàng, không phải TongTien/Đã thu")}


def audit_dev010():
    customers = {clean(r["Mã khách"]) for _, _, r in csv_rows(DATA / "dev_010/khach_hang.csv") if plain(r["Loại khách"]) == "si"}
    orders = {clean(r["Số CT"]): r for _, _, r in csv_rows(DATA / "dev_010/don_hang.csv")
        if clean(r["Mã thẻ"]) in customers and not invalid_status(r["Xử lý"])
        and "2026-04-01" <= day(r["Date"]).isoformat() < "2026-05-01"}
    rows = sale_rows("dev_010", "chi_tiet_don.csv", None, 1, "Mã bill", "Mã H.hóa", ())
    selected = [r for _, _, r in rows if clean(r["Mã bill"]) in orders]
    return {"q_0ae9b7cf7003": answer(rounded(sum((number(r["Cộng"]) for r in selected), Decimal(0))),
            wholesale_customers=len(customers), orders=len(orders), lines=len(selected))}


def audit_dev011():
    rows = sale_rows("dev_011", "SoBanHang.xlsx", "Sổ bán hàng", 1,
                     "STT", "Hàng", ("Ngày", "Ghi chú"))
    bad = {clean(r["STT"]) for _, _, r in rows if invalid_status(r.get("Ghi chú"))}
    selected = {clean(r["STT"]) for _, _, r in rows if clean(r["STT"]) not in bad
                and day(r["Ngày"]).isoformat() == "2026-06-09"}
    return {"q_0a3308eb76bb": answer(len(selected), order_ids=sorted(selected))}


def audit_dev012():
    roster = [r for _, _, r in excel(DATA / "dev_012/DanhSachTho.xlsx", "Sheet1")]
    worker = next(r for r in roster if plain(r["Họ tên"]) == "doan bao phuc")
    assert sum(plain(r["Họ tên"]).split()[-1] == "phuc" for r in roster) == 1
    contributions, variants = [], defaultdict(Decimal)
    for _, line, r in excel(DATA / "dev_012/SoSuaXe.xlsx", "T8-2025", 2):
        if not clean(r.get("Xe")) or r.get("Tiền") is None:
            continue
        if not clean(r.get("Ngày")) or worker_alias(r.get("Thợ")) not in {"phuc", "doan bao phuc"}:
            continue
        if "2025-08-01" <= day(r["Ngày"], 2025).isoformat() < "2025-09-01":
            amount = number(r["Tiền"])
            variants[clean(r["Thợ"])] += amount
            contributions.append({"row": line, "amount": str(amount), "worker": r["Thợ"]})
    total = sum((Decimal(r["amount"]) for r in contributions), Decimal(0))
    paid, warranty = [], 0
    for _, line, r in excel(DATA / "dev_012/SoSuaXe.xlsx", "T2-2026", 2):
        if not clean(r.get("Ngày")) or not clean(r.get("Xe")) or r.get("Tiền") is None:
            continue
        if not "2026-02-01" <= day(r["Ngày"], 2026).isoformat() < "2026-03-01":
            continue
        amount = number(r["Tiền"])
        if amount > 0:
            paid.append(amount)
        else:
            assert "bao hanh" in plain(r.get("Ghi chú")), (line, r)
            warranty += 1
    plates, labels = set(), Counter()
    for _, line, r in excel(DATA / "dev_012/SoSuaXe.xlsx", "T8-2026", 2):
        if not clean(r.get("Ngày")) or not clean(r.get("Xe")) or r.get("Tiền") is None:
            continue
        if not "2026-08-01" <= day(r["Ngày"], 2026).isoformat() < "2026-09-01":
            continue
        label = clean(r["Xe"])
        match = re.search(r"(\d{2}[A-Za-z0-9]{1,3}[-\s]\d{3}[.\s]?\d{2})$", label)
        assert match, (line, label)
        model = re.sub(r"[^a-z0-9]", "", plain(label[:match.start()]))
        if model.startswith("shmode"):
            plates.add(re.sub(r"[^A-Z0-9]", "", match[1].upper()))
            labels[label[:match.start()].strip()] += 1
    return {
        "q_027f5441831d": answer(rounded(total * number(worker["Hoa hồng (%)"]) / 100),
            revenue=str(total), commission_percent=worker["Hoa hồng (%)"], alias_amounts=dict(variants), contributions=contributions),
        "q_05cde771f4c3": answer(rounded(sum(paid, Decimal(0)) / len(paid)),
            revenue=str(sum(paid, Decimal(0))), paid_tickets=len(paid), warranty_tickets=warranty),
        "q_07e2508ad671": answer(len(plates), plates=sorted(plates), model_labels=dict(labels)),
    }


def audit_dev013():
    rows = sale_rows("dev_013", "BanHang_2025.xlsx", "T08-2025", 2,
                     "HĐ", "Tên hàng", ("Ngày", "Ghi chú"))
    bad = {clean(r["HĐ"]) for _, _, r in rows if invalid_status(r.get("Ghi chú"))}
    selected = [r for _, _, r in rows if clean(r["HĐ"]) not in bad and r.get("SL") is not None
                and "2025-08-01" <= day(r["Ngày"], 2025).isoformat() < "2025-09-01"]
    return {"q_099a18d1ed60": answer(rounded(sum((number(r["TT"]) for r in selected), Decimal(0))),
            merchandise_lines=len(selected), orders=len({clean(r["HĐ"]) for r in selected}),
            excluded_order_adjustments="Voucher, Dùng điểm và Phí ship không phải tiền hàng")}


def audit_dev014():
    book = load_workbook(DATA / "dev_014/SoBanHang.xlsx", read_only=True, data_only=True)
    sheets = [s for s in book.sheetnames if s != "Ghi tắt"]
    book.close()
    sales = {}
    bad_sale, cancelled = set(), set()
    for sheet in sheets:
        rows = sale_rows("dev_014", "SoBanHang.xlsx", sheet, 2, "STT", "Hàng", ("Ngày", "Ghi chú"))
        for _, _, r in rows:
            oid = clean(r["STT"])
            when = day(r["Ngày"], int(sheet[-4:])).isoformat()
            if oid in sales:
                assert sales[oid] == when, (oid, sales[oid], when)
            sales[oid] = when
            if invalid_status(r.get("Ghi chú")):
                bad_sale.add(oid)
            if any(t in plain(r.get("Ghi chú")) for t in ("huy", "cancel")):
                cancelled.add(oid)
    contracts, active = {}, None
    for _, line, r in excel(DATA / "dev_014/SoNo.xlsx", "Trả góp"):
        oid = clean(r.get("STT"))
        if clean(r.get("Ngày")):
            assert oid not in contracts
            active = oid
            contracts[oid] = {"row": line, "date": day(r["Ngày"]).isoformat(),
                "principal": number(r["Giá"]) - number(r["Trả trước"]), "payments": []}
        if clean(r.get("Ngày góp")):
            assert oid == active and oid in contracts
            contracts[oid]["payments"].append({"row": line, "date": day(r["Ngày góp"]).isoformat(),
                "amount": number(r["Đã góp"])})
    debts = []
    for oid, c in contracts.items():
        assert oid in sales and sales[oid] == c["date"]
        if c["date"] > "2025-10-18" or oid in cancelled:
            continue
        paid = sum((p["amount"] for p in c["payments"] if p["date"] <= "2025-10-18"), Decimal(0))
        debts.append({"order": oid, "date": c["date"], "principal": str(c["principal"]),
            "paid_at_cutoff": str(paid), "remaining": str(max(c["principal"] - paid, Decimal(0)))})
    tradeins = []
    for _, line, r in excel(DATA / "dev_014/ThuMayCu.xlsx", "Sheet1"):
        oid = clean(r["Trừ vào (STT sổ bán)"])
        assert oid in sales
        if oid not in bad_sale and "2025-09-01" <= sales[oid] < "2025-10-01":
            tradeins.append({"row": line, "order": oid, "order_date": sales[oid],
                             "trade_in_date": day(r["Ngày"]).isoformat(), "amount": str(number(r["Giá thu"]))})
    return {
        "q_0594583b3d46": answer(rounded(sum((Decimal(c["remaining"]) for c in debts), Decimal(0))),
            cutoff_inclusive="2025-10-18", contracts=debts, total_contracts_in_ledger=len(contracts)),
        "q_0b854610073b": answer(rounded(sum((Decimal(r["amount"]) for r in tradeins), Decimal(0))),
            contributions=tradeins),
    }
