from __future__ import annotations

import sys
from pathlib import Path
_SOURCE_TEST_ROOT = Path(__file__).resolve().parent.parent
if str(_SOURCE_TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_TEST_ROOT))

import csv
import tempfile

import pytest

from bizreason.contracts import OutputSpec, QueryPlan, TimeWindow
from bizreason.data.ingestion import BusinessBuilder
from bizreason.rules import RuleRegistry
from bizreason.utils import WORKSPACE

RULEBOOK = """# Business rules
## 1. Đơn hàng và doanh thu
Đơn không huỷ và không trả hàng. Đơn đang giao vẫn tính.
Doanh thu = thành tiền các dòng hàng; không cộng thuế VAT, không gồm phí giao hàng.
Tỉ lệ huỷ: mọi đơn đặt trong kỳ, mọi trạng thái.
## 2. Thời gian
Ngày đặt, khoảng ngày đến hết ngày chốt.
## 3. Xếp hạng
Theo số lượng quy về đơn vị cơ bản, giảm dần, cùng số lượng theo mã tăng dần. Bỏ Phí phục vụ.
## 4. Công nợ
Tổng đơn trừ tiền đã trả đến ngày chốt. Đơn không huỷ. Đơn trả hàng vẫn tính.
Đơn COD, đơn bán qua sàn TMĐT không phải khách nợ.
## 5. Kho, giá vốn
Lãi gộp lấy giá vốn ghi trên dòng bán cho một đơn vị bán.
## 7. Không trả lời được
Thiếu dữ liệu không phải bằng 0.
Kỳ nằm hoàn toàn ngoài khoảng ngày có dữ liệu thì không trả lời được.
## 8. Cách ghi
Ô trống cùng một nhóm lấy dòng đầu nhóm. Dòng trùng cùng đơn tính một lần.
## 9. Đáp án
Tiền nguyên VND, phần trăm 2 số thập phân.
## 10. Từ ngữ
Đơn vị không có dòng quy đổi là đơn vị cơ bản.
"""


@pytest.fixture
def workspace_tmp():
    root = WORKSPACE / "artifacts" / "test_tmp"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        yield Path(directory)


def write_csv(path: Path, columns: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


@pytest.fixture
def rules():
    return RuleRegistry(RULEBOOK)


@pytest.fixture
def business(workspace_tmp, rules):
    directory = workspace_tmp / "business"
    directory.mkdir()
    (directory / "README.md").write_text(
        "Dữ liệu có từ 01/02/2026 đến hết 10/03/2026. Mỗi dòng một sản phẩm, thông tin đơn lặp lại.",
        encoding="utf-8",
    )
    orders = [
        ["O1", "2026-03-01", "completed", 250000, "ghi nợ", "K1"],
        ["O2", "2026-03-02", "returned", 50000, "ghi nợ", "K1"],
        ["O3", "2026-03-03", "completed", 80000, "COD", "K2"],
        ["O4", "2026-03-04", "shipping", 40000, "ghi nợ", None],
        ["O5", "2026-03-02", "cancelled", 30000, "tiền mặt", "K3"],
    ]
    write_csv(
        directory / "orders.csv",
        ["order_id", "ordered_at", "status", "total_due", "payment_method", "customer_id"],
        orders,
    )
    base = {row[0]: row for row in orders}
    lines = [
        ["O1", "P1", "A", 2, 2, 20000, 7000],
        ["O1", "P2", "B", 1, 24, 240000, 180000],
        ["O2", "P1", "A", 5, 5, 50000, 7000],
        ["O3", "P1", "A", 8, 8, 80000, 6250],
        ["O4", "P3", "C", 4, 4, 40000, 6250],
        ["O5", "P1", "A", 3, 3, 30000, 7000],
    ]
    rows = [[item[0], base[item[0]][1], base[item[0]][2], *item[1:]] for item in lines]
    write_csv(
        directory / "lines.csv",
        [
            "order_id",
            "ordered_at",
            "status",
            "product_id",
            "product_name",
            "qty_sale",
            "qty_base",
            "line_amount",
            "cost_sale",
        ],
        rows,
    )
    write_csv(
        directory / "payments.csv",
        ["order_id", "paid_at", "paid_amount"],
        [
            ["O1", "2026-03-01", 150000],
            ["O1", "2026-03-03", 50000],
            ["O1", "2026-03-06", 50000],
            ["O2", "2026-03-02", 10000],
            ["O4", "2026-03-04", 5000],
        ],
    )
    return BusinessBuilder(workspace_tmp / "cache", rule_hash=rules.revision).prepare(directory)


@pytest.fixture
def plan_factory(business, rules):
    def make(metric="sales_revenue", *, output_type="number", shape="scalar", **changes):
        table_names = [
            table.name
            for table in business.tables
            if table.grain
            in ({"order_header", "payment_event"} if metric == "debt_total" else {"order_line"})
        ]
        policies = {
            "sales_revenue": "revenue_eligible_recorded_status",
            "top_products": "revenue_eligible_recorded_status",
            "gross_profit": "revenue_eligible_recorded_status",
            "debt_total": "debt_eligible_recorded_status",
            "cancellation_rate": "all_orders",
        }
        values = dict(
            metric=metric,
            tables=table_names,
            rule_refs=rules.required_refs(metric),
            template_eligible=True,
            windows=[TimeWindow(start="2026-03-01", end_exclusive="2026-03-05")],
            status_policy=policies.get(metric, ""),
            output=OutputSpec(type=output_type, shape=shape),
            limit=3 if metric == "top_products" else None,
        )
        values.update(changes)
        return QueryPlan(**values)

    return make
