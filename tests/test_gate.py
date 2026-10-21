"""Part 4b: the daily gate on the synthetic snapshot in conftest.py.

The snapshot's one error is `counts_sensor_exists`: sensor 99 has 939 count
rows and no sensor record. The tests accept it as a known issue, then plant a
second sensor without a record and check that the gate still fails.
"""
from pathlib import Path

import pytest

from dq import gate
from dq.drift import run_drift
from dq.engine import load_suite, run_suite
from tests.conftest import PARAMS
from tests.test_drift import SUITE as DRIFT_SUITE, build

CHECKS = Path(__file__).resolve().parents[1] / "checks"
SUITE = load_suite(CHECKS / "pedestrian.yml")
SENSOR_99_ROWS = 39 * 24 - 1 + 4
ISSUE = {"check": "counts_sensor_exists", "owner": "Data engineering",
         "since": "2026-10-19", "reason": "Sensor 99 has no record."}


def known(tmp_path, *entries):
    path = tmp_path / "known.yml"
    lines = ["known_issues:"]
    for e in entries:
        lines.append(f"  - check: {e['check']}")
        lines += [f"    {k}: {v!r}" if k == "accept_where" else f"    {k}: {v}"
                  for k, v in e.items() if k != "check"]
    path.write_text("\n".join(lines) + "\n")
    return gate.load_known_issues(path, SUITE + DRIFT_SUITE)


def run_gate(con, issues):
    rows = gate.evaluate(con, "pedestrian.yml", run_suite(con, SUITE, PARAMS), issues)
    return {r.result.check.id: (r.outcome, r.unaccepted) for r in rows}, gate.passed(rows)


def add_sensor_without_record(con, location_id, hours):
    con.execute(f"""
        insert into counts (id, location_id, sensing_date, hourday, direction_1,
                            direction_2, pedestriancount, sensor_name)
        select 0, {location_id}, DATE '2026-10-09', h, 1, 1, 2, NULL from range({hours}) t(h)
    """)


def test_real_known_issues_file_is_valid():
    issues = gate.load_known_issues(CHECKS / "known_issues.yml", SUITE + DRIFT_SUITE)
    assert set(issues) == {"counts_sensor_exists"}


def test_an_error_nobody_has_accepted_fails_the_gate(con, tmp_path):
    rows, ok = run_gate(con, known(tmp_path))
    assert rows == {"counts_sensor_exists": ("blocking", SENSOR_99_ROWS)}
    assert not ok


def test_a_known_issue_is_accepted(con, tmp_path):
    rows, ok = run_gate(con, known(tmp_path, {**ISSUE, "accept_where": "location_id = 99"}))
    assert rows == {"counts_sensor_exists": ("known", 0)}
    assert ok


def test_a_new_problem_in_an_accepted_check_still_fails(con, tmp_path):
    # a count baseline would let this through: 3 new rows are far fewer than
    # the 939 already accepted
    add_sensor_without_record(con, 98, 3)
    rows, ok = run_gate(con, known(tmp_path, {**ISSUE, "accept_where": "location_id = 99"}))
    assert rows == {"counts_sensor_exists": ("blocking", 3)}
    assert not ok


def test_a_condition_that_is_null_does_not_accept(con, tmp_path):
    # `98 in (99, null)` is null, not false
    add_sensor_without_record(con, 98, 3)
    rows, ok = run_gate(con, known(tmp_path, {**ISSUE,
                                              "accept_where": "location_id in (99, null)"}))
    assert rows == {"counts_sensor_exists": ("blocking", 3)}
    assert not ok


def test_a_known_issue_that_passes_is_listed_as_resolved(con, tmp_path):
    con.execute("""insert into sensors values (99, 'Ninety-nine sensor', 'Ninety-nine_T',
                   DATE '2020-01-01', 'Outdoor', 'A', -37.81, 144.96)""")
    rows, ok = run_gate(con, known(tmp_path, {**ISSUE, "accept_where": "location_id = 99"}))
    assert rows == {"counts_sensor_exists": ("resolved", 0)}
    assert ok


def test_drift_errors_fail_the_gate():
    con = build(planted=True)
    results, _ = run_drift(con, DRIFT_SUITE, {})
    rows = gate.evaluate(con, "drift.yml", results, {})
    assert {r.result.check.id for r in rows} == {
        r.check.id for r in results if r.status == "error"}
    assert rows and not gate.passed(rows)
    clean_con = build(planted=False)
    clean, _ = run_drift(clean_con, DRIFT_SUITE, {})
    assert gate.evaluate(clean_con, "drift.yml", clean, {}) == []


@pytest.mark.parametrize("entry, message", [
    ({**ISSUE, "check": "no_such_check", "accept_where": "true"}, "no such check"),
    ({**ISSUE, "check": "counts_id_unique", "accept_where": "true"}, "only error checks"),
    ({**ISSUE, "accept_where": "true", "owner": ""}, "missing owner"),
    ({**ISSUE, "reason": "", "accept_where": ""}, "missing accept_where, reason"),
    ({**ISSUE, "accept_where": "true", "expires": "2027-01-01"}, "unknown field"),
])
def test_known_issues_are_validated(tmp_path, entry, message):
    with pytest.raises(ValueError, match=message):
        known(tmp_path, entry)


def test_the_same_check_cannot_be_accepted_twice(tmp_path):
    entry = {**ISSUE, "accept_where": "true"}
    with pytest.raises(ValueError, match="listed twice"):
        known(tmp_path, entry, entry)
