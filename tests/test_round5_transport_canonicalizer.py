from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from bizreason.config import Settings
from bizreason.contracts import BusinessContext, ColumnMeta, TableMeta
from bizreason.decision import (
    CLAIMABLE_MISSING_ROLES,
    ROLE_SYNONYMS,
    _canonical_required_role,
)
from bizreason.llm import ProviderError, TrammayClient
from bizreason.metadata_fields import canonical_metadata_fields
from bizreason.plan_input import canonicalize_plan_input
from bizreason.prompt_budget import join_reference
from bizreason.streaming import StreamingTrammayClient
from bizreason.temporal import TemporalResolver
from bizreason.triage import normalize_triage_text


# ==============================================================================
# 1. Transport Resilience & Non-Punitive Quota Tests
# ==============================================================================

def test_trammay_client_does_not_consume_quota_on_connect_error():
    """Failed connection attempts must not consume self.usage.calls."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    settings = Settings(
        api_key="mock-key",
        transport_retries=2,
        transport_backoff=0.0,
    )
    client = TrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="ConnectError"):
            client.generate_json("system", "prompt", timeout=5)
        # Usage must remain 0 when transport fails
        assert client.usage.calls == 0
    finally:
        client.close()


def test_trammay_client_does_not_consume_quota_on_exhausted_retryable_502():
    """All attempts fail with 502 -> usage.calls must remain 0."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(502, json={"error": "Bad Gateway"})

    settings = Settings(
        api_key="mock-key",
        transport_retries=2,
        transport_backoff=0.0,
    )
    client = TrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="HTTP 502"):
            client.generate_json("system", "prompt", timeout=10)
        assert call_count >= 3
        assert client.usage.calls == 0
    finally:
        client.close()


def test_streaming_client_does_not_consume_quota_on_all_connect_errors():
    """Streaming client fails all attempts -> usage.calls remains 0."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Host unreachable", request=request)

    settings = Settings(
        api_key="mock-key",
        transport_retries=1,
        transport_backoff=0.0,
    )
    client = StreamingTrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="ConnectError"):
            client.generate_json("system", "prompt", timeout=10)
        assert client.usage.calls == 0
    finally:
        client.close()


def test_streaming_client_consumes_quota_exactly_once_after_recovery():
    """Mid-stream error on attempt 1, success on attempt 2 -> calls == 1."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            def broken():
                yield b"data: " + json.dumps({"choices": [{"delta": {"content": "{\"test\":"}}]}).encode() + b"\n\n"
                raise httpx.ReadError("Broken socket", request=request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=broken())
        events = [
            {"choices": [{"delta": {"content": "{\"recovered\": true}"}, "finish_reason": "stop"}]},
            {"usage": {"total_tokens": 10}},
        ]
        body = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    settings = Settings(
        api_key="mock-key",
        transport_retries=2,
        transport_backoff=0.0,
    )
    client = StreamingTrammayClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.generate_json("system", "prompt", timeout=10)
        assert result == {"recovered": True}
        assert call_count == 2
        assert client.usage.calls == 1
    finally:
        client.close()


def test_dead_socket_pool_reset_on_real_transport():
    """Verify _reset_client_if_transport_error creates a fresh client for real network transport."""
    settings = Settings(api_key="mock-key", transport_retries=1)
    client = TrammayClient(settings)
    original_httpx_client = client._client
    assert not isinstance(getattr(original_httpx_client, "_transport", None), httpx.MockTransport)

    # Calling reset with non-transport error does nothing
    client._reset_client_if_transport_error(ValueError("Not transport"))
    assert client._client is original_httpx_client

    # Calling reset with TransportError closes and replaces
    req = httpx.Request("POST", "https://trammay.xyz/v1/responses")
    client._reset_client_if_transport_error(httpx.ConnectError("Socket closed", request=req))
    assert client._client is not original_httpx_client
    assert client._client.is_closed is False
    client.close()


def test_dead_socket_pool_reset_preserves_mock_transport():
    """MockTransport test doubles must not be destroyed by _reset_client_if_transport_error."""
    settings = Settings(api_key="mock-key", transport_retries=1)
    mock_t = httpx.MockTransport(lambda req: httpx.Response(200))
    client = TrammayClient(settings, transport=mock_t)
    original = client._client

    req = httpx.Request("POST", "https://trammay.xyz/v1/responses")
    client._reset_client_if_transport_error(httpx.RemoteProtocolError("error", request=req))
    assert client._client is original
    client.close()


# ==============================================================================
# 2. Symmetric Undirected Join Canonicalization Tests
# ==============================================================================

