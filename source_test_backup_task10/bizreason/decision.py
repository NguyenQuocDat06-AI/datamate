from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bizreason.contracts import BusinessContext, Candidate, Decision, QueryPlan
from bizreason.data.schema import ALIASES, ROLE_LOOKUP
from bizreason.utils import normalize_lookup

FEATURE_NAMES = [
    "template",
    "execution_success",
    "check_count",
    "log_result_rows",
    "repair_count",
    "unsupported_evidence",
]

HISTORICAL_EVENT_DATE_ROLES = {
    "ordered_at",
    "purchased_at",
    "paid_at",
    "event_at",
    "expense_at",
}

NON_COVERAGE_ROLES = {
    "due_at",
    "expires_at",
    "price_effective_at",
    "returned_at",
}


def candidate_features(
    candidate: Candidate | None, repair_count: int, unsupported: bool = False
) -> list[float]:
    if candidate is None:
        return [0.0, 0.0, 0.0, 0.0, float(repair_count), float(unsupported)]
    return [
        float(candidate.program.backend == "template"),
        float(candidate.verified),
        float(len(candidate.checks)),
        math.log1p(candidate.execution.row_count),
        float(repair_count),
        float(unsupported),
    ]


CLAIMABLE_MISSING_ROLES: dict[str, str] = {
    **ALIASES,
    "channel": "kênh bán|kênh bán hàng|kênh đặt hàng|kenh ban|kenh ban hang|kenh dat hang|sales channel|order channel|selling channel|hinh thuc ban|channel",
    "stock": "tồn kho|ton kho|tồn cuối|ton cuoi|số lượng tồn|so luong ton|inventory_qty|inventory_quantity|stock|stock_quantity|tồn|ton",
    "customer_address": "địa chỉ khách hàng|địa chỉ giao hàng|địa chỉ nhận hàng|địa chỉ|diachi|tuyến đường|quận huyện|phường xã|customer_address|address|shipping_address|delivery_address|street",
    "customer_phone": "số điện thoại khách hàng|số điện thoại|điện thoại|sdt|phone|customer_phone|phone_number",
    "customer_age": "tuổi khách hàng|độ tuổi|ngày sinh|năm sinh|giới tính|customer_age|age|birth_date|gender",
    "sales_staff": "nhân viên bán hàng|nhân viên thu ngân|nhân viên|người bán|thu ngân|mã nhân viên|tên nhân viên|sales_staff|staff|employee|cashier",
    "shipping_fee": "phí vận chuyển|phí giao hàng|phí ship|tiền ship|cước vận chuyển|shipping_fee|delivery_fee|shipping_cost",
    "discount_amount": "tiền giảm giá|chiết khấu|giảm giá|mã giảm giá|khuyến mãi|voucher|discount_amount|discount",
    "tax_amount": "thuế vat|tiền thuế|thuế gtgt|thuế suất|tax_amount|vat|tax",
    "warranty_period": "thời hạn bảo hành|hạn bảo hành|bảo hành|warranty_period|warranty",
    "cancel_reason": "lý do hủy|nguyên nhân hủy|lý do trả hàng|cancel_reason|return_reason|cancellation_reason",
    "manufactured_at": "ngày sản xuất|nsx|manufactured_at|production_date|mfg_date",
    "customer_feedback": "đánh giá khách hàng|phản hồi khách hàng|thái độ phục vụ|thái độ nhân viên|điểm đánh giá|review|rating|feedback|customer_feedback|employee_attitude|service_attitude",
    "dosage_instruction": "liều dùng|cách dùng|hướng dẫn sử dụng|liều lượng|chỉ định|liều uống|dosage|dosage_per_intake|usage_instruction|posology",
}


