"""query_gold: read-only T-SQL over the kanda_assistant views, for Claude.

Two layers, on purpose (decision, Day 5):
1. The database is the real boundary. The reader service principal can SELECT the kanda_assistant
   views and nothing else (scripts/setup_assistant_sql.py proves it on every run).
2. This module checks the SQL before it is sent: it gives Claude a precise error it can correct, and it
   keeps the tool honest even if someone points it at a more privileged identity.
   - parsed with sqlglot (T-SQL dialect); exactly one statement, and it must be a query (SELECT,
     optionally WITH / UNION);
   - no DML/DDL/EXEC/SELECT INTO anywhere in the tree;
   - every table reference is an allow-listed view in kanda_assistant (CTE names excepted), with no
     database/server prefix and no table-valued functions (OPENROWSET etc.).
3. At most MAX_ROWS rows come back (fetchmany, so the SQL is never rewritten) with a `truncated` flag,
   and each query has a QUERY_TIMEOUT_S timeout.
"""

from __future__ import annotations

import datetime as dt
import decimal
import time

import pyodbc
import sqlglot
from sqlglot import exp

SCHEMA = "kanda_assistant"
ALLOWED_VIEWS = frozenset({
    "v_region",
    "v_ndis_cohort_members",
    "v_ndis_plans",
    "v_ndis_utilisation",
    "v_ndis_providers",
    "v_ndis_participants_sa3",
    "v_aged_care_services",
})
MAX_ROWS = 200
QUERY_TIMEOUT_S = 30
MAX_SQL_CHARS = 5000

_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.Into, exp.TruncateTable, exp.Grant, exp.Use, exp.Set, exp.Transaction,
)


class SqlRejected(ValueError):
    """The SQL broke a guardrail. The message is written for Claude to act on."""


def validate(sql: str) -> str:
    """Return the SQL unchanged if it passes every check, else raise SqlRejected."""
    if not sql or not sql.strip():
        raise SqlRejected("Empty query.")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRejected(f"Query is longer than {MAX_SQL_CHARS} characters; simplify it.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SqlRejected(f"Could not parse the SQL as T-SQL: {e}") from None
    if len(statements) != 1:
        raise SqlRejected("Send exactly one statement (no semicolon-separated batches).")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise SqlRejected("Only SELECT queries are allowed.")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN):
            raise SqlRejected(f"{node.key.upper()} is not allowed; the tool is read-only (SELECT only).")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = list(tree.find_all(exp.Table))
    if not tables:
        raise SqlRejected(f"Query must read from a {SCHEMA} view.")
    for table in tables:
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected("Table-valued functions (OPENROWSET, OPENQUERY, …) are not allowed.")
        name, schema, catalog = table.name.lower(), table.db.lower(), table.catalog
        if not schema and name in cte_names:
            continue
        if catalog:
            raise SqlRejected("Don't use a database or server prefix; write kanda_assistant.<view>.")
        if schema != SCHEMA or name not in ALLOWED_VIEWS:
            raise SqlRejected(
                f"'{table.sql(dialect='tsql')}' is not an allowed view. "
                f"Use {SCHEMA}.<view> with one of: {', '.join(sorted(ALLOWED_VIEWS))}."
            )
    return sql


def _json_value(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v


class GoldQuery:
    """Runs validated SQL as the reader service principal. One connection, reopened on failure."""

    def __init__(self, connect=None) -> None:
        if connect is None:
            from assistant.db import connect as db_connect

            connect = lambda: db_connect("reader", timeout_s=QUERY_TIMEOUT_S)  # noqa: E731
        self._connect = connect
        self._conn = None

    def _cursor(self):
        if self._conn is None:
            self._conn = self._connect()
        return self._conn.cursor()

    def run(self, sql: str) -> dict:
        validate(sql)
        started = time.monotonic()
        for attempt in (1, 2):  # the Entra token expires after ~1 h: reconnect once
            try:
                cur = self._cursor()
                cur.execute(sql)
                columns = [d[0] for d in cur.description]
                rows = cur.fetchmany(MAX_ROWS + 1)
                break
            except pyodbc.OperationalError:
                self._conn = None
                if attempt == 2:
                    raise
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        return {
            "columns": columns,
            "rows": [[_json_value(v) for v in r] for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "note": f"Only the first {MAX_ROWS} rows are shown; aggregate or filter further." if truncated else None,
            "elapsed_s": round(time.monotonic() - started, 2),
        }
