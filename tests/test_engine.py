"""Offline tests for the check engine: every check type, tolerance and
suite validation."""
from pathlib import Path
import textwrap

import duckdb
import pytest

from dq.engine import Check, load_suite, run_check, status_for
from dq.load import snapshot_params

SUITE = Path(__file__).resolve().parents[1] / "checks" / "pedestrian.yml"
PARAMS = {"reference_date": "2026-10-11"}


@pytest.fixture
def tiny():
    c = duckdb.connect()
    c.execute("""create table t as select * from (values
        (1, 'a', 5, DATE '2026-10-09'), (2, 'b', NULL, DATE '2026-10-10'),
        (2, 'c', -1, DATE '2026-10-08'), (3, 'z', 7, DATE '2026-10-07')
    ) v(k, cat, x, d)""")
    c.execute("create table parent as select * from (values (1), (2)) v(k)")
    return c


def check(ctype, **options):
    tol = options.pop("tolerance_pct", 0.0)
    return Check(id="c", type=ctype, description="", dimension="d",
                 severity="warn", table=options.pop("table", "t"),
                 tolerance_pct=tol, options=options)


@pytest.mark.parametrize("ctype, options, failing", [
    ("not_null", {"columns": ["x"]}, 1),
    ("not_null", {"columns": ["k", "cat"]}, 0),
    # both rows sharing k = 2 count as failing
    ("unique", {"columns": ["k"]}, 2),
    ("unique", {"columns": ["k", "cat"]}, 0),
    ("accepted_range", {"column": "x", "min": 0}, 1),
    ("accepted_range", {"column": "x", "min": 0, "max": 6}, 2),
    ("accepted_values", {"column": "cat", "values": ["a", "b", "c"]}, 1),
    ("relationships", {"column": "k", "to": "parent", "field": "k"}, 1),
    # a NULL result counts as a failure, not a pass
    ("expression", {"expression": "x >= 0"}, 2),
    ("freshness", {"column": "d", "max_age_days": 1}, 0),
    ("freshness", {"column": "d", "max_age_days": 0}, 1),
    ("sql", {"sql": "select * from t where k = 3"}, 1),
])
def test_check_types(tiny, ctype, options, failing):
    r = run_check(tiny, check(ctype, **options), PARAMS)
    assert r.failing == failing
    assert r.status == ("pass" if failing == 0 else "warn")
    # unique checks sample the duplicated keys, the others the failing rows
    assert bool(r.sample) == bool(failing)


def test_sql_check_uses_params_and_checked_sql(tiny):
    c = check("sql", table=None,
              sql="select * from t where d < DATE '{reference_date}' - 3",
              checked_sql="select count(*) from t where k > 1")
    r = run_check(tiny, c, PARAMS)
    assert (r.failing, r.checked) == (1, 3)


def test_tolerance():
    c = check("not_null", columns=["x"], tolerance_pct=1.0)
    assert status_for(c, 1, 100) == "pass"
    assert status_for(c, 2, 100) == "warn"
    assert status_for(c, 0, 0) == "pass"


def test_real_suite_loads():
    checks = load_suite(SUITE)
    assert len(checks) == len({c.id for c in checks}) >= 15
    assert all(c.description for c in checks)


@pytest.mark.parametrize("body, message", [
    ("- {id: a, type: nope, table: t, description: x, dimension: d}", "unknown check type"),
    ("- {id: a, type: unique, table: t, description: x, dimension: d}", "missing columns"),
    ("- {id: a, type: not_null, columns: [x], description: x, dimension: d}", "missing table"),
    ("- {id: a, type: sql, sql: 'select 1', severity: fatal, description: x, dimension: d}", "severity"),
    ("- {id: a, type: sql, sql: 'select 1', description: x, dimension: d}\n"
     "- {id: a, type: sql, sql: 'select 1', description: x, dimension: d}", "duplicate"),
])
def test_suite_validation(tmp_path, body, message):
    path = tmp_path / "suite.yml"
    path.write_text("checks:\n" + textwrap.indent(body, "  ") + "\n")
    with pytest.raises(ValueError, match=message):
        load_suite(path)


def test_reference_date_is_melbourne_local():
    # 15:39 UTC on 18 Oct is 02:39 on 19 Oct in Melbourne (AEDT, UTC+11)
    source = {"snapshot": "s", "files": {"counts": {
        "data_processed": "2026-10-18T15:39:17+00:00",
        "timezone": "Australia/Melbourne"}}}
    p = snapshot_params(source)
    assert p["reference_date"] == "2026-10-19"
    assert p["reference_ts"] == "2026-10-19T02:39+11:00"
