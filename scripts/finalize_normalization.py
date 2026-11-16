"""Build a checked manifest/index for the full run plus its explicit incremental patch."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path

from bizreason.utils import WORKSPACE, atomic_json, file_hash


def main():
    root = WORKSPACE / "artifacts/normalization_v2"
    public = json.loads((root / "public_ingestion_final.json").read_text(encoding="utf-8"))
    train = json.loads((root / "train_ingestion_final.json").read_text(encoding="utf-8"))
    patch = json.loads((root / "train_ingestion_patch.json").read_text(encoding="utf-8"))
    patched = {row["business_id"]: row for row in patch["businesses"]}
    train["businesses"] = [patched.get(row["business_id"], row) for row in train["businesses"]]
    details = []
    unmapped = []
    for split, report in [("public_test", public), ("train", train)]:
        with zipfile.ZipFile(WORKSPACE / ("public_test.zip" if split == "public_test" else "train.zip")) as archive:
            for business in report["businesses"]:
                bid = business["business_id"]
                catalog = WORKSPACE / "artifacts/cache" / bid / business["revision"][:20] / "catalog.json"
                context = json.loads(catalog.read_text(encoding="utf-8"))
                assert context["schema_complete"]
                for relative, expected in context["manifest"].items():
                    source = WORKSPACE / "data" / split / "data" / bid / relative
                    with archive.open(f"{split}/data/{bid}/{relative}") as stream:
                        original = hashlib.file_digest(stream, "sha256").hexdigest()
                    assert file_hash(source) == expected == original, source
                parse_errors = []
                for table in context["tables"]:
                    assert table["raw_row_count"] == table["row_count"] + table["duplicate_rows"] + table["excluded_rows"]
                    for col in table["columns"]:
                        if col["parse_errors"]:
                            parse_errors.append([table["name"], col["name"], col["parse_errors"]])
                        if not col["semantic_role"] and col["null_count"] < table["row_count"]:
                            unmapped.append({"split": split, "business_id": bid, "table": table["name"], "column": col["name"], "original_name": col["original_name"]})
                assert not parse_errors, (bid, parse_errors)
                assert not any(d["severity"] == "error" for d in context["diagnostics"]), bid
                details.append({"split": split, "business_id": bid, "revision": context["revision"],
                    "tables": len(context["tables"]), "rows": sum(t["row_count"] for t in context["tables"]),
                    "excluded_rows": sum(t["excluded_rows"] for t in context["tables"]),
                    "duplicate_rows": sum(t["duplicate_rows"] for t in context["tables"]),
                    "sources_checked": len(context["manifest"]), "catalog": catalog.relative_to(WORKSPACE).as_posix(),
                    "database": Path(context["database"]).relative_to(WORKSPACE).as_posix(),
                    "normalizer_snapshot": "current_1.2.1" if bid in patched else "runtime_v1.2.0",
                    "diagnostic_counts": dict(Counter(d["code"] for d in context["diagnostics"]))})
    result = {"businesses": details, "source_hash_mismatches": [], "all_row_accounting_valid": True,
        "column_parse_errors": 0, "error_diagnostics": 0, "sources_checked": sum(d["sources_checked"] for d in details),
        "unmapped_populated_columns": len(unmapped),
        "lineage": "Full public 14 + train 30 normalization with 1.2.0; only three train businesses with ambiguous currency columns reingested using 1.2.1. Earlier zero-error caches retained. Every source and column diagnostic rechecked.",
        "current_runtime_sha256": {p.relative_to(WORKSPACE).as_posix(): file_hash(p) for p in (WORKSPACE / "bizreason").rglob("*.py")}}
    atomic_json(root / "manifest.json", result)
    atomic_json(root / "unmapped_columns.json", unmapped)
    lines = ["# Dữ liệu đã chuẩn hoá", "", "Cập nhật 03/11/2026. Các file nguồn được giữ nguyên; dữ liệu chuẩn hoá nằm trong SQLite theo từng doanh nghiệp, kèm catalog và vị trí dòng gốc.", "",
        "- [Manifest và revision từng doanh nghiệp](../artifacts/normalization_v2/manifest.json).",
        "- [Các cột có dữ liệu nhưng chưa gán semantic role](../artifacts/normalization_v2/unmapped_columns.json).", "",
        "| Bộ | Doanh nghiệp | Bảng | Bản ghi giữ lại | Catalog | SQLite |", "|---|---|---:|---:|---|---|"]
    for d in details:
        lines.append(f"| {d['split']} | {d['business_id']} | {d['tables']} | {d['rows']} | [catalog](../{d['catalog']}) | [database](../{d['database']}) |")
    lines += ["", "Đã đối chiếu 252 file nguồn với ZIP, không có hash lệch; mọi bảng đạt raw = kept + excluded + duplicates. Không còn lỗi parse trong metadata cột, kể cả cột số lượng cơ bản được suy từ bảng quy đổi; không có diagnostic mức error.", "",
        "Đợt đầu đã chạy đủ 14 business public và 30 business train với 1.2.0. Sau khi phát hiện ba cột giá mơ hồ, chỉ biz_119, biz_120, biz_124 được đọc lại bằng 1.2.1; manifest ghi rõ lineage. Các cache cũ còn lỗi được giữ để truy nguyên, không dùng trong manifest này.", "",
        "Các cột chưa gán role vẫn được giữ và liệt kê riêng; không suy rằng mọi cột đã được hiểu đầy đủ. Không có parse error không phải bằng chứng mọi SQL đều đúng nghiệp vụ.", "",
        "Bảng vật lý lưu __source_row và __raw_json để truy nguyên. Executor cấp public views chỉ gồm các cột trong catalog, hỗ trợ SELECT * và tiếp tục chặn đọc trực tiếp các cột nội bộ.", ""]
    (WORKSPACE / "docs/NORMALIZED_DATA.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"businesses": len(details), "tables": sum(d['tables'] for d in details),
                      "rows": sum(d['rows'] for d in details), "sources": result['sources_checked'],
                      "column_parse_errors": 0, "error_diagnostics": 0}))


if __name__ == "__main__":
    main()
