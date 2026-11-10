"""Read-only SQLite execution in a disposable process, with a parent watchdog."""

from __future__ import annotations

import atexit
import multiprocessing
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bizreason.contracts import ExecutionResult, ExecutionStatus
from bizreason.data.readers import sqlite_readonly
from bizreason.data.parsing import ParseStatus, parse_date, parse_number
from bizreason.utils import digest, dumps, normalize_lookup, quote_identifier

ALLOWED_FUNCTIONS = {
    "abs",
    "avg",
    "cast",
    "ceil",
    "ceiling",
    "char",
    "coalesce",
    "concat",
    "concat_ws",
    "count",
    "date",
    "datetime",
    "dense_rank",
    "first_value",
    "floor",
    "format",
    "glob",
    "group_concat",
    "hex",
    "ifnull",
    "iif",
    "instr",
    "julianday",
    "lag",
    "last_value",
    "lead",
    "length",
    "like",
    "lower",
    "ltrim",
    "max",
    "min",
    "nullif",
    "printf",
    "rank",
    "replace",
    "round",
    "row_number",
    "rtrim",
    "strftime",
    "substr",
    "substring",
    "sum",
    "time",
    "total",
    "trim",
    "typeof",
    "unicode",
    "unixepoch",
    "upper",
    "decimal_sum",
    "decimal_mul",
    "decimal_div",
    "decimal_sub",
    "decimal_positive",
    "lookup",
    "iso_date",
    "money_vnd",
}


class DecimalSum:
    def __init__(self) -> None:
        self.total = Decimal(0)

    def step(self, value: Any) -> None:
        if value is not None:
            self.total += Decimal(str(value))

    def finalize(self) -> str:
        return str(self.total)


def _sql_iso_date(value: Any) -> str | None:
    if value is None:
        return None
    result = parse_date(value)
    if result.status != ParseStatus.OK:
        raise ValueError("Invalid source date")
    return result.value


