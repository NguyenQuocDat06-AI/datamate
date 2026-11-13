"""SqlFallback: two agreeing generations answer; anything else abstains. Fake model, synthetic in-memory schema."""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from typing import Any

import pytest

from bizreason.contracts import AnswerType
from bizreason.semantic import sql_fallback
from bizreason.semantic.sql_fallback import FallbackResult, SqlFallback

AS_OF = date(2026, 3, 1)


class FakeModel:
    def __init__(self, *replies: Any):
        self.replies = list(replies)
        self.calls: list[tuple[str, str, float]] = []

    def generate_json(self, system: str, prompt: str, *, timeout: float) -> dict[str, Any]:
        self.calls.append((system, prompt, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(sql: str, answer_type: str = "number", definition: str = "a definition") -> dict[str, Any]:
    return {"sql": sql, "answer_type": answer_type, "definition": definition}


@pytest.fixture
def db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(
        """
        CREATE TABLE orders (order_id TEXT, date TEXT, status TEXT, channel TEXT, revenue REAL, customer_key TEXT);
        CREATE TABLE lines (order_id TEXT, date TEXT, status TEXT, product_name TEXT, unit TEXT, qty REAL,
                            amount REAL);
        INSERT INTO orders VALUES
          ('o1', '2026-01-05', 'completed', 'retail', 1000.4, 'c1'),
          ('o2', '2026-01-06', 'shipping', 'online', 2000.2, 'c2'),
          ('o3', '2026-01-07', 'cancelled', 'retail', 500, 'c1'),
          ('o4', '2026-02-01', 'returned', 'online', 300, NULL),
          ('o5', '2026-02-03', 'completed', 'retail', 1200, 'c3');
        INSERT INTO lines VALUES
          ('o1', '2026-01-05', 'completed', 'bolt', 'box', 2, 1000.4),
          ('o2', '2026-01-06', 'shipping', 'nut', 'box', 1, 2000.2),
          ('o3', '2026-01-07', 'cancelled', 'bolt', 'box', 1, 500),
          ('o5', '2026-02-03', 'completed', 'washer', 'pack', 3, 1200);
        """
    )
    yield con
    con.close()


def fallback(model: FakeModel, **kwargs: Any) -> SqlFallback:
    return SqlFallback(model, "Walk-in orders have no customer.", **kwargs)


def orders_rows(con: sqlite3.Connection) -> list[tuple]:
    return con.execute("SELECT * FROM orders ORDER BY order_id").fetchall()


# Agreement and conversion

def test_agreeing_whole_number_is_answered_as_integer(db):
    sql_a = "SELECT ROUND(SUM(amount)) FROM lines WHERE status IN ('completed', 'shipping')"
    sql_b = "SELECT CAST(ROUND(SUM(amount)) AS INTEGER) FROM lines WHERE status IN ('completed', 'shipping')"
    model = FakeModel(reply(sql_a), reply(sql_b))
    result = fallback(model).answer("total of sales", db, AS_OF)
    assert isinstance(result, FallbackResult)
    assert result.type is AnswerType.NUMBER
    assert result.answer == 4201 and isinstance(result.answer, int)
    assert result.sql == sql_a
    assert len(model.calls) == 2


def test_agreeing_percent_is_rounded_to_two_decimals(db):
    model = FakeModel(
        reply("SELECT 100.0 * COUNT(*) / 7 FROM orders", "percent"),
        reply("SELECT ROUND(100.0 * COUNT(*) / 7, 2) FROM orders", "percent"),
    )
    result = fallback(model).answer("share of orders", db, AS_OF)
    assert result is not None and result.type is AnswerType.PERCENT
    assert result.answer == 71.43


def test_string_date_boolean_and_list_answers(db):
    cases = [
        ("SELECT channel FROM orders WHERE order_id = 'o1'", "string", "retail"),
        ("SELECT MAX(date) FROM orders", "date", "2026-02-03"),
        ("SELECT COUNT(*) > 0 FROM orders WHERE status = 'returned'", "boolean", True),
        ("SELECT DISTINCT channel FROM orders ORDER BY channel", "list", ["online", "retail"]),
    ]
    for sql, answer_type, expected in cases:
        model = FakeModel(reply(sql, answer_type), reply(sql, answer_type))
        result = fallback(model).answer("question", db, AS_OF)
        assert result is not None, sql
        assert result.type == AnswerType(answer_type)
        assert result.answer == expected


def test_table_answer_has_columns_and_rows(db):
    sql_a = ("SELECT channel, COUNT(*) AS n, ROUND(SUM(revenue)) AS total FROM orders "
             "GROUP BY channel ORDER BY channel")
    sql_b = ("SELECT channel, COUNT(*), CAST(ROUND(SUM(revenue)) AS INTEGER) FROM orders "
             "GROUP BY channel ORDER BY channel")
    model = FakeModel(reply(sql_a, "table"), reply(sql_b, "table"))
    result = fallback(model).answer("by channel", db, AS_OF)
    assert result is not None and result.type is AnswerType.TABLE
    assert result.answer == {
        "columns": ["channel", "n", "total"],
        "rows": [["online", 2, 2300], ["retail", 3, 2700]],
    }


# Disagreement, failures and abstention

def test_disagreeing_results_abstain(db):
    model = FakeModel(reply("SELECT COUNT(*) FROM orders"),
                      reply("SELECT COUNT(*) FROM orders WHERE status = 'completed'"))
    engine = fallback(model)
    assert engine.answer("how many orders", db, AS_OF) is None
    assert "different results" in engine.last_reason


def test_disagreeing_answer_types_abstain(db):
    model = FakeModel(reply("SELECT COUNT(*) FROM orders", "number"),
                      reply("SELECT COUNT(*) FROM orders", "string"))
    assert fallback(model).answer("how many orders", db, AS_OF) is None


def test_one_query_failing_abstains(db):
    model = FakeModel(reply("SELECT COUNT(*) FROM orders"), reply("SELECT FROM"))
    engine = fallback(model)
    assert engine.answer("how many orders", db, AS_OF) is None
    assert engine.last_reason.startswith("B:")


def test_failed_first_query_skips_second_generation(db):
    model = FakeModel(reply("SELECT * FROM no_such_table"), reply("SELECT 1"))
    assert fallback(model).answer("question", db, AS_OF) is None
    assert len(model.calls) == 1


def test_non_whole_number_abstains(db):
    sql = "SELECT SUM(amount) FROM lines"
    model = FakeModel(reply(sql), reply(sql))
    engine = fallback(model)
    assert engine.answer("total", db, AS_OF) is None
    assert "whole" in engine.last_reason


def test_null_and_empty_results_abstain(db):
    null_sql = "SELECT MAX(customer_key) FROM orders WHERE status = 'nothing'"
    empty_sql = "SELECT order_id FROM orders WHERE 0"
    assert fallback(FakeModel(reply(null_sql), reply(null_sql))).answer("q", db, AS_OF) is None
    assert fallback(FakeModel(reply(empty_sql, "list"), reply(empty_sql, "list"))).answer("q", db, AS_OF) is None


def test_too_many_rows_abstain(db):
    sql = "SELECT order_id FROM orders"
    model = FakeModel(reply(sql, "list"), reply(sql, "list"))
    engine = fallback(model, max_rows=3)
    assert engine.answer("list orders", db, AS_OF) is None
    assert engine.last_reason.endswith("too many rows")


def test_exactly_max_rows_is_allowed(db):
    sql = "SELECT order_id FROM orders"
    result = fallback(FakeModel(reply(sql, "list"), reply(sql, "list")), max_rows=5).answer("q", db, AS_OF)
    assert result is not None and result.answer == ["o1", "o2", "o3", "o4", "o5"]


def test_model_error_and_bad_reply_abstain(db):
    assert fallback(FakeModel(RuntimeError("down"))).answer("q", db, AS_OF) is None
    assert fallback(FakeModel("not a dict")).answer("q", db, AS_OF) is None
    assert fallback(FakeModel(reply("SELECT 1", "abstain"))).answer("q", db, AS_OF) is None
    assert fallback(FakeModel({"sql": "SELECT 1"})).answer("q", db, AS_OF) is None


def test_model_declining_to_answer_abstains(db):
    declined = {"sql": "", "answer_type": "", "definition": "no such measure"}
    assert fallback(FakeModel(declined)).answer("q", db, AS_OF) is None


# Read-only execution

@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "DROP TABLE orders",
    "UPDATE orders SET status = 'x'",
    "INSERT INTO orders VALUES ('o9', '2026-01-01', 'completed', 'retail', 1, 'c9')",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA writable_schema = ON",
    "SELECT 1; DELETE FROM orders",
    "WITH x AS (SELECT 1) DELETE FROM orders",
])
def test_write_statements_are_refused_and_db_unchanged(db, sql):
    before = orders_rows(db)
    model = FakeModel(reply(sql), reply(sql))
    engine = fallback(model)
    assert engine.answer("clear everything", db, AS_OF) is None
    assert orders_rows(db) == before
    assert len(model.calls) == 1  # the first reply is refused before anything runs


