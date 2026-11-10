"""Conservative label mapping. Labels suggest roles; README supplies their meaning."""

from __future__ import annotations

import re

from bizreason.utils import normalize_lookup

ALIASES = {
    "order_id": "mã đơn hàng|mã đơn|madon|madonhang|order_id|order id|số đơn|mahd",
    "ordered_at": "ngày đặt hàng|ngày đặt|ngày bán|ngaydat|ngayban|ngày giờ|ngaygio|order_date|ordered_at",
    "status": "trạng thái đơn hàng|trạng thái đơn|trạng thái|trangthai|trangthaidon|status",
    "customer_id": "mã khách hàng|mã khách|makh|makhach|makhachhang|customer_id",
    "customer_name": "tên khách hàng|tên khách|khách hàng|tenkh|tenkhach|tenkhachhang|customer_name",
    "product_id": "mã sản phẩm|mã hàng|mã sku|masp|mahang|masku|sku|product_id",
    "product_name": "tên sản phẩm|tên hàng|tên thuốc|tensp|tenhang|tenthuoc|product_name",
    "qty_sale": "số lượng sản phẩm|số lượng|soluong|sl|qty_sale|quantity",
    "qty_base": "số lượng cơ bản|số lượng quy đổi|slquydoi|soluongquydoi|qty_base",
    "unit": "đơn vị tính|đơn vị|dvt|donvi|donvitinh|unit",
    "line_amount": "thành tiền|thanhtien|trigia|trị giá|line_amount|line_net_vnd",
    "unit_price": "giá sản phẩm|đơn giá|giá bán|dongia|giaban|giabanle|unit_price",
    "cost_sale": "giá vốn lúc bán|giá vốn|giavonlucban|giavon|cost_per_sale_unit_vnd|cost_sale",
    "total_due": "tổng thanh toán|tongthanhtoan|tổng phải trả|total_due",
    "paid_amount": "số tiền thu|sotienthu|số tiền trả|số tiền thanh toán|paid_amount|amount_paid",
    "paid_at": "ngày thu|ngày trả|ngày thanh toán|ngaythu|ngaytra|ngaythanhtoan|paid_at",
    "payment_method": "hình thức thanh toán|phương thức thanh toán|hinhthucthanhtoan|phuongthuctt|payment_method",
    "conversion_factor": "tỉ lệ quy đổi|tỷ lệ quy đổi|hệ số quy đổi|heso|tylequydoi|tilequydoi|conversion_factor",
    "conversion_unit": "đơn vị quy đổi|donviquydoi|conversion_unit",
    "base_unit": "đơn vị cơ bản|donvicoban|base_unit",
    "product_category": "loại sản phẩm|nhóm hàng|loaisp|nhomhang|product_category",
    "purchase_id": "mã đơn nhập|mã phiếu nhập|maphieunhap|madonnhap|purchase_id",
    "purchased_at": "ngày nhập|ngaynhap|purchased_at",
    "supplier_id": "mã nhà cung cấp|mancc|manhacungcap|supplier_id",
    "batch_id": "mã lô|số lô|malo|solo|batch_id",
    "expires_at": "hạn sử dụng|hạn dùng|hansudung|handung|expires_at",
    "inventory_qty": "tồn cuối|toncuoi|tồn kho|tonkho|inventory_qty",
    "event_at": "ngày phát sinh|ngayphatsinh|ngày chứng từ|ngaychungtu|event_at",
    "expense_amount": "tiền chi|expense_amount",
    "order_revenue": "order_revenue|merchandise_total",
    "due_at": "ngày phải trả|hạn thanh toán|due_at|due_date",
}
ROLE_LOOKUP = {
    normalize_lookup(alias): role
    for role, aliases in ALIASES.items()
    for alias in aliases.split("|")
}
ROLE_LOOKUP.update(
    {
        normalize_lookup(alias): role
        for role, aliases in {
            "scope": "IDCuaHang|store_id|scope",
            "order_id": "MaHoaDon|Mã chứng từ|Số HĐ",
            "product_id": "MaHangHoa|ID SP",
            "qty_base": "SL quy đổi",
            "unit_price": "GiaDonVi|Giá đơn vị",
            "line_amount": "T.Tiền",
            "conversion_factor": "HeSoQuyDoi",
            "ordered_at": "ThoiGianLap",
            "ordered_date": "NgayLap",
            "paid_at": "NgayGioThu|Thời gian thu",
            "event_at": "NgayGioPhatSinh",
            "purchased_at": "NgayNhapKho",
            "price_effective_at": "NgayApDung",
            "returned_at": "NgayGioTra",
            "base_unit": "DonViCoSo",
            "payment_method": "Phương thức TT",
            "sales_channel": "KenhBanHang|Bán qua|sales_channel",
        }.items()
        for alias in aliases.split("|")
    }
)
ROLE_LOOKUP.update({
    normalize_lookup(alias): role
    for role, aliases in {
        "order_id": "Đơn số|HĐ|Bill",
        "ordered_at": "Giờ tạo đơn|Giờ bán|Ngày HĐ|Ngày xuất",
        "status": "Trạng thái giao|Trạng thái xử lý|Tiến độ|TT đơn",
        "product_id": "Barcode|Mã H.hóa|Mã H.hoá|Mã H.hoa|Mã h.hóa|Ma hang|Mã hàng hóa",
        "product_name": "Hàng hóa|Hàng hoá|Hàng bán|Mặt hàng|Tên hàng hóa",
        "qty_sale": "S.L|SL bán|SLg|Sluong",
        "qty_base": "SL (ĐV cơ bản)|SL lẻ|Số lượng lẻ",
        "unit": "ĐV|Đơn vị (tính)",
        "customer_name": "Khách hàng (tên)|Khách|Họ tên khách",
        "line_amount": "Thành tiền (đ)|Amount",
        "unit_price": "Đơn giá (đ)|Giá 1 đv",
        "qty_returned": "Số lượng trả",
    }.items()
    for alias in aliases.split("|")
})
ALIASES["qty_returned"] = "số lượng trả|qty_returned"
NUMERIC_ROLES = {
    "order_revenue",
    "contract_amount",
    "upfront_amount",
    "qty_sale",
    "qty_base",
    "line_amount",
    "unit_price",
    "cost_sale",
    "total_due",
    "paid_amount",
    "conversion_factor",
    "inventory_qty",
    "qty_returned",
    "expense_amount",
}
DATE_ROLES = {
    "due_at",
    "contract_at",
    "ordered_at",
    "ordered_date",
    "paid_at",
    "purchased_at",
    "expires_at",
    "event_at",
    "price_effective_at",
    "returned_at",
    "expense_at",
}
MONEY_ROLES = {"line_amount", "unit_price", "cost_sale", "total_due", "paid_amount", "expense_amount", "contract_amount", "upfront_amount", "order_revenue"}