def test_symmetric_join_canonicalization_with_join_id(business):
    edge = business.joins[0]
    jid = join_reference(edge)

    # 1. Forward match
    plan_fwd = {
        "metric": "sales_revenue",
        "tables": [edge.left, edge.right],
        "joins": [{"join_id": jid, "left": edge.left, "right": edge.right, "keys": [list(k) for k in edge.keys]}],
    }
    c_fwd = canonicalize_plan_input(plan_fwd, business)
    assert c_fwd["joins"][0]["left"] == edge.left
    assert c_fwd["joins"][0]["right"] == edge.right

    # 2. Reversed match (exact reversed tables and reversed keys)
    rev_keys = [[k[1], k[0]] for k in edge.keys]
    plan_rev = {
        "metric": "sales_revenue",
        "tables": [edge.left, edge.right],
        "joins": [{"join_id": jid, "left": edge.right, "right": edge.left, "keys": rev_keys}],
    }
    c_rev = canonicalize_plan_input(plan_rev, business)
    # Must resolve cleanly to canonical edge
    assert c_rev["joins"][0]["left"] == edge.left
    assert c_rev["joins"][0]["right"] == edge.right
    assert [list(k) for k in c_rev["joins"][0]["keys"]] == [list(k) for k in edge.keys]

    # 3. Conflicting left table (e.g. invented_table) STILL raises ValueError
    plan_conflict = {
        "metric": "sales_revenue",
        "tables": [edge.left, edge.right],
        "joins": [{"join_id": jid, "left": "invented_table"}],
    }
    with pytest.raises(ValueError, match="Conflicting join reference"):
        canonicalize_plan_input(plan_conflict, business)

    # 4. Conflicting keys in reversed match STILL raises ValueError
    plan_bad_rev_keys = {
        "metric": "sales_revenue",
        "tables": [edge.left, edge.right],
        "joins": [{"join_id": jid, "left": edge.right, "right": edge.left, "keys": [["wrong", "key"]]}],
    }
    with pytest.raises(ValueError, match="Conflicting join reference"):
        canonicalize_plan_input(plan_bad_rev_keys, business)


def test_symmetric_join_canonicalization_without_join_id(business):
    edge = business.joins[0]
    rev_keys = [[k[1], k[0]] for k in edge.keys]

    # Reversed supplied fields without join_id resolves to canonical edge
    plan_no_id = {
        "metric": "sales_revenue",
        "tables": [edge.left, edge.right],
        "joins": [{"left": edge.right, "right": edge.left, "keys": rev_keys}],
    }
    res = canonicalize_plan_input(plan_no_id, business)
    assert res["joins"][0]["left"] == edge.left
    assert res["joins"][0]["right"] == edge.right


def test_join_tolerance_with_executable_sql(business):
    edge = business.joins[0]

    # 1. Unknown join_id with executable SQL and template_eligible=False: does not raise ValueError
    plan_hallucinated_jid = {
        "metric": "generic_sql",
        "tables": [edge.left, edge.right],
        "sql": f"SELECT * FROM {edge.left} JOIN {edge.right} ON {edge.left}.{edge.keys[0][0]} = {edge.right}.{edge.keys[0][1]}",
        "joins": [{"join_id": "join_1"}],
        "template_eligible": False,
    }
    res = canonicalize_plan_input(plan_hallucinated_jid, business)
    # Hallucinated metadata join without valid endpoints is dropped
    assert len(res["joins"]) == 0

    # 2. Unknown join_id but valid left/right/keys matching context.joins: resolves to canonical edge
    plan_hallucinated_jid_with_keys = {
        "metric": "generic_sql",
        "tables": [edge.left, edge.right],
        "sql": f"SELECT * FROM {edge.left} JOIN {edge.right} ON {edge.left}.{edge.keys[0][0]} = {edge.right}.{edge.keys[0][1]}",
        "joins": [{"join_id": "join_hallucinated", "left": edge.left, "right": edge.right, "keys": edge.keys}],
        "template_eligible": False,
    }
    res2 = canonicalize_plan_input(plan_hallucinated_jid_with_keys, business)
    assert len(res2["joins"]) == 1
    assert res2["joins"][0]["left"] == edge.left
    assert res2["joins"][0]["right"] == edge.right

    # 3. Conflicting fields with executable SQL: dropped instead of raising ValueError
    jid = join_reference(edge)
    plan_conflict_sql = {
        "metric": "generic_sql",
        "tables": [edge.left, edge.right],
        "sql": "SELECT 1",
        "joins": [{"join_id": jid, "left": "invented_table"}],
        "template_eligible": False,
    }
    res3 = canonicalize_plan_input(plan_conflict_sql, business)
    assert len(res3["joins"]) == 0

    # 4. Unknown join_id when template_eligible=True: STILL raises ValueError
    plan_template_unknown = {
        "metric": "sales_revenue",
        "tables": [edge.left, edge.right],
        "sql": "SELECT 1",
        "joins": [{"join_id": "join_1"}],
        "template_eligible": True,
    }
    with pytest.raises(ValueError, match="Unknown join reference"):
        canonicalize_plan_input(plan_template_unknown, business)