def test_authorizer_denies_writes_even_when_reached_directly(db):
    engine = fallback(FakeModel())
    before = orders_rows(db)
    with pytest.raises(Exception):
        engine._run(db, "DELETE FROM orders")
    assert orders_rows(db) == before
    # The handlers are restored: the connection works normally afterwards.
    db.execute("CREATE TABLE scratch (x)")
    db.execute("INSERT INTO scratch VALUES (1)")
    assert db.execute("SELECT COUNT(*) FROM scratch").fetchone()[0] == 1


def test_slow_recursive_query_is_aborted_by_time_limit(db):
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
    model = FakeModel(reply(slow), reply(slow))
    engine = fallback(model, time_limit_s=0.2)
    started = time.monotonic()
    assert engine.answer("count forever", db, AS_OF) is None
    assert time.monotonic() - started < 5.0
    assert "time limit" in engine.last_reason
    # The progress handler was cleared: an ordinary query still runs.
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 5


def test_handlers_are_cleared_after_a_successful_answer(db):
    sql = "SELECT COUNT(*) FROM orders"
    assert fallback(FakeModel(reply(sql), reply(sql))).answer("q", db, AS_OF) is not None
    db.execute("CREATE TABLE after_answer (x)")  # would be denied if the authorizer stayed set


def test_bad_constructor_arguments_are_rejected():
    with pytest.raises(ValueError):
        SqlFallback(FakeModel(), None, max_rows=0)
    with pytest.raises(ValueError):
        SqlFallback(FakeModel(), None, time_limit_s=0)


