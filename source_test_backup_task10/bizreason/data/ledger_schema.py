"""Recognize typed ledger fields by a conjunction of headers, never business IDs."""

from __future__ import annotations

from bizreason.utils import normalize_lookup


def contextual_roles(headers: tuple, roles: list[str | None], descriptions: dict[str, str], readme: str = "") -> list[str | None]:
    result = list(roles)
    labels = [normalize_lookup(str(h)) for h in headers]
    present = set(labels)
    documentation = normalize_lookup(readme)
    numbered_orders = "stt" in documentation and any(
        phrase in documentation for phrase in ("cung stt la mot don", "so thu tu don", "same stt is one order")
    )
    installment = (
        bool(present & {"stt", "contract id", "ma hop dong"})
        and bool(present & {"tra truoc", "down payment", "upfront amount"})
        and bool(present & {"ngay gop", "payment date"})
        and bool(present & {"da gop", "payment amount"})
        and bool(present & {"gia", "contract amount", "tong gia tri"})
    )
    aliases = {
        "contract_id": {"stt", "contract id", "ma hop dong"},
        "contract_at": {"ngay", "contract date", "ngay mua"},
        "contract_amount": {"gia", "contract amount", "tong gia tri"},
        "upfront_amount": {"tra truoc", "down payment", "upfront amount"},
        "paid_at": {"ngay gop", "payment date"},
        "paid_amount": {"da gop", "payment amount"},
    }
    for i, label in enumerate(labels):
        description = normalize_lookup(descriptions.get(label, ""))
        if installment:
            for role, choices in aliases.items():
                if label in choices:
                    result[i] = role
        if label in {"vehicle description", "vehicle with plate"} or (
            "bien so" in description and "dong xe" in description
        ):
            result[i] = "vehicle_description"
        if numbered_orders and {"stt", "ngay", "hang", "sl"} <= present and not installment:
            if label == "stt":
                result[i] = "order_id"
            elif label == "ngay":
                result[i] = "ordered_at"
            elif label == "hang":
                result[i] = "product_name"
    return result