# ==============================================================================
# 3. Vietnamese Status Value Canonicalization Tests
# ==============================================================================

def test_canonicalize_vietnamese_status_filter_values(business):
    # lines table has column 'status' with semantic_role 'status'
    tbl = business.tables[0]
    status_col = tbl.role("status") or "status"

    plan = {
        "metric": "sales_revenue",
        "tables": [tbl.name],
        "filters": [
            {
                "field": f"{tbl.name}.{status_col}",
                "operator": "in",
                "values": ["hoàn thành", "đã hủy", "đang giao", "đã trả"],
            }
        ],
    }
    res = canonicalize_plan_input(plan, business)
    vals = res["filters"][0]["values"]
    assert vals == ["completed", "cancelled", "shipping", "returned"]


def test_prune_non_canonical_status_filter_on_generic_sql(business):
    tbl = business.tables[0]
    status_col = tbl.role("status") or "status"

    # generic_sql with decorative non-canonical status filter (e.g. unknown status value)
    plan = {
        "metric": "generic_sql",
        "sql": f'SELECT 1 FROM "{tbl.name}"',
        "tables": [tbl.name],
        "filters": [
            {
                "field": f"{tbl.name}.{status_col}",
                "operator": "eq",
                "values": ["trang_thai_la_hoan_toan_khong_hop_le"],
            }
        ],
    }
    res = canonicalize_plan_input(plan, business)
    assert len(res["filters"]) == 0
    assert "pruned_non_canonical_decorative_status_filter" in res.get("derivation_notes", [])


def test_keep_canonical_status_filter_on_generic_sql(business):
    tbl = business.tables[0]
    status_col = tbl.role("status") or "status"

    plan = {
        "metric": "generic_sql",
        "sql": f'SELECT 1 FROM "{tbl.name}"',
        "tables": [tbl.name],
        "filters": [
            {
                "field": f"{tbl.name}.{status_col}",
                "operator": "eq",
                "values": ["hoàn tất"],  # auto-canonicalizes to "completed"
            }
        ],
    }
    res = canonicalize_plan_input(plan, business)
    assert len(res["filters"]) == 1
    assert res["filters"][0]["values"] == ["completed"]
    assert "pruned_non_canonical_decorative_status_filter" not in res.get("derivation_notes", [])


# ==============================================================================
# 4. Colloquial Yesterday Shorthands Tests
# ==============================================================================

def test_yesterday_colloquial_temporal_resolver():
    resolver = TemporalResolver()
    as_of = date(2026, 3, 10)
    expected_start = date(2026, 3, 9)
    expected_end = date(2026, 3, 10)

    # hqua
    wins_hqua = resolver.resolve("Doanh thu hqua là bao nhiêu?", as_of)
    assert len(wins_hqua) == 1
    assert wins_hqua[0].start == expected_start and wins_hqua[0].end_exclusive == expected_end

    # hom qa
    wins_hom_qa = resolver.resolve("Doanh thu hom qa", as_of)
    assert len(wins_hom_qa) == 1
    assert wins_hom_qa[0].start == expected_start and wins_hom_qa[0].end_exclusive == expected_end

    # bua qua
    wins_bua_qua = resolver.resolve("Số đơn bữa qua", as_of)
    assert len(wins_bua_qua) == 1
    assert wins_bua_qua[0].start == expected_start and wins_bua_qua[0].end_exclusive == expected_end

    # Standalone "qua" must NOT be treated as yesterday
    # "qua Zalo" -> no yesterday window
    wins_zalo = resolver.resolve("Doanh thu bán qua Zalo", as_of)
    assert all(w.start != expected_start for w in wins_zalo)


def test_yesterday_triage_normalization():
    assert "hom qua" in normalize_triage_text("Doanh thu hqua")
    assert "hom qua" in normalize_triage_text("Doanh thu hom qa")
    assert "hom qua" in normalize_triage_text("Doanh thu bữa qua")
    # Standalone "qua" remains "qua"
    assert "qua zalo" in normalize_triage_text("Doanh thu bán qua Zalo")


# ==============================================================================
# 5. Missing Roles & Synonyms Expansion Tests
# ==============================================================================

