"""Behavioral contracts for bounded SQL source reads."""

import sqlite3

import pytest

from issue_agent.adapters.sql_read_budget import (
    BudgetedSqlConnection,
    MAX_SQL_READ_BYTES,
    SqlReadBudgetExceeded,
    bounded_sql_source_reads,
)


def _db() -> BudgetedSqlConnection:
    db = sqlite3.connect(":memory:", factory=BudgetedSqlConnection)
    db.execute("CREATE TABLE source (payload, n)")
    db.execute("INSERT INTO source VALUES (?, ?)", (b"abc", 7))
    db.execute("INSERT INTO source VALUES (?, ?)", (b"de", 9))
    db.commit()
    db.execute("BEGIN")
    return db


def test_cumulative_bytes_charge_before_cursor_is_returned_and_nested_reads_charge_each_scope():
    db = _db()
    with bounded_sql_source_reads(db, 100) as outer:
        first = db.execute("SELECT payload FROM source WHERE n=7")
        assert outer.source_bytes == 3
        with bounded_sql_source_reads(db, 100) as inner:
            second = db.execute("SELECT payload FROM source WHERE n=9")
            assert inner.source_bytes == 2
            assert outer.source_bytes == 5
            assert second.fetchone()[0] == b"de"
        assert first.fetchone()[0] == b"abc"
        assert outer.source_bytes == 5
    db.close()


def test_repeated_reads_are_charged_again_and_exact_boundary_is_inclusive():
    db = _db()
    with bounded_sql_source_reads(db, 5) as budget:
        db.execute("SELECT payload FROM source").fetchall()
        assert budget.source_bytes == 5
        with pytest.raises(SqlReadBudgetExceeded):
            db.execute("SELECT payload FROM source WHERE n=7").fetchall()
        assert budget.source_bytes == 5
    with bounded_sql_source_reads(db, 5) as budget:
        db.execute("SELECT payload FROM source").fetchall()
        assert budget.source_bytes == 5
    db.close()


def test_one_byte_short_refuses_before_cursor_and_does_not_charge_failed_read():
    db = _db()
    with bounded_sql_source_reads(db, 4) as budget:
        cursor = db.execute("SELECT payload FROM source WHERE n=7")
        assert cursor.fetchone()[0] == b"abc"
        assert budget.source_bytes == 3
        with pytest.raises(SqlReadBudgetExceeded):
            db.execute("SELECT payload FROM source WHERE n=9")
        assert budget.source_bytes == 3
    db.close()


def test_last_row_oversize_and_row_ceiling_refuse_without_partial_charge():
    db = _db()
    with bounded_sql_source_reads(db, 4) as budget:
        with pytest.raises(SqlReadBudgetExceeded):
            db.execute("SELECT payload FROM source ORDER BY n")
        assert budget.source_bytes == 0
    db.close()


def test_result_row_limit_refuses_after_ceiling_without_partial_charge():
    db = _db()
    sql = (
        "WITH RECURSIVE valueset(value) AS (VALUES (1) "
        "UNION ALL SELECT value+1 FROM valueset WHERE value<=1000000) "
        "SELECT NULL FROM valueset"
    )
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, MAX_SQL_READ_BYTES) as budget:
            db.execute(sql)
            assert budget.rows == 0
    db.close()


def test_value_larger_than_sqlite_length_limit_is_refused_without_charge():
    db = _db()
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 2**22) as budget:
            db.execute("SELECT zeroblob(?)", (2**21 + 1,))
            assert budget.source_bytes == 0
    db.close()


def test_value_limit_is_applied_and_caller_limit_is_restored_on_success_and_failure():
    db = _db()
    old = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
    try:
        with bounded_sql_source_reads(db, 10) as budget:
            assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) <= 2**21
            db.execute("SELECT payload FROM source WHERE n=7").fetchall()
            assert budget.source_bytes == 3
        assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == 4096
        with pytest.raises(SqlReadBudgetExceeded):
            with bounded_sql_source_reads(db, 2):
                db.execute("SELECT payload FROM source WHERE n=7")
        assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == 4096
    finally:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old)
        db.close()


