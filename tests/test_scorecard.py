"""The trust scorecard: rule loading, verdict logic, the measures on the
synthetic snapshot in conftest.py, and the real report definitions end to
end on that snapshot."""
from pathlib import Path

import duckdb
import pytest

from dq import completeness as cp
from dq import scorecard as sc
from dq.engine import load_suite, run_suite
from tests.conftest import PARAMS

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "checks" / "pedestrian.yml"
REPORTS = ROOT / "checks" / "reports.yml"


# ---- loading and verdicts ---------------------------------------------------

def write(tmp_path, text: str) -> Path:
    p = tmp_path / "reports.yml"
    p.write_text(text)
    return p


REPORT = """
reports:
  - id: r
    name: R
    question: Q?
    window: w
    rules:
"""


@pytest.mark.parametrize("rule, error", [
    ("- {check: nope, status: [error], verdict: dont_use, reason: x}", "unknown check nope"),
    ("- {measure: nope, above: 1, verdict: caveat, reason: x}", "unknown measure nope"),
    ("- {measure: m, above: 1, verdict: trusted, reason: x}", "verdict must be"),
    ("- {measure: m, above: 1, below: 2, verdict: caveat, reason: x}", "exactly one of above"),
    ("- {measure: m, check: c, status: [warn], verdict: caveat, reason: x}", "exactly one of check"),
    ("- {check: c, status: [fail], verdict: caveat, reason: x}", "status must be"),
])
def test_bad_rules_are_rejected(tmp_path, rule, error):
    path = write(tmp_path, REPORT + "      " + rule + "\n")
    with pytest.raises(ValueError, match=error):
        sc.load_reports(path, {"c"}, {"m"})


def test_unknown_rule_set_is_rejected(tmp_path):
    path = write(tmp_path, REPORT.replace("    rules:", "    include: [nope]"))
    with pytest.raises(ValueError, match="unknown rule set"):
        sc.load_reports(path, set(), set())


def evaluate(rules: str, measures: dict) -> sc.Verdict:
    path = Path(__file__).parent / "_tmp_reports.yml"
    try:
        path.write_text(REPORT + rules)
        reports, _ = sc.load_reports(path, set(), set(measures))
    finally:
        path.unlink()
    return sc.evaluate(reports, {}, measures)[0]


TIERS = """
      - {measure: m, above: 1, verdict: caveat, reason: "m is {value:.1f}"}
      - {measure: m, above: 5, verdict: dont_use, reason: "m is {value:.1f} ({detail})"}
"""


@pytest.mark.parametrize("value, verdict, fired", [
    (0.5, "trusted", 0), (1.0, "trusted", 0), (3.0, "caveat", 1), (7.0, "dont_use", 2),
])
def test_worst_rule_wins(value, verdict, fired):
    v = evaluate(TIERS, {"m": sc.Measure(value, "d")})
    assert v.verdict == verdict
    assert sum(o.fired for o in v.outcomes) == fired
    # when both tiers fire only the worse one is reported
    assert len(v.fired) == min(fired, 1)
    if fired == 2:
        assert v.fired[0].reason == "m is 7.0 (d)"


def test_unmeasurable_rule_gives_a_caveat():
    v = evaluate(TIERS, {"m": sc.Measure(None, "no counts")})
    assert v.verdict == "caveat"
    assert "can't be measured" in v.fired[0].reason


# ---- measures on the synthetic snapshot ---------------------------------------

@pytest.fixture
def measures(con):
    cp.build_gaps(con)
    cp.estimate_gaps(con)
    return sc.compute_measures(con)


def test_windows(con):
    w = {k: (str(a), str(b)) for k, (a, b) in sc.windows(con).items()}
    assert w == {
        "newest_day": ("2026-10-10", "2026-10-10"),
        "last_day": ("2026-10-09", "2026-10-09"),
        "last_28_days": ("2026-09-12", "2026-10-09"),
        "month": ("2026-09-01", "2026-09-30"),
        "prev_month": ("2026-08-01", "2026-08-31"),
        "year_ago_month": ("2025-09-01", "2025-09-30"),
    }


def test_measures_on_the_synthetic_snapshot(measures):
    v = {k: m.value for k, m in measures.items()}
    # newest day: 4 hours x 4 sensors; the previous 28 days have a median of
    # 96 rows (4 sensors x 24; sensor 3 adds 24 until 09-15, 09-20 and 10-04
    # have fewer)
    assert v["newest_day_rows_pct"] == pytest.approx(100 * 16 / 96)
    assert v["last_day_rows_pct"] == pytest.approx(100.0)
    assert v["last_day_outage_pct"] == 0
    assert v["last_day_dropped_sensors"] == 0
    # sensor 2 is dark on 09-20, inside the window: its count is 3h + 2, so
    # the day is worth 876 and the estimate recovers it exactly; on 10-04 the
    # 02:00 hour doesn't exist (868)
    assert v["ranking_max_shortfall_pct"] == pytest.approx(100 * 876 / (26 * 876 + 868 + 876))
    assert measures["ranking_max_shortfall_pct"].detail.startswith("1 of 5 sensors")
    # sensor 3 stops on 09-15, inside the window
    assert v["ranking_partial_sensors"] == 1
    assert v["window_orphan_sensors"] == 1          # sensor 99
    assert v["window_shared_names"] == 1            # Dup_T
    assert measures["window_shared_names"].detail == "Dup_T"
    # no quiet gaps in the window, so nothing to overstate
    assert v["profile_max_overstatement_pct"] == 0
    # there are no counts before September 2026
    for k in ("mom_outage_pct", "mom_distortion_pts", "mom_sensor_mix_pts",
              "yoy_outage_pct", "yoy_distortion_pts", "yoy_sensor_mix_pts"):
        assert v[k] is None, k