ROLE_SYNONYMS = {
    "expiry_date": "expires_at",
    "expiration_date": "expires_at",
    "expire_date": "expires_at",
    "expired_at": "expires_at",
    "shelf_life": "expires_at",
    "best_before": "expires_at",
    "han_su_dung": "expires_at",
    "han_dung": "expires_at",
    "ngay_het_han": "expires_at",
    "hsd": "expires_at",
    "lot_number": "batch_id",
    "batch_number": "batch_id",
    "lot_id": "batch_id",
    "stock": "stock",
    "inventory_quantity": "stock",
    "qty_stock": "stock",
    "stock_quantity": "stock",
    "current_stock": "stock",
    "closing_stock": "stock",
    "remaining_stock": "stock",
    "inventory_level": "stock",
    "ton_kho": "stock",
    "inventory_balance": "stock",
    "stock_qty": "stock",
    "stock_balance": "stock",
    "on_hand_qty": "stock",
    "cost_price": "cost_sale",
    "cogs": "cost_sale",
    "unit_cost": "cost_sale",
    "expense": "expense_amount",
    "operating_expense": "expense_amount",
    "category": "product_category",
    "channel": "channel",
    "sales_channel": "channel",
    "order_channel": "channel",
    "selling_channel": "channel",
    "kenh_ban": "channel",
    "kenh_ban_hang": "channel",
    "kenh_dat_hang": "channel",
    "return_qty": "qty_returned",
    "returned_qty": "qty_returned",
    "supplier": "supplier_id",
    "vendor_id": "supplier_id",
    "address": "customer_address",
    "customer_address": "customer_address",
    "shipping_address": "customer_address",
    "delivery_address": "customer_address",
    "street": "customer_address",
    "dia_chi": "customer_address",
    "dia_chi_khach_hang": "customer_address",
    "phone": "customer_phone",
    "phone_number": "customer_phone",
    "sdt": "customer_phone",
    "age": "customer_age",
    "gender": "customer_age",
    "birth_date": "customer_age",
    "staff": "sales_staff",
    "employee": "sales_staff",
    "cashier": "sales_staff",
    "employee_id": "sales_staff",
    "shipping_cost": "shipping_fee",
    "delivery_fee": "shipping_fee",
    "discount": "discount_amount",
    "tax": "tax_amount",
    "vat": "tax_amount",
    "warranty": "warranty_period",
    "return_reason": "cancel_reason",
    "cancellation_reason": "cancel_reason",
    "production_date": "manufactured_at",
    "manufacturing_date": "manufactured_at",
    "import_quantity": "stock",
    "received_quantity": "stock",
    "qty_import": "stock",
    "purchase_price": "cost_sale",
    "import_price": "cost_sale",
    "don_gia_nhap": "cost_sale",
    "customer_feedback": "customer_feedback",
    "employee_attitude": "customer_feedback",
    "service_attitude": "customer_feedback",
    "review": "customer_feedback",
    "rating": "customer_feedback",
    "dosage": "dosage_instruction",
    "dosage_per_intake": "dosage_instruction",
    "dosage_instruction": "dosage_instruction",
    "usage_instruction": "dosage_instruction",
}

CLAIMABLE_LOOKUP: dict[str, str] = dict(ROLE_LOOKUP)
for role_name, aliases_str in CLAIMABLE_MISSING_ROLES.items():
    for alias in aliases_str.split("|"):
        norm = normalize_lookup(alias)
        if norm and norm not in CLAIMABLE_LOOKUP:
            CLAIMABLE_LOOKUP[norm] = role_name


def _canonical_required_role(role: str) -> str | None:
    cleaned = role.strip().lower()
    if cleaned in CLAIMABLE_MISSING_ROLES:
        return cleaned
    syn = ROLE_SYNONYMS.get(cleaned)
    if syn and syn in CLAIMABLE_MISSING_ROLES:
        return syn
    from_lookup = ROLE_LOOKUP.get(normalize_lookup(role))
    if from_lookup and from_lookup in CLAIMABLE_MISSING_ROLES:
        return from_lookup
    from_claimable = CLAIMABLE_LOOKUP.get(normalize_lookup(role))
    if from_claimable and from_claimable in CLAIMABLE_MISSING_ROLES:
        return from_claimable
    tokens = set(normalize_lookup(role).split())
    if tokens:
        if tokens <= {
            "inventory",
            "stock",
            "qty",
            "quantity",
            "balance",
            "on",
            "hand",
            "current",
            "closing",
            "remaining",
            "ton",
            "kho",
        } and ("stock" in tokens or "inventory" in tokens or "ton" in tokens):
            return "stock"
        if "channel" in tokens or ("kenh" in tokens and "ban" in tokens):
            return "channel"
        if (tokens & {"expiry", "expiration", "expire", "expires"}) or (
            "han" in tokens and ("dung" in tokens or "su" in tokens)
        ):
            return "expires_at"
        if "address" in tokens or "street" in tokens or ("dia" in tokens and "chi" in tokens):
            return "customer_address"
        if tokens & {"attitude", "feedback", "review", "rating"}:
            return "customer_feedback"
        if tokens & {"dosage", "posology"} or ("lieu" in tokens and "dung" in tokens):
            return "dosage_instruction"
    return None


@dataclass(frozen=True)
class UnsupportedEvidence:
    verified: bool
    reason: str
    sources: tuple[str, ...] = ()


