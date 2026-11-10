"""Raw calculations for sales questions; never import solver caches or predictions."""

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

from scripts.next50_common import (DATA, answer, rounded, clean, csv_rows, day, deduplicate,
    excel, grouped, invalid_status, number, plain, quantity, sales, total, order_ids,
    customer_counts, repeat_percentage)


def audit_dev001():
    rows = sales("dev_001", ["So_chi_tiet_ban_hang.xlsx"], "Sheet1", 4,
        "Mã đơn hàng", "Tên hàng hóa", ("Ngày", "Trạng thái", "Khách hàng"))
    november = customer_counts(rows, "Ngày", "Mã đơn hàng", "Khách hàng", "2025-11-01", "2025-12-01")
    july = customer_counts(rows, "Ngày", "Mã đơn hàng", "Khách hàng", "2025-07-01", "2025-08-01")
    return {
        "q_0ed1ebc4fe4d": answer(sum(len(ids) >= 4 for ids in november.values()), buyers=len(november)),
        "q_0f59f61affd1": answer(len(order_ids(rows, "Ngày", "Mã đơn hàng", "2025-01-01", "2025-03-13"))),
        "q_11c915f78c7e": answer(len(order_ids(rows, "Ngày", "Mã đơn hàng", "2026-06-29", "2026-07-03")), window=["2026-06-29", "2026-07-03"]),
        "q_1618b7c50ae7": repeat_percentage(july, 3),
    }


def audit_dev002():
    raw = list(deduplicate(grouped(excel(DATA / "dev_002/BanHang_T6_2025.xlsx", "DonHang_ChiTiet", 4),
        "Mã đơn hàng", "Tên sản phẩm", ("Ngày đặt hàng", "Trạng thái đơn hàng"))))
    cancelled = {clean(r["Mã đơn hàng"]) for _, _, r in raw if in_june(r) and
        any(t in plain(r["Trạng thái đơn hàng"]) for t in ("huy", "cancel", "khong giao duoc"))}
    march = sales("dev_002", ["BanHang_T3_2025.xlsx"], "DonHang_ChiTiet", 4,
        "Mã đơn hàng", "Tên sản phẩm", ("Ngày đặt hàng", "Trạng thái đơn hàng"))
    selected = [r for _, _, r in march if plain(r["Tên sản phẩm"]) == "nuoc ep cam (m)"
        and "2025-03-01" <= day(r["Ngày đặt hàng"]).isoformat() < "2025-04-01"]
    assert all(plain(r["Đơn vị tính"]) == "ly" for r in selected)
    return {"q_0c62d2078959": answer(len(cancelled)),
        "q_0ef28ee17f12": answer(rounded(sum((quantity(r["Số lượng sản phẩm"]) for r in selected), Decimal(0))), lines=len(selected))}


def in_june(r):
    return "2025-06-01" <= day(r["Ngày đặt hàng"]).isoformat() < "2025-07-01"