def column_descriptions(readme: str, filename: str, sheet: str) -> dict[str, str]:
    """Read scoped column descriptions, including partitioned exports of a base file."""
    descriptions = {}
    applicable = False
    for line in readme.splitlines():
        if line.startswith("## "):
            names = re.findall(r"`([^`]+)`", line)
            applicable = False
            if names:
                wanted = normalize_lookup(names[0].rsplit(".", 1)[0])
                actual = normalize_lookup(filename.rsplit(".", 1)[0])
                applicable = actual == wanted or bool(
                    re.fullmatch(re.escape(wanted) + r" (?:\d+|20\d{2})", actual)
                )
                if len(names) > 1:
                    applicable = applicable and normalize_lookup(names[1]) == normalize_lookup(
                        sheet
                    )
        match = re.match(r"\s*-\s*\*\*(.+?)\*\*\s*:\s*(.+)", line)
        if applicable and match:
            descriptions[normalize_lookup(match.group(1))] = match.group(2)
    return descriptions


def described_role(label: str, description: str, table_label: str) -> str | None:
    text = normalize_lookup(description)
    table = normalize_lookup(table_label)
    if text.startswith("tien hang cua don") and "thanh tien" in text:
        return "order_revenue"  # Documented item subtotal, not payment or total including shipping.
    for prefix, role in [
        ("ma don", "order_id"),
        ("ma phieu nhap", "purchase_id"),
        ("ma hang", "product_id"),
        ("ma khach", "customer_id"),
        ("ten khach", "customer_name"),
        ("ten hang", "product_name"),
        ("trang thai don", "status"),
        ("ngay ban", "ordered_at"),
        ("ngay dat", "ordered_at"),
        ("ngay sua", "event_at"),
        ("tong tien ca phieu", "line_amount"),
        ("thanh tien", "line_amount"),
        ("so luong da doi ra don vi co ban", "qty_base"),
        ("thoi diem thu", "paid_at"),
        ("so khach phai tra", "total_due"),
        ("tong tien don", "total_due"),
        ("don gia", "unit_price"),
    ]:
        if text.startswith(prefix):
            return role
    if text == "ten" and any(word in table for word in ["san pham", "sachsanpham", "thuoc"]):
        return "product_name"
    if text == "ten" and any(word in table for word in ["khach hang", "khachhang"]):
        return "customer_name"
    if text.startswith("so tien") and any(
        word in table for word in ["thanh toan", "thanhtoan", "phieu thu"]
    ):
        return "paid_amount"
    if text == "so luong" or text.startswith("so luong ghi ") or text.startswith("so luong ban "):
        return "qty_sale"
    return None


