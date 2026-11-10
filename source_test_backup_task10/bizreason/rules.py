from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bizreason.utils import digest, normalize_lookup


@dataclass(frozen=True)
class RuleRecord:
    id: str
    section: str
    text: str
    start_line: int
    end_line: int

    def render(self) -> str:
        return f"[{self.id}] rulebook.md:L{self.start_line}-L{self.end_line} ({self.section})\n{self.text}"


class RuleRegistry:
    def __init__(self, text: str):
        self.text = text
        self.revision = digest(text)
        self.records: list[RuleRecord] = []
        section = "preamble"
        start = 1
        buffer: list[str] = []

        def append(end: int) -> None:
            content = "\n".join(buffer).strip()
            if content:
                self.records.append(
                    RuleRecord(
                        "rb:" + digest([section, content])[:12], section, content, start, end
                    )
                )

        for number, line in enumerate(text.splitlines(), 1):
            if re.match(r"^#{1,4}\s+", line):
                append(number - 1)
                section = line.lstrip("# ")
                start, buffer = number, [line]
            else:
                buffer.append(line)
        append(len(text.splitlines()))
        self.by_id = {record.id: record for record in self.records}

    @classmethod
    def load(cls, path: Path) -> RuleRegistry:
        return cls(path.read_text(encoding="utf-8-sig"))

    @property
    def entries(self) -> dict[str, RuleRecord]:
        return self.by_id

    @property
    def inheritance_allowed(self) -> bool:
        text = normalize_lookup(self.text)
        return "cung mot nhom" in text and ("dong dau" in text or "ke thua" in text)

    def required_refs(self, metric: str) -> list[str]:
        sections = {
            "sales_revenue": [1, 2, 8, 9],
            "cancellation_rate": [1, 2, 8, 9],
            "top_products": [1, 2, 3, 8, 9, 10],
            "debt_total": [2, 4, 8, 9],
            "gross_profit": [1, 2, 5, 8, 9],
            "unsupported": [7],
        }.get(metric, [])
        return [
            record.id
            for record in self.records
            if any(re.match(rf"{number}\.\s", record.section) for number in sections)
        ]

    def standard_policy_supported(self, metric: str) -> bool:
        """Templates require the released rule's actual wording, not a stored train hash."""
        text = normalize_lookup(self.text)
        sales = "don khong huy va khong tra hang" in text and "don dang giao van tinh" in text
        if metric == "sales_revenue":
            return sales and all(
                term in text
                for term in [
                    "thanh tien cac dong hang",
                    "khong cong thue vat",
                    "khong gom phi giao hang",
                ]
            )
        if metric == "cancellation_rate":
            return sales and "moi don dat trong ky" in text and "moi trang thai" in text
        if metric == "top_products":
            return (
                sales
                and "theo so luong quy ve don vi co ban" in text
                and "ma" in text
                and "phi phuc vu" in text
            )
        if metric == "gross_profit":
            return sales and "gia von ghi tren dong ban" in text and "mot don vi ban" in text
        if metric == "debt_total":
            return all(
                term in text
                for term in [
                    "don khong huy",
                    "don tra hang van tinh",
                    "don cod don ban qua san tmdt",
                    "khong phai khach no",
                    "tong don",
                ]
            )
        return False

    def render(self) -> str:
        return "\n\n".join(record.render() for record in self.records)