def audit_dev003():
    rows = sales("dev_003", ["BanHang_TongHop_2025.xlsx", "BanHang_TongHop_2026.xlsx"], "BanHang", 1,
        "Đơn số", "Hàng hóa", ("Giờ tạo đơn", "Trạng thái giao"))
    customers = {clean(r["Mã KH mua"]): r for _, _, r in excel(DATA / "dev_003/BanHang_TongHop_2025.xlsx", "BanHang_KhachHang")}
    products = {clean(r["Mã H.hóa"]): r for _, _, r in excel(DATA / "dev_003/BanHang_TongHop_2025.xlsx", "BanHang_HangHoa")}
    buyer_orders = defaultdict(set)
    product_orders = defaultdict(set)
    june_profit, m_cups = Decimal(0), Decimal(0)
    loyal_dec, morning = [], set()
    for _, _, r in rows:
        d = day(r["Giờ tạo đơn"]).isoformat()
        oid, customer, product = clean(r["Đơn số"]), clean(r.get("Mã khách")), clean(r["Barcode"])
        if "2025-06-01" <= d < "2025-07-01" and customer:
            buyer_orders[customer].add(oid)
        if "2025-05-01" <= d < "2025-06-01" and plain(r["Hàng hóa"]) != "phi phuc vu":
            product_orders[product].add(oid)
        if "2026-06-01" <= d < "2026-07-01":
            june_profit += number(r["Thành tiền (đ)"]) - quantity(r["S.L"]) * number(r["Giá vốn lúc bán"])
        if "2025-12-01" <= d < "2026-01-01" and customer in customers and plain(customers[customer]["Kiểu khách"]) == "than thiet":
            loyal_dec.append(r)
        if "2026-03-01" <= d < "2026-04-01" and product in products:
            p = products[product]
            if re.search(r"\(\s*m\s*\)\s*$", str(p["Tên hàng hóa"]), re.I) and plain(p["DVT"]) == "ly":
                m_cups += quantity(r["SL (ĐV cơ bản)"])
        if "2026-07-01" <= d < "2026-08-01" and 6 <= int(str(r["Giờ tạo đơn"])[11:13]) < 14:
            morning.add(oid)
    ranked = sorted(buyer_orders, key=lambda code: (-len(buyer_orders[code]), code))
    winner = ranked[0]
    months = [total(rows, "Giờ tạo đơn", "Thành tiền (đ)", start, end) for start, end in
        [("2025-05-01", "2025-06-01"), ("2025-06-01", "2025-07-01"), ("2025-07-01", "2025-08-01")]]
    loyal_revenue = sum((number(r["Thành tiền (đ)"]) for r in loyal_dec), Decimal(0))
    loyal_orders = len({clean(r["Đơn số"]) for r in loyal_dec})
    return {
        "q_0cb6a1a6f1ef": answer(customers[winner]["Khách hàng mua"], "string", winner_code=winner,
            aliases=list({r["Khách hàng (tên)"] for _, _, r in rows if clean(r.get("Mã khách")) == winner}),
            orders=len(buyer_orders[winner]), ranking=[{"code": c, "orders": len(buyer_orders[c])} for c in ranked[:5]]),
        "q_101e84f9b702": answer(rounded(june_profit), year=2026),
        "q_10a0c4fec3d2": answer(months[0] < months[1] < months[2], "boolean", monthly_revenue=[str(v) for v in months]),
        "q_12d78074f059": answer(rounded(loyal_revenue / loyal_orders), revenue=str(loyal_revenue), orders=loyal_orders),
        "q_1341cdede99a": answer(sum(len(ids) >= 122 for ids in product_orders.values()), threshold_inclusive=122),
        "q_1602cfcc7d42": answer(rounded(m_cups)),
        "q_16fc554b8db0": answer(len(morning)),
    }