def test_last_day_dropped_sensor(con):
    con.execute("delete from counts where location_id = 1 and sensing_date = DATE '2026-10-09'")
    cp.build_gaps(con)
    cp.estimate_gaps(con)
    m = sc.compute_measures(con)
    # sensor 1 reports on 10-10 again, so 10-09 is an outage it can be
    # filled for. Sensor s counts 3h + s an hour, so 3 x 276 + 24s a day.
    day = {s: 3 * 276 + 24 * s for s in (1, 11, 2, 99)}
    others = day[11] + day[2] + day[99]
    assert m["last_day_outage_pct"].value == pytest.approx(100 * day[1] / (others + day[1]))
    assert m["last_day_dropped_sensors"].value == 0
    con.execute("delete from counts where location_id = 1 and sensing_date = DATE '2026-10-10'")
    cp.build_gaps(con)
    cp.estimate_gaps(con)
    m = sc.compute_measures(con)
    # now sensor 1 just stopped: not an outage in the expected hours, but a
    # dropped sensor (and the newest day is checked against fewer rows)
    assert m["last_day_outage_pct"].value == 0
    assert m["last_day_dropped_sensors"].value == 1
    assert m["last_day_dropped_sensors"].detail == "One sensor (1)"


def test_profile_overstatement(con):
    # Make 2026-10-01 00:00-03:00 quiet for sensor 1 and drop 01:00-02:00:
    # a 2-hour likely-zero gap inside the window.
    con.execute("""update counts set pedestriancount = 1, direction_1 = 0, direction_2 = 1
                   where location_id = 1 and sensing_date = DATE '2026-10-01' and hourday <= 3""")
    con.execute("""delete from counts where location_id = 1
                   and sensing_date = DATE '2026-10-01' and hourday in (1, 2)""")
    cp.build_gaps(con)
    cp.estimate_gaps(con)
    m = sc.compute_measures(con)["profile_max_overstatement_pct"]
    # 02:00 has the fewest published sensor-hours in the window: 4 sensors x
    # 28 days, less 4 on 10-04 (no 02:00), less sensor 2's dark 09-20, plus
    # sensor 3's 4 days, less the gap = 110; with one more hour at 0 the
    # mean is over 111 hours, so the plain mean is 111/110 - 1 too high
    assert m.value == pytest.approx(100 / 110)
    assert m.detail == "worst hour 02:00"


def test_period_comparison():
    con = duckdb.connect()
    con.execute("""create table counts as select * from (values
        (1, DATE '2026-08-01', 0, 100), (1, DATE '2026-09-01', 0, 110),
        (2, DATE '2026-08-01', 0, 50),  (2, DATE '2026-09-01', 0, 50),
        (3, DATE '2026-09-01', 0, 40)
    ) t(location_id, sensing_date, hourday, pedestriancount)""")
    con.execute("""create table gap_est as select 1 as location_id,
        DATE '2026-09-02' as sensing_date, 0 as hourday,
        'short_outage' as gap_class, 20 as est""")
    con.execute("create table sensors as select 1 as location_id, 'One' as sensor_description")
    m = sc._compare(con, (sc.date(2026, 8, 1), sc.date(2026, 8, 31)),
                    (sc.date(2026, 9, 1), sc.date(2026, 9, 30)), "mom")
    # published 150 -> 200 (+33.3%); gap-filled 150 -> 220 (+46.7%)
    assert m["mom_distortion_pts"].value == pytest.approx(100 * (220 / 150 - 200 / 150))
    assert m["mom_outage_pct"].value == pytest.approx(100 * 20 / 220)
    # like-for-like (sensors 1 and 2): 150 -> 160 (+6.7%); sensor 3 is new
    assert m["mom_sensor_mix_pts"].value == pytest.approx(100 * (200 / 150 - 160 / 150))
    assert "1 in only one: (no sensor record) (3)" in m["mom_sensor_mix_pts"].detail


# ---- the real reports on the synthetic snapshot -------------------------------

def test_real_reports_on_the_synthetic_snapshot(con, measures):
    results = {r.check.id: r for r in run_suite(con, load_suite(SUITE), PARAMS)}
    reports, notes = sc.load_reports(REPORTS, set(results), set(measures))
    verdicts = {v.report.id: v for v in sc.evaluate(reports, results, measures)}
    assert {k: v.verdict for k, v in verdicts.items()} == {
        "yesterday": "dont_use",            # the newest day only has hours 0-3
        "last_complete_day": "trusted",
        "month_on_month": "caveat",         # no August: can't be checked
        "year_on_year": "caveat",
        "street_ranking": "caveat",         # sensor 2 short by 3.4%, plus more
        "hourly_profile": "trusted",
        "sensor_map": "caveat",
    }
    fired = {o.rule.ref for o in verdicts["street_ranking"].fired}
    assert fired == {"ranking_max_shortfall_pct", "ranking_partial_sensors",
                     "window_orphan_sensors", "window_shared_names"}
    fired = {o.rule.ref for o in verdicts["sensor_map"].fired}
    assert fired == {"sensors_active_outdoor_reporting", "sensors_name_unique",
                     "window_orphan_sensors"}
    assert notes


def test_a_broken_key_stops_every_count_report(con, measures):
    # duplicate one sensor-hour: counts_grain_unique fails with an error
    con.execute("insert into counts select * from counts limit 1")
    results = {r.check.id: r for r in run_suite(con, load_suite(SUITE), PARAMS)}
    reports, _ = sc.load_reports(REPORTS, set(results), set(measures))
    verdicts = {v.report.id: v.verdict for v in sc.evaluate(reports, results, measures)}
    assert all(verdicts[r] == "dont_use" for r in verdicts if r != "sensor_map")
    assert verdicts["sensor_map"] == "caveat"
