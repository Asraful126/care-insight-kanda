"""Unit tests for the query_gold guard (no database needed)."""

import pytest

from assistant.sql_tool import MAX_ROWS, GoldQuery, SqlRejected, validate

ALLOWED = [
    "SELECT TOP 5 * FROM kanda_assistant.v_ndis_plans",
    "select region_name, active_participants from KANDA_ASSISTANT.V_NDIS_PLANS where region_type = 'STATE'",
    """WITH s AS (SELECT state_code, people FROM kanda_assistant.v_aged_care_services WHERE program = 'HCP')
       SELECT state_code, SUM(people) AS people FROM s GROUP BY state_code""",
    """SELECT p.region_name, p.active_participants, v.provider_count
       FROM kanda_assistant.v_ndis_plans p
       JOIN kanda_assistant.v_ndis_providers v ON v.region_key = p.region_key AND v.report_date = p.report_date""",
    "SELECT 1 AS a FROM kanda_assistant.v_region UNION ALL SELECT 2 FROM kanda_assistant.v_region",
    "SELECT * FROM kanda_assistant.v_region WHERE region_key IN (SELECT region_key FROM kanda_assistant.v_ndis_plans)",
]

REJECTED = [
    ("", "Empty"),
    ("SELECT * FROM kanda_gold.fact_ndis_plans", "not an allowed view"),
    ("SELECT * FROM v_ndis_plans", "not an allowed view"),  # no schema
    ("SELECT * FROM kanda_silver.ndis_plans", "not an allowed view"),
    ("SELECT * FROM lh_care_insights.kanda_assistant.v_ndis_plans", "prefix"),
    ("SELECT * FROM kanda_assistant.v_region; DROP TABLE kanda_gold.dim_region", "exactly one"),
    ("DELETE FROM kanda_assistant.v_region", "Only SELECT"),
    ("UPDATE kanda_assistant.v_region SET region_name = 'x'", "Only SELECT"),
    ("SELECT * INTO kanda_assistant.copy FROM kanda_assistant.v_region", "INTO"),
    ("EXEC sp_who", "Only SELECT"),
    ("CREATE VIEW kanda_assistant.x AS SELECT 1 AS a", "Only SELECT"),
    ("SELECT * FROM OPENROWSET(BULK 'https://x/y.csv', FORMAT = 'CSV') AS r", None),
    ("SELECT name FROM sys.tables", "not an allowed view"),
    ("SELECT 1", "must read from"),
    ("SELECT * FROM kanda_assistant.v_region -- " + "x" * 6000, "longer than"),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    assert validate(sql) == sql


@pytest.mark.parametrize("sql,message", REJECTED)
def test_rejected(sql, message):
    with pytest.raises(SqlRejected) as err:
        validate(sql)
    if message:
        assert message in str(err.value)


class FakeCursor:
    def __init__(self, n):
        self.n, self.description = n, [("x",)]

    def execute(self, sql):
        pass

    def fetchmany(self, k):
        return [(i,) for i in range(min(k, self.n))]


class FakeConn:
    def __init__(self, n):
        self.n = n

    def cursor(self):
        return FakeCursor(self.n)


@pytest.mark.parametrize("n,truncated", [(3, False), (MAX_ROWS, False), (MAX_ROWS + 50, True)])
def test_row_limit(n, truncated):
    out = GoldQuery(connect=lambda: FakeConn(n)).run("SELECT x FROM kanda_assistant.v_region")
    assert out["row_count"] == min(n, MAX_ROWS)
    assert out["truncated"] is truncated