def verify_unsupported(plan: QueryPlan, context: BusinessContext) -> UnsupportedEvidence:
    claim = plan.unsupported
    if not claim or not claim.source_refs:
        return UnsupportedEvidence(False, "No source-linked unsupported claim")
    if any(item.code in {"SOURCE_READ_ERROR", "UNRESOLVED_HEADER"} for item in context.diagnostics):
        return UnsupportedEvidence(False, "Unread sources cannot prove missing data")
    if claim.kind == "outside_coverage":
        if not claim.required or len(claim.required) != 1 or claim.required[0] not in HISTORICAL_EVENT_DATE_ROLES:
            return UnsupportedEvidence(
                False, "Sales coverage cannot establish absence in another data domain"
            )
        role = claim.required[0]
        if not plan.windows:
            return UnsupportedEvidence(False, "No explicit requested time window")
        period_windows = [w for w in plan.windows if w.mode == "period"]
        if not period_windows:
            return UnsupportedEvidence(False, "Cumulative windows do not establish outside_coverage")
        bounds = []
        for table in context.tables:
            for column in table.columns:
                if column.semantic_role == role:
                    cov = table.coverage.get(column.name)
                    if cov and cov[0] and cov[1]:
                        bounds.append(cov)
                    else:
                        return UnsupportedEvidence(
                            False, f"Coverage bounds missing for {table.name}.{column.name}"
                        )
        if not bounds:
            return UnsupportedEvidence(False, "Transaction coverage is unknown" if role == "ordered_at" else f"{role} coverage is unknown")
        start_candidates = [bound[0][:10] for bound in bounds]
        end_candidates = [bound[1][:10] for bound in bounds]
        if role == "ordered_at":
            if context.declared_start:
                start_candidates.append(context.declared_start.isoformat())
            if context.declared_end:
                end_candidates.append(context.declared_end.isoformat())
        start = min(start_candidates)
        end = max(end_candidates)
        if any(
            window.end_exclusive.isoformat() <= start
            or window.start.isoformat() > end
            for window in period_windows
        ):
            reason = (
                f"Requested sales period is outside released coverage {start}..{end}"
                if role == "ordered_at"
                else f"Requested {role} period is outside released coverage {start}..{end}"
            )
            return UnsupportedEvidence(
                True,
                reason,
                (f"catalog:{role}", "README.md"),
            )
        return UnsupportedEvidence(False, "Requested period overlaps released data")
    canonical_roles = [_canonical_required_role(r) for r in (claim.required or [])]
    if not canonical_roles or any(r is None for r in canonical_roles):
        return UnsupportedEvidence(False, "Required field absence was not established")
    present = {
        column.semantic_role
        for table in context.tables
        for column in table.columns
        if column.semantic_role
    }
    if "inventory_qty" in present:
        present.add("stock")
    if "stock" in present:
        present.add("inventory_qty")
    if "sales_channel" in present:
        present.add("channel")
    for r, aliases_str in CLAIMABLE_MISSING_ROLES.items():
        role_aliases = {
            normalize_lookup(alias)
            for alias in aliases_str.split("|")
            if normalize_lookup(alias)
        }
        if any(
            normalize_lookup(column.original_name) in role_aliases
            for table in context.tables
            for column in table.columns
        ):
            present.add(r)
    missing_roles = [r for r in canonical_roles if r is not None and r not in present]
    if not missing_roles:
        return UnsupportedEvidence(False, "Required field absence was not established")
    if claim.kind == "missing_history":
        if "khong co lich su" in normalize_lookup(context.readme):
            return UnsupportedEvidence(
                True, "README explicitly states missing history", ("README.md",)
            )
        return UnsupportedEvidence(
            False, "Snapshot or query failure alone cannot prove missing history"
        )
    known_sources = (
        set(context.manifest)
        | {Path(p).name for p in context.manifest}
        | {source.path for table in context.tables for source in table.sources if source.path}
        | {Path(source.path).name for table in context.tables for source in table.sources if source.path}
        | {table.name for table in context.tables}
        | ({"README.md"} if context.readme else set())
    )
    harmless = {
        "ghi chu", "note", "notes", "no", "stt", "so thu tu",
        "gio", "thoi gian", "bao hanh", "duoc ban truc tiep",
        "dinh muc ton toi thieu", "dinh muc ton toi da",
        "dv lon", "dv nho", "he so qd", "gia moi", "gia cu", "gia nhap",
        "ton thuc", "ton dau", "sl nhap", "sl xuat", "nhap", "xuat",
        "dien thoai", "sdt", "so dien thoai", "dia chi", "email",
        "loai hang hoa", "nhom hang hoa", "thuong hieu", "hang san xuat",
        "xuat xu", "quy cach", "trong luong", "kich thuoc",
        "noi dung",
        "ma barcode", "barcode", "ma vach",
        "ton lon nhat", "ton nho nhat", "ton toi thieu", "ton toi da",
        "diem tich luy", "tong diem", "trang thai", "ma so thue",
        "loai hinh khach", "ngay sinh", "nguoi tao", "nha cung cap",
    }
    missing_aliases = {
        normalize_lookup(alias)
        for r in missing_roles
        for alias in CLAIMABLE_MISSING_ROLES[r].split("|")
        if normalize_lookup(alias)
    }
    readme_documented: dict[str, str] = {
        normalize_lookup(m.group(1)): normalize_lookup(m.group(2))
        for line in (context.readme or "").splitlines()
        if (m := re.match(r"^\s*-\s*\*\*(.+?)\*\*\s*:\s*(.+)$", line))
    }
    inventory_mapped = all(
        column.semantic_role or column.null_count == table.row_count
        or (
            normalize_lookup(column.original_name) in harmless
            and normalize_lookup(column.original_name) not in missing_aliases
        )
        or (
            (norm_col := normalize_lookup(column.original_name)) in readme_documented
            and not any(
                alias in norm_col or alias in readme_documented[norm_col]
                for alias in missing_aliases
            )
        )
        for table in context.tables for column in table.columns
    )
    # Absence is supported only after all sources and all populated headers have
    # been accounted for. An unmapped field is not proof that a capability is absent.
    if context.schema_complete and inventory_mapped and set(claim.source_refs) & known_sources:
        return UnsupportedEvidence(
            True, "Complete source/header inventory excludes required fields: " + ", ".join(claim.required),
            tuple(sorted(known_sources)),
        )
    negative_lines = [
        normalize_lookup(line)
        for line in context.readme.splitlines()
        if any(term in normalize_lookup(line) for term in ["khong ghi", "khong co", "khong luu"])
    ]
    if all(
        any(
            normalize_lookup(alias) in line
            for alias in CLAIMABLE_MISSING_ROLES[role].split("|")
            for line in negative_lines
        )
        for role in missing_roles
    ):
        return UnsupportedEvidence(
            True, "README explicitly excludes required fields", ("README.md",)
        )
    return UnsupportedEvidence(
        False, "No explicit evidence that the data cannot support this question"
    )


