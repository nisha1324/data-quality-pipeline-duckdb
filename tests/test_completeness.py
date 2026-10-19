"""Gap classification, imputation and the zero signature on a synthetic
sensor with known gaps.

Sensor 7 reports every hour from 2026-08-03 to 2026-10-10 with the same
pattern every day: 1 person an hour from 00:00 to 05:59, 100 + hour after
that. Planted gaps:
- 2026-08-20 02:00-03:00: quiet on both sides            -> likely_zero, 2 h
- 2026-08-25 12:00: between 111 and 113 people            -> short_outage, 1 h
- 2026-09-08, the whole day                               -> long_outage, 24 h
- 2026-10-04 01:00 and 03:00: 02:00 doesn't exist that day (daylight saving
  starts), so these are one gap of 2 hours, not two gaps  -> likely_zero, 2 h
"""
import duckdb
import pytest

from dq import completeness as cp
from dq.load import create_views


def pattern(h: int) -> int:
    return 1 if h < 6 else 100 + h


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
        create table counts as
        select 7 as location_id, d.day::date as sensing_date, h.hourday,
               case when h.hourday < 6 then 1 else 100 + h.hourday end as pedestriancount
        from generate_series(DATE '2026-08-03', DATE '2026-10-10', interval 1 day) d(day)
        cross join range(24) h(hourday)
        where not (d.day = DATE '2026-08-20' and h.hourday in (2, 3))
          and not (d.day = DATE '2026-08-25' and h.hourday = 12)
          and not (d.day = DATE '2026-09-08')
          and not (d.day = DATE '2026-10-04' and h.hourday in (1, 2, 3))
    """)
    c.execute("""create table sensors as select 7 as location_id,
                 'Seven' as sensor_description""")
    create_views(c)
    yield c
    c.close()


def gaps(con):
    return con.execute("""
        select strftime(start_hour, '%Y-%m-%d %H'), hours, before_n, after_n, gap_class
        from gaps order by start_hour
    """).fetchall()


def test_gaps_are_classed(con):
    cp.build_gaps(con)
    assert gaps(con) == [
        ("2026-08-20 02", 2, 1, 1, "likely_zero"),
        ("2026-08-25 12", 1, 111, 113, "short_outage"),
        ("2026-09-08 00", 24, 123, 1, "long_outage"),
        ("2026-10-04 01", 2, 1, 1, "likely_zero"),
    ]
    # every missing hour belongs to exactly one gap
    n = con.execute("select count(*), count(distinct (sensing_date, hourday)) "
                    "from gap_hours").fetchone()
    assert n == (29, 29)


def test_quiet_threshold(con):
    # the quiet gaps have 1 person on each side: quiet at 1, not at 0
    cp.build_gaps(con, quiet_max=1)
    assert [g[4] for g in gaps(con)][::3] == ["likely_zero", "likely_zero"]
    cp.build_gaps(con, quiet_max=0)
    assert [g[4] for g in gaps(con)] == ["short_outage"] * 2 + ["long_outage", "short_outage"]


def test_impute_recovers_the_pattern(con):
    cp.build_gaps(con)
    cp.impute(con, "gap_hours")
    rows = con.execute("""
        select sensing_date, hourday, est, method from imputed_gap_hours
        order by sensing_date, hourday
    """).fetchall()
    assert len(rows) == 29
    assert all(est == pattern(h) for _, h, est, _ in rows)
    assert {m for *_, m in rows} == {"same weekday +/- 4 weeks"}
    day = sum(est for d, _, est, _ in rows if str(d) == "2026-09-08")
    assert day == sum(pattern(h) for h in range(24))


def test_impute_backtest_never_uses_the_same_day(con):
    # Leave Monday 15:00 only on 2026-08-31 (set to 10,000) and a week later.
    # If the same day's reading leaked in, the median would be ~5,058.
    con.execute("""delete from counts where hourday = 15 and isodow(sensing_date) = 1
                   and sensing_date not in (DATE '2026-08-31', DATE '2026-09-07')""")
    con.execute("""update counts set pedestriancount = 10000
                   where sensing_date = DATE '2026-08-31' and hourday = 15""")
    cp.impute(con, "counts")
    est = dict(con.execute("""select sensing_date::varchar, est from imputed_counts
                              where hourday = 15 and isodow(sensing_date) = 1""").fetchall())
    assert est == {"2026-08-31": pattern(15), "2026-09-07": 10000}


def test_impute_falls_back_to_whole_snapshot(con):
    # A Tuesday hour with no reading 1-4 weeks either side.
    con.execute("""delete from counts where hourday = 9 and isodow(sensing_date) = 2
                   and sensing_date between DATE '2026-08-04' and DATE '2026-09-29'
                   and sensing_date <> DATE '2026-08-04'""")
    con.execute("""create table target as select 7 as location_id,
                   DATE '2026-09-01' as sensing_date, 9 as hourday""")
    cp.impute(con, "target")
    est, method = con.execute("select est, method from imputed_target").fetchone()
    # 2026-08-04 is 4 weeks before, so the near estimate still finds it
    assert (est, method) == (pattern(9), "same weekday +/- 4 weeks")
    con.execute("delete from counts where sensing_date = DATE '2026-08-04' and hourday = 9")
    cp.impute(con, "target")
    est, method = con.execute("select est, method from imputed_target").fetchone()
    # 2026-10-06 is the only Tuesday 09:00 left in the whole snapshot
    assert (est, method) == (pattern(9), "same weekday, whole snapshot")


def test_zero_signature(con):
    sig = {b: (hours, missing) for b, hours, missing, _ in cp.zero_signature(con)}
    # only the isolated daytime hour has both neighbours published; the
    # two-hour gaps and the DST gap don't, and the dark day doesn't either
    assert sum(m for _, m in sig.values()) == 1
    assert sig["51+"][1] == 1
    assert set(sig) <= {"0-1", "2", "3-5", "6-10", "11-20", "21-50", "51+"}