def audit_dev005():
    products = {plain(r["Tên sản phẩm"]): r for _, _, r in excel(DATA / "dev_005/DanhSachSanPham.xlsx", "Sheet1")}
    rx = {clean(r["Mã SKU"]) for _, _, r in excel(DATA / "dev_005/DanhSachThuoc.xlsx", "Sheet1")
          if r["Thuốc kê đơn"] is True or plain(r["Thuốc kê đơn"]) in {"true", "1"}}
    rx_names = {name for name, r in products.items() if clean(r["Mã SKU"]) in rx}
    returned = {clean(r["Mã đơn hàng"]) for _, _, r in excel(DATA / "dev_005/DonHang_Lo.xlsx", "Sheet1")
                if r.get("Số lượng trả") is not None and quantity(r["Số lượng trả"]) > 0}
    files = sorted(p.name for p in (DATA / "dev_005").glob("BanHang_Thang*.xlsx"))
    rows = sales("dev_005", files, "DonHang_ChiTiet", 4, "Mã đơn hàng", "Tên sản phẩm",
        ("Ngày đặt hàng", "Trạng thái đơn hàng", "Tên khách hàng"), extra_bad=returned)
    def revenue(start, end, prescription=None):
        return sum((number(r["Tổng tiền"]) for _, _, r in rows
            if start <= day(r["Ngày đặt hàng"]).isoformat() < end and
            (prescription is None or (plain(r["Tên sản phẩm"]) in rx_names) == prescription)), Decimal(0))
    rx_may = revenue("2025-05-01", "2025-06-01", True)
    all_may = revenue("2025-05-01", "2025-06-01")
    week = [r for _, _, r in rows if "2025-07-28" <= day(r["Ngày đặt hàng"]).isoformat() < "2025-08-04"]
    week_total = sum((number(r["Tổng tiền"]) for r in week), Decimal(0))
    counts = customer_counts(rows, "Ngày đặt hàng", "Mã đơn hàng", "Tên khách hàng", "2025-06-01", "2025-07-01")
    efticol = [name for name in products if name.startswith("efticol")]
    assert len(efticol) == 1, efticol
    product_name = efticol[0]
    conversion = {}
    for _, _, r in excel(DATA / "dev_005/DanhSachQuyDoi.xlsx", "Sheet1"):
        if plain(r["Tên sản phẩm"]) != product_name:
            continue
        text = str(r["Tỉ lệ quy đổi"])
        amount = quantity(text)
        unit = re.sub(r"^\s*[\d.,]+\s*", "", text)
        conversion[plain(r["Đơn vị quy đổi"])] = (amount, plain(unit or r["Đơn vị cơ bản"]))
    base = plain(products[product_name]["Đơn vị"])
    def factor(unit, seen=()):
        unit = plain(unit)
        if unit == base:
            return Decimal(1)
        assert unit not in seen and unit in conversion
        amount, child = conversion[unit]
        return amount * factor(child, (*seen, unit))
    quantities = [Decimal(0), Decimal(0)]
    for _, _, r in rows:
        if plain(r["Tên sản phẩm"]) != product_name:
            continue
        d = day(r["Ngày đặt hàng"])
        if date(2025, 12, 27) <= d < date(2026, 1, 26):
            quantities[0] += quantity(r["Số lượng sản phẩm"]) * factor(r["Đơn vị tính"])
        elif date(2026, 1, 26) <= d < date(2026, 2, 25):
            quantities[1] += quantity(r["Số lượng sản phẩm"]) * factor(r["Đơn vị tính"])
    april = revenue("2025-04-01", "2025-04-18")
    march = revenue("2025-03-01", "2025-03-18")
    return {
        "q_0da15db21978": answer(rounded(rx_may * 100 / all_may, 2), "percent", rx_revenue=str(rx_may), all_revenue=str(all_may)),
        "q_0dff1966b7c3": answer(rounded(week_total / len({clean(r["Mã đơn hàng"]) for r in week})), window=["2025-07-28", "2025-08-04"]),
        "q_0ea6fdb19b86": answer(rounded((quantities[1] - quantities[0]) * 100 / quantities[0], 2), "percent", before=str(quantities[0]), after=str(quantities[1]), base_unit=base),
        "q_105bc863feea": answer(rounded(revenue("2026-04-01", "2026-05-01", False))),
        "q_107e86e608dc": answer(rounded(revenue("2025-06-01", "2025-07-01", False))),
        "q_1473ee8c46fd": answer(rounded((april - march) * 100 / march, 2), "percent", april=str(april), march=str(march)),
        "q_15dd37187b0d": repeat_percentage(counts, 4),
    }


def audit_dev007():
    rows = sales("dev_007", ["chi_tiet_hoa_don.csv"], None, 1, "Mã bill", "Mặt hàng", ("Ngày giao dịch", "Tiến độ"))
    return {"q_0f7d15097a8c": answer(len(order_ids(rows, "Ngày giao dịch", "Mã bill", "2025-07-01", "2025-10-01"))),
        "q_14565ad8b459": answer(rounded(total(rows, "Ngày giao dịch", "Thành tiền", "2025-08-09", "2025-08-10")))}