def _resolve_plan_event_role(plan: QueryPlan, context: BusinessContext) -> str | None:
    roles = []
    for window in plan.windows:
        if window.field:
            if "." in window.field:
                tbl_name, col_name = window.field.rsplit(".", 1)
                tbl = context.table(tbl_name)
                col = tbl.column(col_name) if tbl else None
                roles.append(col.semantic_role if col else None)
            else:
                matching = [
                    col.semantic_role
                    for tbl_name in plan.tables
                    if (tbl := context.table(tbl_name))
                    for col in tbl.columns
                    if col.name == window.field
                ]
                roles.append(matching[0] if len(matching) == 1 else None)
        else:
            roles.append(None)

    if all(r is not None for r in roles):
        unique_roles = set(roles)
        if len(unique_roles) == 1:
            target_role = next(iter(unique_roles))
            if target_role in HISTORICAL_EVENT_DATE_ROLES:
                return target_role
        return None

    selected_tables = [context.table(name) for name in plan.tables if context.table(name)]
    all_table_roles = {
        col.semantic_role
        for tbl in selected_tables
        for col in tbl.columns
        if col.semantic_role
    }
    hist_roles = all_table_roles & HISTORICAL_EVENT_DATE_ROLES
    future_roles = all_table_roles & {"due_at", "expires_at"}
    if len(hist_roles) == 1 and not future_roles:
        target_role = next(iter(hist_roles))
        specified_roles = {r for r in roles if r is not None}
        if not specified_roles or specified_roles == {target_role}:
            return target_role
    return None