def test_exception_rolls_back_transaction_and_restores_budget_state_and_limit():
    db = _db()
    old = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    with pytest.raises(RuntimeError):
        with bounded_sql_source_reads(db, 10):
            db.execute("SELECT payload FROM source").fetchall()
            raise RuntimeError("abort caller transaction")
    assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == old
    db.rollback()
    assert not db.in_transaction
    assert db.cursor().execute("SELECT 1").fetchone() == (1,)
    db.close()


def test_raw_cursor_and_script_routes_are_refused_inside_scope_but_work_outside():
    db = _db()
    with bounded_sql_source_reads(db, 10):
        with pytest.raises(SqlReadBudgetExceeded):
            db.cursor()
        with pytest.raises(SqlReadBudgetExceeded):
            db.executescript("SELECT 1;")
    cursor = db.cursor()
    assert cursor.execute("SELECT 1").fetchone() == (1,)
    db.executescript("CREATE TABLE other (x); INSERT INTO other VALUES (2);")
    assert db.execute("SELECT x FROM other").fetchone() == (2,)
    db.close()


def test_sql_aliases_duplicate_columns_joins_subqueries_and_aggregates_are_counted():
    db = _db()
    sql = (
        "SELECT a.payload AS same, b.payload AS same, "
        "(SELECT payload FROM source WHERE n=9) AS nested "
        "FROM source a JOIN source b ON a.n=7 AND b.n=9"
    )
    with bounded_sql_source_reads(db, 20) as budget:
        row = db.execute(sql).fetchone()
        assert row == (b"abc", b"de", b"de")
        # Returned cells, including repeated nested values, are the charged unit.
        assert budget.source_bytes == 7
    with bounded_sql_source_reads(db, 40) as budget:
        assert db.execute("SELECT sum(n), count(*) FROM source").fetchone() == (16, 2)
        assert budget.source_bytes == 16
    db.close()


def test_blob_bytes_are_counted_and_invalid_utf8_text_is_charged_before_decode_failure():
    db = _db()
    with bounded_sql_source_reads(db, 2) as budget:
        assert db.execute("SELECT x'80ff'").fetchone()[0] == b"\x80\xff"
        assert budget.source_bytes == 2
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 2) as budget:
            cursor = db.execute("SELECT CAST(x'80ff' AS TEXT)")
            assert budget.source_bytes == 2
            cursor.fetchone()
    db.close()


def test_null_integer_real_and_text_use_sqlite_octet_sizes():
    db = _db()
    with bounded_sql_source_reads(db, 18) as budget:
        row = db.execute("SELECT NULL, 2, 3.5, 'é'").fetchone()
        assert row == (None, 2, 3.5, "é")
        assert budget.source_bytes == 18  # NULL=0, numeric=8 each, UTF-8 text=2.
    db.close()


@pytest.mark.parametrize("sql", [
    "/* lead comment */ SELECT payload FROM source",
    "-- lead comment\nSELECT payload FROM source",
    "VALUES (zeroblob(32))",
    "PRAGMA table_info(source)",
])
def test_noncanonical_read_forms_cannot_bypass_preflight(sql):
    db = _db()
    with bounded_sql_source_reads(db, 1):
        with pytest.raises(SqlReadBudgetExceeded):
            db.execute(sql).fetchall()
    db.close()


def test_insert_select_cannot_bypass_scope():
    db = _db()
    db.execute("CREATE TABLE sink (payload)")
    with bounded_sql_source_reads(db, 4) as budget:
        with pytest.raises(SqlReadBudgetExceeded):
            db.execute("INSERT INTO sink SELECT payload FROM source")
        assert budget.source_bytes == 0
    db.close()