# Prompts

def test_prompt_carries_schema_samples_dates_status_meaning_and_rulebook(db):
    db.execute("CREATE TABLE custom_extra (alpha, beta)")
    sql = "SELECT COUNT(*) FROM orders"
    model = FakeModel(reply(sql), reply(sql))
    fallback(model).answer("how many orders", db, AS_OF)
    direct_system, direct_prompt, timeout = model.calls[0]
    definition_system, definition_prompt, _ = model.calls[1]
    assert direct_system != definition_system
    assert timeout == sql_fallback.MODEL_TIMEOUT_S
    assert "how many orders" in direct_prompt and "2026-03-01" in direct_prompt
    assert "Values: cancelled | completed | returned | shipping" in direct_prompt
    assert "orders.date ranges from 2026-01-05 to 2026-02-03." in direct_prompt
    assert "Valid sale statuses (orders that count as sales and revenue): completed, shipping." in direct_prompt
    assert "Walk-in orders have no customer." in direct_prompt
    assert "custom_extra (not described): columns alpha, beta" in direct_prompt
    assert "definition" in definition_system and definition_prompt.index("Question:") > definition_prompt.index(
        "Rulebook")


def test_prompt_states_no_rulebook_when_none_given(db):
    sql = "SELECT COUNT(*) FROM orders"
    model = FakeModel(reply(sql), reply(sql))
    SqlFallback(model, None).answer("q", db, AS_OF)
    assert "(no rulebook was given)" in model.calls[0][1]


def test_long_rulebook_is_truncated(db):
    long_rulebook = "\n".join(f"| walk-in note {i} | value {i} |" for i in range(2000))
    sql = "SELECT COUNT(*) FROM orders"
    model = FakeModel(reply(sql), reply(sql))
    SqlFallback(model, long_rulebook).answer("walk-in orders", db, AS_OF)
    assert len(model.calls[0][1]) < sql_fallback.RULEBOOK_CHARS + 6000
    assert "rulebook excerpt truncated" in model.calls[0][1]


def test_schema_text_without_known_tables_lists_unknown_ones_by_name():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE widgets (sku, colour)")
    text = sql_fallback.schema_text(con)
    assert "widgets (not described): columns sku, colour" in text
    assert "orders" not in text
    con.close()


def test_solver_uses_fallback_only_for_inexpressible_questions(tmp_path):
    import sqlite3 as _sqlite3
    from datetime import date as _date
    from bizreason.contracts import AnswerType, Question
    from bizreason.semantic import solver as solver_module

    db = tmp_path / "shop.sqlite"
    con = _sqlite3.connect(db)
    con.execute("CREATE TABLE orders (order_id TEXT, date TEXT)")
    con.commit()
    con.close()
    ro = _sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)

    class Result:
        type, answer = AnswerType.NUMBER, 7

    class Fallback:
        last_reason = "stub"
        def answer(self, question, connection, as_of):
            assert connection is not ro
            return Result()

    solver = solver_module.SemanticSolver(None, None, ro)
    q = Question(id="q1", business_id="b", question="x", as_of_date=_date(2026, 1, 1))
    trace = solver_module.SolveTrace(question_id="q1", intent=None, windows=[], decision="abstain", reason="")
    try:
        solver_module.set_fallback(Fallback())
        pred, tr = solver._finish(q, trace, "abstain", "measure commission")
        assert pred.answer == 7 and tr.decision == "value"
        pred, tr = solver._finish(q, trace, "abstain", "unresolved product: x")
        assert pred.type == AnswerType.ABSTAIN
    finally:
        solver_module.set_fallback(None)