def assess_metric_coverage(
    plan: QueryPlan, context: BusinessContext, rule_text: str
) -> UnsupportedEvidence:
    """Do not turn an out-of-coverage sales query into zero via COALESCE."""
    supported_metrics = {
        "sales_revenue",
        "cancellation_rate",
        "top_products",
        "gross_profit",
        "generic_sql",
    }
    if plan.metric not in supported_metrics:
        return UnsupportedEvidence(False, "No automatic coverage policy for this metric")
    if "ky nam hoan toan ngoai khoang ngay co du lieu" not in normalize_lookup(rule_text):
        return UnsupportedEvidence(
            False, "Runtime rulebook does not establish this coverage policy"
        )
    from bizreason.contracts import UnsupportedClaim

    if plan.metric in {"sales_revenue", "cancellation_rate", "top_products", "gross_profit"}:
        claim = UnsupportedClaim(
            kind="outside_coverage",
            required=["ordered_at"],
            explanation="Check every required sales time window against released coverage",
            source_refs=["catalog:ordered_at", "README.md", "rulebook.md"],
        )
        return verify_unsupported(plan.model_copy(update={"unsupported": claim}), context)

    # generic_sql
    if not plan.windows or any(window.mode != "period" for window in plan.windows):
        return UnsupportedEvidence(
            False, "generic_sql out-of-coverage requires explicit period windows"
        )

    due_or_expires_col_names = {
        col.name
        for tbl in context.tables
        for col in tbl.columns
        if col.semantic_role in {"due_at", "expires_at"}
    } | {"due_at", "expires_at"}
    sql_clean = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/", " ", plan.sql or "")
    if any(
        re.search(r"(?<![\w])" + re.escape(c) + r"(?![\w])", sql_clean, re.I)
        for c in due_or_expires_col_names
    ):
        return UnsupportedEvidence(False, "Query references future/due date fields")

    role = _resolve_plan_event_role(plan, context)
    if not role or role not in HISTORICAL_EVENT_DATE_ROLES:
        return UnsupportedEvidence(
            False, "generic_sql windows do not resolve to a single historical event role"
        )

    claim = UnsupportedClaim(
        kind="outside_coverage",
        required=[role],
        explanation=f"Check every required {role} time window against released coverage",
        source_refs=[f"catalog:{role}", "README.md", "rulebook.md"],
    )
    return verify_unsupported(plan.model_copy(update={"unsupported": claim}), context)


def _probability(model: dict[str, Any], features: list[float]) -> float:
    scaled = [
        (value - mean) / (scale or 1)
        for value, mean, scale in zip(features, model["mean"], model["scale"])
    ]
    logit = model["intercept"] + sum(
        value * coefficient for value, coefficient in zip(scaled, model["coefficients"])
    )
    return 1 / (1 + math.exp(-max(-700, min(700, logit))))


class DecisionGate:
    def __init__(self, mode: str = "verified", calibration: dict[str, Any] | None = None):
        if mode not in {"verified", "templates", "abstain", "calibrated"}:
            raise ValueError("Unknown gate mode")
        if mode == "calibrated" and not calibration:
            raise ValueError("Calibrated mode requires a fitted calibration artifact")
        if calibration and calibration.get("feature_names") != FEATURE_NAMES:
            raise ValueError("Calibration feature schema mismatch")
        self.mode, self.calibration = mode, calibration

    def choose(
        self,
        candidate: Candidate | None,
        unsupported: UnsupportedEvidence,
        repair_count: int = 0,
        *,
        semantic_score: float | None = None,
    ) -> Decision:
        critic_rejected = semantic_score is not None and semantic_score < 0.5
        eligible_value = candidate is not None and candidate.verified and not critic_rejected
        if self.mode == "abstain":
            return Decision(
                action="abstain", reason="Configured abstain baseline", policy_version="abstain-v1"
            )
        if self.mode == "calibrated":
            assert self.calibration is not None
            features = candidate_features(candidate, repair_count, unsupported.verified)
            p_value = (
                _probability(self.calibration["value_model"], features)
                if eligible_value and not critic_rejected
                else 0.0
            )
            p_u = (
                _probability(self.calibration["unanswerable_model"], features)
                if unsupported.verified
                else 0.0
            )
            scores = {"abstain": 0.0, "value": 2 * p_value - 1, "unanswerable": 2 * p_u - 1}
            action = max(
                scores, key=scores.get
            )  # Insertion order makes abstain win exact zero ties.
            return Decision(
                action=action,
                reason="Maximum estimated PA utility among eligible actions",
                p_value=p_value,
                p_unanswerable=p_u,
                policy_version="logistic-gate-v1",
            )
        if critic_rejected and not unsupported.verified:
            return Decision(
                action="abstain",
                reason=f"Candidate rejected by semantic critic (score={semantic_score:.2f})",
                policy_version=f"{self.mode}-critic-veto-v1",
            )
        if unsupported.verified:
            return Decision(
                action="unanswerable", reason=unsupported.reason, policy_version="evidence-gate-v1"
            )
        if eligible_value and (self.mode != "templates" or candidate.program.backend == "template"):
            return Decision(
                action="value",
                reason="Candidate passed configured structural checks; probability is uncalibrated",
                policy_version=f"{self.mode}-uncalibrated-v1",
            )
        return Decision(
            action="abstain",
            reason="No eligible candidate or unsupported evidence",
            policy_version=f"{self.mode}-uncalibrated-v1",
        )