def test_cursor_returned_by_execute_cannot_be_reused_as_an_unmetered_route():
    db = _db()
    with bounded_sql_source_reads(db, 4) as budget:
        # A cursor returned by connection.execute remains directly executable.
        # It must not become an unmetered route after the initial preflight.
        with pytest.raises(SqlReadBudgetExceeded):
            cursor = db.execute("SELECT payload FROM source WHERE n=7")
            cursor.execute("SELECT zeroblob(64)")
        assert budget.source_bytes == 3
    db.close()


def test_cursor_created_before_scope_is_still_metered_inside_scope():
    db = _db()
    cursor = db.cursor()
    with bounded_sql_source_reads(db, 4) as budget:
        cursor.execute("SELECT payload FROM source WHERE n=7")
        assert cursor.fetchone()[0] == b"abc"
        assert budget.source_bytes == 3
        with pytest.raises(SqlReadBudgetExceeded):
            cursor.execute("SELECT zeroblob(64)")
        assert budget.source_bytes == 3
    db.close()


def test_metadata_preflight_cannot_charge_a_different_value_than_raw_execution():
    db = _db()
    values = iter((b"x", b"0123456789"))
    db.create_function("changing_blob", 0, lambda: next(values))
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 1) as budget:
            db.execute("SELECT changing_blob()")
            assert budget.source_bytes == 0
    db.close()


@pytest.mark.parametrize("sql", ["SELECT random()", "SELECT randomblob(32)"])
def test_nondeterministic_builtin_functions_are_refused(sql):
    db = _db()
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 100):
            db.execute(sql)
    db.close()


def test_custom_function_and_overridden_builtin_are_refused_during_scoped_reads():
    db = _db()
    db.create_function("lower", 1, lambda value: "unmetered")
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 100):
            db.execute("SELECT lower('source')")
    db.close()


def test_custom_window_function_cannot_be_registered_during_a_budget_scope():
    db = _db()

    class WindowValue:
        def step(self, value):
            pass

        def inverse(self, value):
            pass

        def value(self):
            return b"x"

        def finalize(self):
            return b"x"

    with bounded_sql_source_reads(db, 10):
        with pytest.raises(SqlReadBudgetExceeded):
            db.create_window_function("sum", 1, WindowValue)
    db.close()


def test_pre_registered_window_function_cannot_override_an_allowed_builtin():
    db = _db()
    outputs = []

    class ChangingWindow:
        def step(self, value):
            pass

        def inverse(self, value):
            pass

        def value(self):
            result = b"x" if not outputs else b"0123456789"
            outputs.append(result)
            return result

        def finalize(self):
            return self.value()

    # sqlite3.Connection.create_window_function is inherited by the budgeted
    # connection, so registration must be tracked before entering the scope.
    db.create_window_function("sum", 1, ChangingWindow)
    sql = "SELECT sum(n) OVER () FROM source WHERE n=7"
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 1):
            db.execute(sql)
    assert outputs == []
    db.close()


def test_custom_collation_refuses_scope_before_changing_sqlite_limits():
    db = _db()
    db.create_collation("custom_order", lambda left, right: (left > right) - (left < right))
    old = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 100):
            pass
    assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == old
    db.close()


def test_authorizer_is_composed_during_scope_and_restored_afterward():
    db = _db()
    calls = []

    def allow_all(*arguments):
        calls.append(arguments)
        return sqlite3.SQLITE_OK

    db.set_authorizer(allow_all)
    with bounded_sql_source_reads(db, 10):
        assert db.execute("SELECT payload FROM source WHERE n=7").fetchone() == (b"abc",)
    count_at_exit = len(calls)
    assert db.execute("SELECT 1").fetchone() == (1,)
    assert len(calls) > count_at_exit
    db.close()


def test_authorizer_and_function_registration_are_refused_while_scope_is_active():
    db = _db()
    with bounded_sql_source_reads(db, 10):
        with pytest.raises(SqlReadBudgetExceeded):
            db.set_authorizer(None)
        with pytest.raises(SqlReadBudgetExceeded):
            db.create_function("new_function", 0, lambda: b"x")
    db.close()


