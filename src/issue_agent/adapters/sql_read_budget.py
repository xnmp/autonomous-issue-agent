"""Scoped metadata preflight for every retained value read in a SQL transaction.

Only the checkpoint/audit paths opt in. Ordinary control-store connections
retain sqlite3 behavior outside the scope. SQLite 3.43's octet_length can inspect
direct stored fields without materializing them; the engine length limit also
bounds expressions evaluated by a metadata query.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3
import re
from typing import Any, Iterator


MAX_SQL_READ_BYTES = 2**27
MAX_SQL_READ_ROWS = 1_000_000
MAX_SQL_VALUE_BYTES = 2**21
_STABLE_SQL_FUNCTIONS = frozenset({
    "abs", "avg", "char", "coalesce", "count", "format", "glob", "hex", "ifnull", "iif", "instr",
    "json_extract", "json_type", "json_valid", "length", "like", "likelihood", "likely",
    "lower", "ltrim", "max", "min", "nullif", "octet_length", "replace",
    "printf", "quote", "round", "rtrim", "sqlite_source_id", "sqlite_version", "substr", "substring",
    "sum", "total", "trim", "typeof", "unicode", "unlikely", "upper", "zeroblob",
})


class SqlReadBudgetExceeded(ValueError):
    pass


@dataclass
class SqlReadBudget:
    maximum_bytes: int
    source_bytes: int = 0
    rows: int = 0

    def charge(self, size: int, rows: int) -> None:
        if (size < 0 or self.source_bytes + size > self.maximum_bytes
                or self.rows + rows > MAX_SQL_READ_ROWS):
            raise SqlReadBudgetExceeded("SQL source read exceeds its cumulative bound")
        self.source_bytes += size
        self.rows += rows


class BudgetedSqlConnection(sqlite3.Connection):
    _source_budgets: tuple[SqlReadBudget, ...] = ()
    _source_custom_functions: frozenset[str] = frozenset()
    _source_custom_collations: frozenset[str] = frozenset()
    _user_authorizer: Any = None
    _source_metadata_sql: dict[str, str] | None = None

    def set_authorizer(self, authorizer_callback: Any) -> None:
        if self._source_budgets:
            raise SqlReadBudgetExceeded("SQL source budget cannot change its authorizer")
        self._user_authorizer = authorizer_callback
        super().set_authorizer(authorizer_callback)

    def _budget_authorizer(self, action, first, second, database, trigger):
        if action == sqlite3.SQLITE_FUNCTION and (
            second is None or second.casefold() not in _STABLE_SQL_FUNCTIONS
            or second.casefold() in self._source_custom_functions
        ):
            return sqlite3.SQLITE_DENY
        if self._user_authorizer is not None:
            return self._user_authorizer(action, first, second, database, trigger)
        return sqlite3.SQLITE_OK

    def create_function(self, name, narg, func, *, deterministic=False):
        if self._source_budgets:
            raise SqlReadBudgetExceeded("SQL source budget cannot register functions")
        self._source_custom_functions = self._source_custom_functions | {name.casefold()}
        return super().create_function(name, narg, func, deterministic=deterministic)

    def create_aggregate(self, name, n_arg, aggregate_class):
        if self._source_budgets:
            raise SqlReadBudgetExceeded("SQL source budget cannot register aggregates")
        self._source_custom_functions = self._source_custom_functions | {name.casefold()}
        return super().create_aggregate(name, n_arg, aggregate_class)

    def create_window_function(self, name, num_params, aggregate_class, /):
        if self._source_budgets:
            raise SqlReadBudgetExceeded("SQL source budget cannot register window functions")
        self._source_custom_functions = self._source_custom_functions | {name.casefold()}
        return super().create_window_function(name, num_params, aggregate_class)

    def create_collation(self, name, callable):
        if self._source_budgets:
            raise SqlReadBudgetExceeded("SQL source budget cannot register collations")
        self._source_custom_collations = self._source_custom_collations | {name.casefold()}
        return super().create_collation(name, callable)

    def _preflight_read(self, sql: str, parameters: Any) -> None:
        if not self.in_transaction:
            raise SqlReadBudgetExceeded("SQL source budget lost its transaction")
        statement = sql.strip().rstrip(";")
        head = statement.split(None, 1)[0].casefold() if statement else ""
        # Nested publishers use transaction-local savepoints under the same
        # cumulative meter. Ending the caller's transaction remains forbidden.
        if re.fullmatch(
            r"(?:SAVEPOINT|RELEASE(?: SAVEPOINT)?|ROLLBACK(?: TRANSACTION)? TO(?: SAVEPOINT)?)"
            r"\s+[A-Za-z_][A-Za-z_0-9]{0,127}", statement, re.IGNORECASE,
        ):
            return
        if head not in {"select", "with"}:
            if (head not in {"insert", "update", "delete"}
                    or re.search(r"\b(select|returning|with)\b", statement, re.IGNORECASE)):
                raise SqlReadBudgetExceeded("SQL source budget does not permit this statement form")
            return
        # The outer SELECT uses SQLite's result-column names (including its
        # disambiguation of duplicate aliases), not caller-provided identifiers.
        templates = self._source_metadata_sql
        metadata_sql = templates.get(statement) if templates is not None else None
        if metadata_sql is None:
            description = super().execute(f"SELECT * FROM ({statement}) LIMIT 0", parameters).description
            if description is None:
                raise SqlReadBudgetExceeded("SQL source budget requires a read-only result")
            expressions = []
            for column in description:
                quoted = '"' + column[0].replace('"', '""') + '"'
                expressions.append(
                    f"CASE typeof({quoted}) WHEN 'null' THEN 0 "
                    f"WHEN 'integer' THEN 8 WHEN 'real' THEN 8 "
                    f"ELSE octet_length({quoted}) END"
                )
            metadata_sql = f"SELECT {'+'.join(expressions) or '0'} FROM ({statement})"
            if templates is not None and len(templates) < 128 and len(metadata_sql) <= 16384:
                templates[statement] = metadata_sql
        metadata = super().execute(metadata_sql, parameters)
        metadata.row_factory = None
        size, rows = 0, 0
        for row in metadata:
            if type(row[0]) is not int or row[0] < 0:
                raise SqlReadBudgetExceeded("SQL source metadata is malformed")
            size += row[0]
            rows += 1
            if any(budget.source_bytes + size > budget.maximum_bytes
                   or budget.rows + rows > MAX_SQL_READ_ROWS for budget in self._source_budgets):
                raise SqlReadBudgetExceeded("SQL source read exceeds its cumulative bound")
        for budget in self._source_budgets:
            budget.charge(size, rows)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return super().cursor(BudgetedSqlCursor).execute(sql, parameters)

    def cursor(self, factory: Any = None) -> sqlite3.Cursor:
        if factory is not None and factory is not BudgetedSqlCursor:
            # A cursor retained from before a budget scope must still meter
            # later execution. Custom factories can override that contract.
            raise SqlReadBudgetExceeded("SQL source connection requires its metered cursor factory")
        if self._source_budgets:
            # There is one metered route; a raw cursor would bypass preflight.
            raise SqlReadBudgetExceeded("SQL source budget requires connection.execute")
        return super().cursor(BudgetedSqlCursor)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return super().cursor(BudgetedSqlCursor).executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        if self._source_budgets:
            raise SqlReadBudgetExceeded("SQL source budget does not permit scripts")
        return super().executescript(sql_script)


class BudgetedSqlCursor(sqlite3.Cursor):
    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        connection = self.connection
        if isinstance(connection, BudgetedSqlConnection) and connection._source_budgets:
            try:
                connection._preflight_read(sql, parameters)
            except sqlite3.Error as exc:
                raise SqlReadBudgetExceeded("SQL source metadata preflight refused") from exc
        return super().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        connection = self.connection
        if isinstance(connection, BudgetedSqlConnection) and connection._source_budgets:
            connection._preflight_read(sql, ())
        return super().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        connection = self.connection
        if isinstance(connection, BudgetedSqlConnection) and connection._source_budgets:
            raise SqlReadBudgetExceeded("SQL source budget does not permit scripts")
        return super().executescript(sql_script)


@contextmanager
def bounded_sql_source_reads(
    db: sqlite3.Connection, maximum_bytes: int,
) -> Iterator[SqlReadBudget]:
    if (not isinstance(db, BudgetedSqlConnection) or not db.in_transaction
            or type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_SQL_READ_BYTES):
        raise ValueError("bounded SQL source reads require a metered transaction and byte ceiling")
    if sqlite3.sqlite_version_info < (3, 43, 0) or not all(
        callable(getattr(db, name, None)) for name in ("getlimit", "setlimit")
    ):
        raise SqlReadBudgetExceeded("bounded SQL source reads require SQLite 3.43+ and length-limit APIs")
    if db._source_custom_collations:
        raise SqlReadBudgetExceeded("bounded SQL source reads cannot use custom collations")
    budget = SqlReadBudget(maximum_bytes)
    previous = db._source_budgets
    previous_limit = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(previous_limit, MAX_SQL_VALUE_BYTES))
    db._source_budgets = previous + (budget,)
    if not previous:
        # DDL is forbidden inside this transaction-local scope. Only column
        # templates are reused: values, rows and budget charges are always fresh.
        db._source_metadata_sql = {}
        sqlite3.Connection.set_authorizer(db, db._budget_authorizer)
    try:
        yield budget
    except sqlite3.Error as exc:
        raise SqlReadBudgetExceeded("bounded SQL source read is unreadable or oversized") from exc
    finally:
        db._source_budgets = previous
        if not previous:
            db._source_metadata_sql = None
            sqlite3.Connection.set_authorizer(db, db._user_authorizer)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)
