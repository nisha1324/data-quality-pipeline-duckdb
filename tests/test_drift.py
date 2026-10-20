"""Part 4: the drift suite on a synthetic pair of snapshots.

The old snapshot has sensors 1, 2 and 3 from 2026-09-01 to 2026-09-10, with
the newest day (09-10) published up to 03:00 only, and no row for sensor 1 on
09-07 at 04:00. The new snapshot was taken a day later: the window has moved
on by one day (09-02 to 09-11) and 09-10 is complete.

Planted drift in the new snapshot (`planted=True`):
- settled history: sensor 2 on 09-05 at 07:00 is gone, sensor 3 on 09-06 at
  08:00 has a different count, and the missing sensor 1 on 09-07 at 04:00
  arrived late;
- the filling day: sensor 1 at 03:00 was revised up, and sensor 2 at 03:00
  disappeared;
- the sensor table: sensor 3's status changed from A to I.
"""
from pathlib import Path

import duckdb

from dq import drift
from dq.engine import load_suite

SUITE = load_suite(Path(__file__).resolve().parents[1] / "checks" / "drift.yml")


def build(planted: bool, new_first: str = "2026-09-02", new_last: str = "2026-09-11",
          rename: bool = False, filled: bool = True):
    con = duckdb.connect()
    for side, first, last in (("old", "2026-09-01", "2026-09-10"),
                              ("new", new_first, new_last)):
        partial = "2026-09-10" if side == "old" or not filled else last
        con.execute(f"""
            create table {side}_counts as
            select location_id, d.day::date as sensing_date, h.hourday,
                   10 * location_id + h.hourday as pedestriancount
            from (values (1), (2), (3)) s(location_id)
            cross join generate_series(DATE '{first}', DATE '{last}', interval 1 day) d(day)
            cross join range(24) h(hourday)
            where not (d.day = DATE '{partial}' and h.hourday > 3)
              and not (d.day > DATE '{partial}')
        """)
        if side == "old":
            con.execute("""delete from old_counts where location_id = 1
                           and sensing_date = DATE '2026-09-07' and hourday = 4""")
        con.execute(f"""
            create table {side}_sensors as select * from (values
                (1, 'One_T', 'A'), (2, 'Two_T', 'A'), (3, 'Three_T', 'A')
            ) t(location_id, sensor_name, status)
        """)
    if not planted:
        con.execute("""delete from new_counts where location_id = 1
                       and sensing_date = DATE '2026-09-07' and hourday = 4""")
    else:
        con.execute("""
            delete from new_counts
            where (location_id = 2 and sensing_date = DATE '2026-09-05' and hourday = 7)
               or (location_id = 2 and sensing_date = DATE '2026-09-10' and hourday = 3);
            update new_counts set pedestriancount = pedestriancount + 4
            where (location_id = 3 and sensing_date = DATE '2026-09-06' and hourday = 8)
               or (location_id = 1 and sensing_date = DATE '2026-09-10' and hourday = 3);
            update new_sensors set status = 'I' where location_id = 3;
        """)
    if rename:
        con.execute("alter table new_counts rename pedestriancount to total_of_directions")
    drift.create_column_views(con)
    return con


def run(con):
    results, skipped = drift.run_drift(con, SUITE, {})
    return {r.check.id: (r.status, r.failing) for r in results}, [c.id for c in skipped]


def test_clean_pair_passes():
    statuses, skipped = run(build(planted=False))
    assert skipped == []
    assert statuses == {c.id: ("pass", 0) for c in SUITE}


def test_planted_drift_is_caught_with_exact_counts():
    statuses, _ = run(build(planted=True))
    assert statuses == {
        "drift_counts_schema_same": ("pass", 0),
        "drift_sensors_schema_same": ("pass", 0),
        "drift_feed_moves_forward": ("pass", 0),
        "drift_window_keeps_length": ("pass", 0),
        "drift_settled_rows_kept": ("error", 1),
        "drift_settled_counts_unchanged": ("error", 1),
        "drift_settled_no_late_rows": ("warn", 1),
        # the revised hour and the one that disappeared
        "drift_published_hours_final": ("warn", 2),
        "drift_filling_day_completed": ("pass", 0),
        # sensor 3 as it was and as it is now
        "drift_sensor_table_same": ("warn", 2),
    }


def test_daily_drift_and_first_vs_final():
    con = build(planted=True)
    drift.create_overlap_view(con)
    # a full day of the three sensors: 24 * (10 + 20 + 30) + 3 * (0 + 1 + ... + 23)
    full = 24 * 60 + 3 * 276
    days = {d[0].isoformat(): d[1:] for d in drift.daily_drift(con)}
    # part, rows old/new, pedestrians old/new, appeared, disappeared, changed
    assert days == {
        "2026-09-05": ("settled", 72, 71, full, full - 27, 0, 1, 0),
        "2026-09-06": ("settled", 72, 72, full, full + 4, 0, 0, 1),
        "2026-09-07": ("settled", 71, 72, full - 14, full, 1, 0, 0),
        "2026-09-10": ("filling", 12, 71, 60 + 63 + 66 + 69, full - 23 + 4, 60, 1, 1),
    }
    # hour, sensors and pedestrians first published, revised, the same
    # sensors now, sensors now, pedestrians now
    hours = {h[0]: h[1:] for h in drift.first_vs_final(con)}
    assert hours == {0: (3, 60, 0, 60, 3, 60), 1: (3, 63, 0, 63, 3, 63),
                     2: (3, 66, 0, 66, 3, 66), 3: (3, 69, 1, 50, 2, 50)}


def test_window_that_drops_more_than_it_adds_warns():
    statuses, _ = run(build(planted=False, new_first="2026-09-03"))
    assert statuses["drift_window_keeps_length"] == ("warn", 1)


def test_feed_rolled_back_is_an_error():
    statuses, _ = run(build(planted=False, new_first="2026-08-31", new_last="2026-09-09",
                            filled=False))
    assert statuses["drift_feed_moves_forward"] == ("error", 1)


def test_partial_day_that_never_fills_warns():
    con = build(planted=False)
    con.execute("""delete from new_counts
                   where sensing_date = DATE '2026-09-10' and hourday > 3""")
    statuses, _ = run(con)
    assert statuses["drift_filling_day_completed"] == ("warn", 1)


def test_schema_change_stops_before_row_checks():
    statuses, skipped = run(build(planted=True, rename=True))
    assert statuses == {"drift_counts_schema_same": ("error", 2),
                        "drift_sensors_schema_same": ("pass", 0)}
    assert len(skipped) == len(SUITE) - 2
