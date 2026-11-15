"""A payment must record more than a free-text method or export label."""
from test_semantic_layer import _context, _record, _source, _query
from bizreason.semantic import layer


def test_method_only_metadata_is_not_a_payment_but_incomplete_events_survive(tmp_path):
    columns = ["ref", "day", "value", "channel"]
    specs = {"receipts": (columns, [
        (None, None, None, "Accumulated"),
        (None, None, None, "cash"),
        (None, None, "0", "cash"),
        (None, None, "20", "transfer"),
        ("R1", None, None, "cash"),
        (None, "2024-08-01", None, "cash"),
    ])}
    records = [_record(columns, "payment", {
        "ref": "order_id", "day": "paid_at", "value": "paid_amount", "channel": "payment_method",
    })]
    source = _source(tmp_path, specs)
    out = tmp_path / "layer.sqlite"
    report = layer.build(_context(source, specs), records, out)
    assert report["payments"] == 4
    assert _query(out, "SELECT COUNT(*), SUM(amount) FROM payments") == [(4, 20)]
    assert _query(out, "SELECT COUNT(*) FROM payments WHERE amount=0") == [(1,)]
    assert _query(out, "SELECT COUNT(*) FROM payments WHERE order_id='R1'") == [(1,)]
    # Input rows remain recoverable; the canonical event table is a filtered view of them.
    assert _query(source, "SELECT COUNT(*) FROM receipts") == [(6,)]