def audit_dev008():
    rows = sales("dev_008", ["ChiTietHoaDon_1.xlsx", "ChiTietHoaDon_2.xlsx"], "Sheet1", 1,
        "Bill", "Tên hàng", ("Ngày xuất", "TT đơn"))
    products = {clean(r["Mã sản phẩm bán"]): r for _, _, r in excel(DATA / "dev_008/DanhMucHang.xlsx", "Sheet1")}
    week = [r for _, _, r in rows if "2025-06-16" <= day(r["Ngày xuất"]).isoformat() < "2025-06-23"]
    week_total = sum((number(r["Thành tiền"]) for r in week), Decimal(0))
    wallet = sum((number(r["Thành tiền"]) for _, _, r in rows if plain(r["Thanh toán bằng"]) == "ewallet"
        and "2026-07-01" <= day(r["Ngày xuất"]).isoformat() < "2026-07-23"), Decimal(0))
    may_orders, april_days = defaultdict(Decimal), defaultdict(Decimal)
    dishes = Decimal(0)
    for _, _, r in rows:
        d = day(r["Ngày xuất"]).isoformat()
        if "2026-05-01" <= d < "2026-06-01":
            may_orders[clean(r["Bill"])] += number(r["Thành tiền"])
        if "2026-04-01" <= d < "2026-05-01":
            april_days[d] += number(r["Thành tiền"])
        if "2025-06-09" <= d < "2025-06-16" and clean(r["Ma hang"]) in products:
            if plain(products[clean(r["Ma hang"])] ["DVT"]) == "dia":
                dishes += quantity(r["SL lẻ"])
    purchases = list(deduplicate(grouped(excel(DATA / "dev_008/ChiTietNhapHang.xlsx", "Sheet1"),
        "Mã phiếu nhập", "Tên mặt hàng", ("Trạng thái phiếu", "Ngày nhập"))))
    imports = {}
    for start, end in [("2026-03-01", "2026-04-01"), ("2025-06-01", "2025-07-01")]:
        selected = [r for _, _, r in purchases if not invalid_status(r["Trạng thái phiếu"])
                    and start <= day(r["Ngày nhập"]).isoformat() < end]
        imports[start] = sum((number(r["Thành tiền"]) for r in selected), Decimal(0))
    return {
        "q_0bee4ad8f965": answer(rounded(week_total / len({clean(r["Bill"]) for r in week}))),
        "q_0c0a1a780164": answer(rounded(wallet)),
        "q_0e72ff8aa98e": answer(sum(v >= 1_100_000 for v in may_orders.values()), threshold_inclusive=1100000),
        "q_0ef1608808dd": answer(rounded(imports["2026-03-01"])),
        "q_121c6959016d": answer(sum(v >= 40_000_000 for v in april_days.values()), daily_revenue={d: str(v) for d, v in april_days.items()}),
        "q_129b29f14e89": answer(rounded(imports["2025-06-01"])),
        "q_15711c61bb0d": answer(None, "unanswerable", reason="Không có dữ liệu đánh giá độ thân thiện của nhân viên; doanh thu không đo độ thân thiện.", source_files=[p.name for p in (DATA / "dev_008").iterdir()]),
        "q_15a8893ff6f9": answer(rounded(dishes)),
        "q_16ec75d6c8a4": answer(len(order_ids(rows, "Ngày xuất", "Bill", "2026-01-01", "2026-06-02"))),
    }


def audit_dev009():
    rows = [r for _, _, r in deduplicate(csv_rows(DATA / "dev_009/danh_sach_hoa_don.csv")) if clean(r.get("Mã hóa đơn")) and not invalid_status(r["Trạng thái đơn"])]
    return {"q_0bac8c88238d": answer(rounded(sum((number(r["Giá trị hàng"]) for r in rows if day(r["Ngày tháng"]).isoformat() == "2026-04-19"), Decimal(0)))),
        "q_128fd07d838e": answer(len({clean(r["Mã hóa đơn"]) for r in rows if day(r["Ngày tháng"]).isoformat() == "2025-08-17"}))}