def register_helpers(connection: sqlite3.Connection) -> None:
    connection.create_function("iso_date", 1, _sql_iso_date)
    def money_vnd(value, locale="unknown"):
        if locale not in {"unknown", "vi", "en"}:
            raise ValueError("Unsupported numeric locale")
        result = parse_number(value, locale=locale, unit="VND")
        if result.status == ParseStatus.EMPTY:
            return None
        if result.status != ParseStatus.OK:
            raise ValueError("Ambiguous or invalid source money")
        return str(result.value)
    connection.create_function("money_vnd", 1, money_vnd)
    connection.create_function("money_vnd", 2, money_vnd)
    connection.create_aggregate("decimal_sum", 1, DecimalSum)
    connection.create_function(
        "decimal_mul",
        2,
        lambda a, b: None if a is None or b is None else str(Decimal(str(a)) * Decimal(str(b))),
    )
    connection.create_function(
        "decimal_div",
        2,
        lambda a, b: (
            None
            if a is None or b is None or Decimal(str(b)) == 0
            else str(Decimal(str(a)) / Decimal(str(b)))
        ),
    )
    connection.create_function(
        "decimal_sub",
        2,
        lambda a, b: None if a is None or b is None else str(Decimal(str(a)) - Decimal(str(b))),
    )
    connection.create_function(
        "decimal_positive",
        1,
        lambda value: None if value is None else str(max(Decimal(str(value)), Decimal(0))),
    )
    def _sql_lookup(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        prefix = "%" if text.startswith("%") else ""
        suffix = "%" if text.endswith("%") and len(text) > 1 else ""
        core = normalize_lookup(text)
        if not core:
            return prefix or None
        return f"{prefix}{core}{suffix}"

    connection.create_function("lookup", 1, _sql_lookup)


def _run_isolated_query(
    database: str,
    sql: str,
    parameters: list[Any],
    allowed: list[str],
    timeout: float,
    max_rows: int,
    max_bytes: int = 8_000_000,
) -> ExecutionResult:
    started = time.monotonic()
    accessed: set[str] = set()
    denied: list[str] = []
    connection = None
    try:
        connection = sqlite_readonly(Path(database))
        connection.enable_load_extension(False)
        connection.execute("PRAGMA trusted_schema=OFF")
        # Read-only main database plus connection-local public projections. A
        # normal SELECT * reads public fields; explicit provenance reads remain denied.
        connection.execute("PRAGMA query_only=OFF")
        for table in allowed:
            columns = [row[1] for row in connection.execute(
                f"PRAGMA main.table_info({quote_identifier(table)})") if not row[1].startswith("__")]
            if columns:
                projection = ",".join(quote_identifier(column) for column in columns)
                connection.execute(
                    f"CREATE TEMP VIEW {quote_identifier(table)} AS SELECT {projection} FROM main.{quote_identifier(table)}"
                )
        connection.execute("PRAGMA query_only=ON")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 200_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        register_helpers(connection)

        def authorize(
            action: int,
            first: str | None,
            second: str | None,
            database_name: str | None,
            trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ:
                if second and second.startswith("__"):
                    denied.append("READ_OUTSIDE_CATALOG")
                    return sqlite3.SQLITE_DENY
                if database_name is not None and first not in allowed:
                    denied.append("READ_OUTSIDE_CATALOG")
                    return sqlite3.SQLITE_DENY
                if first in allowed:
                    accessed.add(first)
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION:
                if str(second or first).lower() in ALLOWED_FUNCTIONS:
                    return sqlite3.SQLITE_OK
            elif action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_OK
            denied.append("DISALLOWED_SQL_OPERATION")
            return sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        connection.set_progress_handler(lambda: int(time.monotonic() - started > timeout), 2000)
        cursor = connection.execute(sql, parameters)
        if cursor.description is None:
            raise sqlite3.DatabaseError("Query did not produce a result")
        columns = [column[0] for column in cursor.description]
        rows: list[list[Any]] = []
        result_bytes = 0
        status = ExecutionStatus.SUCCESS
        for row in cursor:
            cells = list(row)
            result_bytes += len(dumps(cells).encode("utf-8"))
            if len(rows) >= max_rows or result_bytes > max_bytes:
                status = ExecutionStatus.RESULT_LIMIT
                rows.clear()  # Never return a truncated result as a complete answer.
                break
            rows.append(cells)
        if not rows and status == ExecutionStatus.SUCCESS:
            status = ExecutionStatus.EMPTY_SUCCESS
        result = ExecutionResult(
            status=status,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            result_hash=digest([columns, rows]) if status != ExecutionStatus.RESULT_LIMIT else "",
            accessed_tables=sorted(accessed),
        )
    except (sqlite3.Error, ValueError, TypeError, InvalidOperation) as error:
        timeout_hit = time.monotonic() - started >= timeout
        status = (
            ExecutionStatus.POLICY_DENIED
            if denied
            else ExecutionStatus.TIMEOUT if timeout_hit else ExecutionStatus.EXECUTION_ERROR
        )
        result = ExecutionResult(
            status=status,
            error_code=denied[0] if denied else type(error).__name__,
            diagnostics=[str(error)[:500]],
            accessed_tables=sorted(accessed),
        )
    except Exception as error:
        result = ExecutionResult(
            status=ExecutionStatus.EXECUTION_ERROR, error_code=type(error).__name__
        )
    finally:
        if connection:
            connection.close()
    return result.model_copy(update={"elapsed_ms": (time.monotonic() - started) * 1000})


def _worker(
    sender: Any,
    database: str,
    sql: str,
    parameters: list[Any],
    allowed: list[str],
    timeout: float,
    max_rows: int,
    max_bytes: int,
) -> None:
    result = _run_isolated_query(
        database=database,
        sql=sql,
        parameters=parameters,
        allowed=allowed,
        timeout=timeout,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )
    try:
        sender.send(result.model_dump(mode="json"))
    finally:
        sender.close()


def _pool_worker_loop(pipe: Any) -> None:
    try:
        while True:
            try:
                msg = pipe.recv()
            except (EOFError, KeyboardInterrupt):
                break
            if msg is None:
                break
            database, sql, parameters, allowed_tables, timeout, max_rows, max_bytes = msg
            result = _run_isolated_query(
                database=database,
                sql=sql,
                parameters=parameters,
                allowed=allowed_tables,
                timeout=timeout,
                max_rows=max_rows,
                max_bytes=max_bytes,
            )
            try:
                pipe.send(result.model_dump(mode="json"))
            except (BrokenPipeError, EOFError, OSError):
                break
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


class _PoolWorker:
    def __init__(
        self, context: multiprocessing.context.BaseContext, *, start: bool = True
    ) -> None:
        self.context = context
        if not start:
            self.parent_conn: Any = None
            self.process: Any = None
            return
        self.parent_conn, child_conn = context.Pipe(duplex=True)
        self.process = context.Process(
            target=_pool_worker_loop,
            args=(child_conn,),
            daemon=True,
        )
        self.process.start()
        child_conn.close()

    def is_alive(self) -> bool:
        return bool(self.process is not None and self.process.is_alive() and self.process.exitcode is None)

    def close(self) -> None:
        try:
            if self.parent_conn is not None and self.is_alive():
                self.parent_conn.send(None)
        except Exception:
            pass
        if self.process is not None and self.is_alive():
            self.process.terminate()
            self.process.join(timeout=0.5)
            if self.is_alive():
                self.process.kill()
                self.process.join(timeout=0.5)
        try:
            if self.parent_conn is not None:
                self.parent_conn.close()
        except Exception:
            pass


class _SqliteWorkerPool:
    def __init__(self, max_workers: int = 4) -> None:
        self.max_workers = max_workers
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._idle_workers: list[_PoolWorker] = []
        self._active_workers: set[_PoolWorker] = set()
        self._context = multiprocessing.get_context("spawn")
        self._closed = False

    def _prune_dead_locked(self) -> None:
        dead_active = [w for w in self._active_workers if not w.is_alive()]
        for w in dead_active:
            self._active_workers.discard(w)
            w.close()
        still_idle: list[_PoolWorker] = []
        for w in self._idle_workers:
            if w.is_alive():
                still_idle.append(w)
            else:
                w.close()
        self._idle_workers = still_idle

    def lease_worker(self, timeout: float = 0.2) -> _PoolWorker:
        with self._cond:
            deadline = time.monotonic() + max(0.0, timeout)
            while not self._closed:
                self._prune_dead_locked()
                if len(self._active_workers) < self.max_workers:
                    while self._idle_workers:
                        worker = self._idle_workers.pop()
                        if worker.is_alive():
                            self._active_workers.add(worker)
                            return worker
                        worker.close()
                    worker = _PoolWorker(self._context, start=True)
                    self._active_workers.add(worker)
                    return worker
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _PoolWorker(self._context, start=False)
                self._cond.wait(timeout=remaining)
            raise RuntimeError("SQLite worker pool is closed")

    def return_worker(self, worker: _PoolWorker) -> None:
        with self._cond:
            self._active_workers.discard(worker)
            self._prune_dead_locked()
            if (
                self._closed
                or not worker.is_alive()
                or (len(self._active_workers) + len(self._idle_workers)) >= self.max_workers
            ):
                worker.close()
            else:
                self._idle_workers.append(worker)
            self._cond.notify_all()

    def discard_worker(self, worker: _PoolWorker) -> None:
        with self._cond:
            self._active_workers.discard(worker)
            self._cond.notify_all()
        worker.close()

    def shutdown(self) -> None:
        with self._cond:
            self._closed = True
            workers = list(self._idle_workers) + list(self._active_workers)
            self._idle_workers.clear()
            self._active_workers.clear()
            self._cond.notify_all()
        for worker in workers:
            worker.close()


_GLOBAL_POOL: _SqliteWorkerPool | None = None
_POOL_LOCK = threading.Lock()


def get_worker_pool() -> _SqliteWorkerPool:
    global _GLOBAL_POOL
    with _POOL_LOCK:
        if _GLOBAL_POOL is None or _GLOBAL_POOL._closed:
            _GLOBAL_POOL = _SqliteWorkerPool()
        return _GLOBAL_POOL


def shutdown_sql_pool() -> None:
    global _GLOBAL_POOL
    with _POOL_LOCK:
        if _GLOBAL_POOL is not None:
            _GLOBAL_POOL.shutdown()
            _GLOBAL_POOL = None


atexit.register(shutdown_sql_pool)


_INDEXED_DBS: set[str] = set()
_INDEX_LOCK = threading.Lock()


def ensure_join_indexes(db_path: Path | str, joins: Any = None) -> None:
    try:
        resolved = str(Path(db_path).resolve())
    except Exception:
        resolved = str(db_path)
    with _INDEX_LOCK:
        if resolved in _INDEXED_DBS:
            return
        _INDEXED_DBS.add(resolved)

    try:
        conn = sqlite3.connect(resolved, timeout=5.0)
        try:
            cursor = conn.cursor()
            if joins:
                for edge in joins:
                    left_tbl = getattr(edge, "left", None) or (edge.get("left") if isinstance(edge, dict) else None)
                    right_tbl = getattr(edge, "right", None) or (edge.get("right") if isinstance(edge, dict) else None)
                    keys = getattr(edge, "keys", None) or (edge.get("keys") if isinstance(edge, dict) else None) or []
                    for k in keys:
                        if len(k) >= 2:
                            l_col, r_col = k[0], k[1]
                            if left_tbl and l_col:
                                idx_name = f"idx_jn_{left_tbl}_{l_col}"[:60]
                                try:
                                    cursor.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{left_tbl}" ("{l_col}")')
                                except Exception:
                                    pass
                            if right_tbl and r_col:
                                idx_name = f"idx_jn_{right_tbl}_{r_col}"[:60]
                                try:
                                    cursor.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{right_tbl}" ("{r_col}")')
                                except Exception:
                                    pass

            tables = [
                row[0]
                for row in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for t_name in tables:
                try:
                    cnt_row = cursor.execute(f'SELECT COUNT(*) FROM "{t_name}"').fetchone()
                    row_cnt = cnt_row[0] if cnt_row else 0
                except Exception:
                    continue
                if row_cnt <= 500:
                    continue
                cols = [c[1] for c in cursor.execute(f'PRAGMA table_info("{t_name}")').fetchall()]
                for col in cols:
                    col_l = col.lower()
                    if (
                        col_l.startswith("ma_")
                        or col_l.startswith("so_")
                        or col_l.startswith("id_")
                        or col_l.endswith("_id")
                        or col_l
                        in {
                            "order_id",
                            "customer_id",
                            "product_id",
                            "bill_id",
                            "ma_giao_dich",
                            "so_chung_tu",
                            "so_hd",
                            "ma_bill",
                            "so_phieu",
                            "ma_don",
                            "ma_nhap_hang",
                            "ma_hang",
                            "ma_mat_hang",
                            "ma_sp",
                            "ma_san_pham_ban",
                            "ma_kh",
                            "so_kh",
                        }
                    ):
                        idx_name = f"idx_col_{t_name}_{col}"[:60]
                        try:
                            cursor.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{t_name}" ("{col}")')
                        except Exception:
                            pass
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def execute_sql(
    database: Path | str,
    sql: str,
    parameters: list[Any],
    allowed_tables: list[str],
    *,
    timeout: float = 15,
    max_rows: int = 10000,
    use_pool: bool = True,
) -> ExecutionResult:
    try:
        ensure_join_indexes(database)
    except Exception:
        pass
    if timeout <= 0:
        return ExecutionResult(status=ExecutionStatus.TIMEOUT, error_code="DEADLINE_EXCEEDED")
    started = time.monotonic()
    if not use_pool:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_worker,
            args=(
                sender,
                str(database),
                sql,
                parameters,
                allowed_tables,
                timeout,
                max_rows,
                8_000_000,
            ),
            daemon=True,
        )
        try:
            process.start()
            try:
                sender.close()
            except Exception:
                pass
            remaining_timeout = max(0.01, timeout - (time.monotonic() - started))
            if receiver.poll(remaining_timeout):
                try:
                    return ExecutionResult.model_validate(receiver.recv())
                except EOFError:
                    return ExecutionResult(
                        status=ExecutionStatus.EXECUTION_ERROR, error_code="WORKER_EXITED"
                    )
            return ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                error_code="PARENT_DEADLINE",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        finally:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
            receiver.close()
            try:
                sender.close()
            except Exception:
                pass

    pool = get_worker_pool()
    worker = pool.lease_worker(timeout=max(0.05, timeout))
    if not worker.is_alive():
        return ExecutionResult(
            status=ExecutionStatus.TIMEOUT,
            error_code="POOL_LEASE_TIMEOUT",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    remaining_timeout = timeout - (time.monotonic() - started)
    if remaining_timeout <= 0:
        pool.return_worker(worker)
        return ExecutionResult(
            status=ExecutionStatus.TIMEOUT,
            error_code="PARENT_DEADLINE",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    keep_worker = False
    try:
        assert worker.parent_conn is not None
        worker.parent_conn.send(
            (str(database), sql, parameters, allowed_tables, remaining_timeout, max_rows, 8_000_000)
        )
        if worker.parent_conn.poll(remaining_timeout):
            try:
                raw_result = worker.parent_conn.recv()
                result = ExecutionResult.model_validate(raw_result)
                if result.status == ExecutionStatus.TIMEOUT:
                    pool.discard_worker(worker)
                else:
                    keep_worker = True
                return result
            except (EOFError, BrokenPipeError, OSError):
                pool.discard_worker(worker)
                return ExecutionResult(
                    status=ExecutionStatus.EXECUTION_ERROR, error_code="WORKER_EXITED"
                )
        pool.discard_worker(worker)
        return ExecutionResult(
            status=ExecutionStatus.TIMEOUT,
            error_code="PARENT_DEADLINE",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    except Exception as error:
        pool.discard_worker(worker)
        return ExecutionResult(
            status=ExecutionStatus.EXECUTION_ERROR, error_code=type(error).__name__
        )
    finally:
        if keep_worker:
            pool.return_worker(worker)