def test_missing_roles_and_synonyms():
    # 1. CLAIMABLE_MISSING_ROLES contains customer_feedback and dosage_instruction
    assert "customer_feedback" in CLAIMABLE_MISSING_ROLES
    assert "dosage_instruction" in CLAIMABLE_MISSING_ROLES

    # 2. ROLE_SYNONYMS mapping
    assert ROLE_SYNONYMS["import_quantity"] == "stock"
    assert ROLE_SYNONYMS["received_quantity"] == "stock"
    assert ROLE_SYNONYMS["qty_import"] == "stock"

    assert ROLE_SYNONYMS["purchase_price"] == "cost_sale"
    assert ROLE_SYNONYMS["import_price"] == "cost_sale"
    assert ROLE_SYNONYMS["don_gia_nhap"] == "cost_sale"

    assert ROLE_SYNONYMS["customer_feedback"] == "customer_feedback"
    assert ROLE_SYNONYMS["employee_attitude"] == "customer_feedback"
    assert ROLE_SYNONYMS["service_attitude"] == "customer_feedback"
    assert ROLE_SYNONYMS["review"] == "customer_feedback"
    assert ROLE_SYNONYMS["rating"] == "customer_feedback"

    assert ROLE_SYNONYMS["dosage"] == "dosage_instruction"
    assert ROLE_SYNONYMS["dosage_per_intake"] == "dosage_instruction"
    assert ROLE_SYNONYMS["dosage_instruction"] == "dosage_instruction"
    assert ROLE_SYNONYMS["usage_instruction"] == "dosage_instruction"

    # 3. _canonical_required_role token fallback
    assert _canonical_required_role("employee attitude") == "customer_feedback"
    assert _canonical_required_role("review and feedback") == "customer_feedback"
    assert _canonical_required_role("product rating") == "customer_feedback"
    assert _canonical_required_role("thái độ phục vụ") == "customer_feedback"

    assert _canonical_required_role("dosage per day") == "dosage_instruction"
    assert _canonical_required_role("posology guidance") == "dosage_instruction"
    assert _canonical_required_role("lieu dung hang ngay") == "dosage_instruction"
    assert _canonical_required_role("hướng dẫn liều dùng") == "dosage_instruction"


# ==============================================================================
# 6. SQL Status Literals & Bound Parameters Canonicalization Tests
# ==============================================================================

def test_sql_status_literals_and_parameters_synchronization():
    ctx = BusinessContext(
        business_id="test_biz",
        revision="1",
        database=Path("."),
        readme="",
        tables=[
            TableMeta(
                name="orders",
                sources=[],
                columns=[
                    ColumnMeta(name="order_id", original_name="Mã đơn", dtype="text", semantic_role="order_id"),
                    ColumnMeta(name="trang_thai", original_name="Trạng thái", dtype="text", semantic_role="status"),
                    ColumnMeta(name="customer_id", original_name="Mã khách", dtype="text", semantic_role="customer_id"),
                ],
                row_count=10,
                raw_row_count=10,
            )
        ],
        manifest={},
    )

    # 1. WHERE lookup(o.trang_thai) NOT IN (lookup('huỷ'), lookup('trả hàng'))
    # -> rewritten to 'cancelled', 'returned'
    resp1 = {
        "metric": "generic_sql",
        "tables": ["orders"],
        "sql": "SELECT COUNT(*) FROM orders o WHERE lookup(o.trang_thai) NOT IN (lookup('huỷ'), lookup('trả hàng'))",
    }
    canon1 = canonical_metadata_fields(resp1, ctx)
    assert "lookup('cancelled')" in canon1["sql"]
    assert "lookup('returned')" in canon1["sql"]
    assert "huỷ" not in canon1["sql"]
    assert "trả hàng" not in canon1["sql"]
    assert "canonicalized_sql_status_literals" in canon1.get("derivation_notes", [])

    # 2. COUNT(DISTINCT CASE WHEN lookup(trang_thai) = lookup('đã hủy') THEN ma_don END)
    # -> rewritten to 'cancelled'
    resp2 = {
        "metric": "generic_sql",
        "tables": ["orders"],
        "sql": "SELECT COUNT(DISTINCT CASE WHEN lookup(trang_thai) = lookup('đã hủy') THEN order_id END) FROM orders",
    }
    canon2 = canonical_metadata_fields(resp2, ctx)
    assert "lookup('cancelled')" in canon2["sql"]
    assert "đã hủy" not in canon2["sql"]
    assert "canonicalized_sql_status_literals" in canon2.get("derivation_notes", [])

    # 3. WHERE lookup(trang_thai) = lookup(?) with parameters=["Huỷ"]
    # -> parameters=["cancelled"] while leaving non-status ? parameters untouched!
    resp3 = {
        "metric": "generic_sql",
        "tables": ["orders"],
        "sql": "SELECT * FROM orders WHERE customer_id = ? AND lookup(trang_thai) = lookup(?)",
        "parameters": ["CUST123", "Huỷ"],
    }
    canon3 = canonical_metadata_fields(resp3, ctx)
    assert canon3["parameters"] == ["CUST123", "cancelled"]
    assert "canonicalized_sql_status_literals" in canon3.get("derivation_notes", [])