def test_executemany_selecting_source_rows_is_refused_before_insert():
    db = _db()
    db.execute("CREATE TABLE sink (payload)")
    with bounded_sql_source_reads(db, 10):
        with pytest.raises(SqlReadBudgetExceeded):
            db.executemany(
                "INSERT INTO sink SELECT payload FROM source WHERE n=?",
                [(7,), (9,)],
            )
    assert db.execute("SELECT count(*) FROM sink").fetchone()[0] == 0
    db.close()


def test_older_sqlite_and_missing_limit_api_fail_before_scope_mutation(monkeypatch):
    db = _db()
    import issue_agent.adapters.sql_read_budget as module

    monkeypatch.setattr(module.sqlite3, "sqlite_version_info", (3, 42, 0))
    previous = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 10):
            pass
    assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == previous
    monkeypatch.undo()
    monkeypatch.setattr(db, "getlimit", None, raising=False)
    with pytest.raises(ValueError):
        with bounded_sql_source_reads(db, 10):
            pass
    db.close()



def test_nested_savepoint_rollback_preserves_cumulative_read_charges():
    db = _db()
    with bounded_sql_source_reads(db, 10) as outer:
        assert db.execute("SELECT payload FROM source WHERE n=7").fetchone()[0] == b"abc"
        db.execute("SAVEPOINT bounded_publication")
        db.execute("INSERT INTO source VALUES (?, ?)", (b"zz", 11))
        with bounded_sql_source_reads(db, 10):
            assert db.execute("SELECT payload FROM source WHERE n=11").fetchone()[0] == b"zz"
        db.execute("ROLLBACK TO bounded_publication")
        db.execute("RELEASE bounded_publication")
        assert db.execute("SELECT payload FROM source WHERE n=9").fetchone()[0] == b"de"
        with pytest.raises(SqlReadBudgetExceeded):
            db.execute("SELECT payload FROM source")
        assert outer.source_bytes == 7
    assert db.execute("SELECT n FROM source ORDER BY n").fetchall() == [(7,), (9,)]
    assert db.in_transaction
    db.close()


@pytest.mark.parametrize("sql", [
    "SAVEPOINT bounded; SELECT payload FROM source",
    "RELEASE bounded RETURNING payload",
    "ROLLBACK", "COMMIT", "BEGIN",
])
def test_nested_control_forms_cannot_bypass_reads_or_end_the_caller_transaction(sql):
    db = _db()
    with bounded_sql_source_reads(db, 100):
        with pytest.raises(SqlReadBudgetExceeded):
            db.execute(sql)
    assert db.in_transaction
    assert db.execute("SELECT payload FROM source ORDER BY n").fetchall() == [(b"abc",), (b"de",)]
    db.close()


def test_nested_savepoints_still_obey_and_restore_the_caller_authorizer():
    db = _db()
    def reject_savepoints(action, first, second, database, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_SAVEPOINT else sqlite3.SQLITE_OK
    db.set_authorizer(reject_savepoints)
    with pytest.raises(SqlReadBudgetExceeded):
        with bounded_sql_source_reads(db, 100):
            db.execute("SAVEPOINT bounded")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("SAVEPOINT bounded")
    assert db.in_transaction
    db.close()


@pytest.mark.parametrize("factory", (sqlite3.Cursor, lambda db: sqlite3.Cursor(db)))
def test_unmetered_cursor_factory_cannot_be_retained_before_scope(factory):
    db = sqlite3.connect(":memory:", factory=BudgetedSqlConnection)
    try:
        db.execute("CREATE TABLE retained(value BLOB)")
        db.execute("INSERT INTO retained VALUES (?)", (b"x" * 20,))
        with pytest.raises(SqlReadBudgetExceeded, match="metered cursor factory"):
            db.cursor(factory)
        cursor = db.cursor()
        with bounded_sql_source_reads(db, 1):
            with pytest.raises(SqlReadBudgetExceeded, match="cumulative bound"):
                cursor.execute("SELECT value FROM retained")
    finally:
        db.close()