def infer_role(label: str, readme: str = "") -> str | None:
    normalized = normalize_lookup(label)
    role = ROLE_LOOKUP.get(normalized)
    if role:
        return role
    # Parenthetical currency labels retain the meaning of the base column.
    base = re.sub(r"\s*\((?:đ|₫|vnd|vnđ)\)\s*$", "", label, flags=re.IGNORECASE)
    if base != label and normalize_lookup(base) in ROLE_LOOKUP:
        return ROLE_LOOKUP[normalize_lookup(base)]
    if normalized == "tong tien":
        for line in readme.splitlines():
            if normalize_lookup(label) in normalize_lookup(
                line
            ) and "thanh tien" in normalize_lookup(line):
                return "line_amount"
    return None


def infer_grain(roles: set[str], table_label: str, readme: str) -> str:
    label = normalize_lookup(table_label)
    if {"contract_id", "contract_at", "contract_amount", "paid_at", "paid_amount"} <= roles:
        return "installment_ledger"
    if "order_id" in roles and roles & {"product_id", "product_name"} and "qty_sale" in roles:
        return "order_line"
    if {"order_id", "paid_amount", "paid_at"} <= roles:
        return "payment_event"
    if {"order_id", "ordered_at"} <= roles:
        return "order_header"
    if "conversion_factor" in roles:
        return "unit_conversion"
    if "purchase_id" in roles and roles & {"product_id", "product_name"}:
        return "purchase_line"
    if "purchase_id" in roles and "purchased_at" in roles:
        return "purchase_header"
    if "inventory_qty" in roles:
        return "inventory_event" if roles & DATE_ROLES else "snapshot"
    if (
        roles & {"product_id", "product_name"}
        and roles & {"line_amount", "ordered_at"}
        and "order_id" not in roles
    ):
        return "independent_ledger_row"
    if roles & {"product_id", "product_name"} and not roles & {
        "qty_sale",
        "order_id",
        "purchase_id",
    }:
        return "product_dimension"
    if "so" in label or "mỗi dòng" in readme.lower():
        return "independent_ledger_row"
    return "unknown"


def sheet_year(label: str) -> int | None:
    years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", label))
    return int(years.pop()) if len(years) == 1 else None