def audit_dev010():
    orders = {clean(r["Số CT"]): r for _, _, r in csv_rows(DATA / "dev_010/don_hang.csv")}
    lines = list(deduplicate(grouped(csv_rows(DATA / "dev_010/chi_tiet_don.csv"), "Mã bill", "Mã H.hóa", ())))
    totals = defaultdict(Decimal)
    for _, _, r in lines:
        oid = clean(r["Mã bill"])
        order = orders[oid]
        d = day(order["Date"])
        if not invalid_status(order["Xử lý"]) and d.year == 2026 and d.month in (1, 2):
            totals[d.month] += number(r["Cộng"])
    repaid = Decimal(0)
    for _, _, r in csv_rows(DATA / "dev_010/thanh_toan.csv"):
        o = orders[clean(r["Mã đơn"])]
        if plain(o["Phương thức TT"]) != "debt" or any(t in plain(o["Xử lý"]) for t in ("huy", "cancel")):
            continue
        d = day(r["Ngay thu"])
        if date(2025, 12, 1) <= d < date(2026, 1, 1) and d > day(o["Date"]):
            repaid += number(r["So tien"])
    return {"q_0e1eff36bbc5": answer(rounded((totals[2] - totals[1]) * 100 / totals[1], 2), "percent",
        january=str(totals[1]), february=str(totals[2])), "q_16e72d67a5f4": answer(rounded(repaid))}


def audit_dev012():
    totals = []
    for sheet in ("T7-2025", "T8-2025"):
        amounts = [number(r["Tiền"]) for _, _, r in excel(DATA / "dev_012/SoSuaXe.xlsx", sheet, 2)
                   if clean(r.get("Xe")) and r.get("Tiền") is not None]
        totals.append(sum(amounts, Decimal(0)))
    return {"q_1205eaa6091a": answer(rounded((totals[1] - totals[0]) * 100 / totals[0], 2), "percent",
        july=str(totals[0]), august=str(totals[1]))}


def audit_dev013():
    rows = []
    for month in range(1, 5):
        current = sales("dev_013", ["BanHang_2026.xlsx"], f"T{month:02d}-2026", 2, "HĐ", "Tên hàng", ("Ngày", "Ghi chú"))
        for _, _, r in current:
            r["Ngày"] = day(r["Ngày"], 2026).isoformat()
        rows += current
    merchandise = [(s, n, r) for s, n, r in rows if r.get("SL") is not None]
    catalog = {}
    for _, _, r in excel(DATA / "dev_013/QuanLy.xlsx", "DanhMuc"):
        for label in [r["Tên hàng"], *str(r.get("Tên gọi khác") or "").split(",")]:
            if plain(label):
                assert plain(label) not in catalog or catalog[plain(label)]["Tên hàng"] == r["Tên hàng"]
                catalog[plain(label)] = r
    brands = defaultdict(Decimal)
    qualifying = []
    for _, _, r in merchandise:
        if not "2026-03-01" <= day(r["Ngày"], 2026).isoformat() < "2026-04-01":
            continue
        name = plain(r["Tên hàng"])
        assert name in catalog, name
        c = catalog[name]
        if plain(c["Nhóm"]) != "sua bot" or plain(c["Số / Size"]) != "so 2" or plain(c["ĐVT"]) != "lon":
            continue
        factor = Decimal(1)
        if plain(r["ĐVT"]) != "lon":
            m = re.fullmatch(r"1\s+(\S+)\s*=\s*(\d+)\s+(\S+)", plain(c["Quy đổi"]))
            assert m and plain(r["ĐVT"]) == m[1] and m[3] == "lon", (r, c)
            factor = Decimal(m[2])
        value = quantity(r["SL"]) * factor
        brands[clean(c["Hãng"])] += value
        qualifying.append({"product": c["Tên hàng"], "brand": c["Hãng"], "base_quantity": str(value)})
    winner = sorted(brands, key=lambda name: (-brands[name], name))[0]
    return {
        "q_102720daf6a9": answer(rounded(total(merchandise, "Ngày", "TT", "2026-01-01", "2026-02-16"))),
        "q_162c076a350d": answer(rounded(total(merchandise, "Ngày", "TT", "2026-01-01", "2026-04-11"))),
        "q_14a10f251fd0": answer(winner, "string", quantities={k: str(v) for k, v in brands.items()}, qualifying_lines=len(qualifying)),
    }
