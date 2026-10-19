"""Load a downloaded snapshot into DuckDB and work out the check parameters."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json

import duckdb

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"


def latest_snapshot(raw: Path = RAW) -> Path:
    snaps = sorted(p for p in raw.iterdir() if (p / "source.json").exists())
    if not snaps:
        raise SystemExit("no snapshot found; run scripts/download_data.py first")
    return snaps[-1]


def snapshot_params(source: dict) -> dict:
    """Reference time for freshness checks: when the portal last processed
    the counts, as a date in the data's own time zone (Melbourne)."""
    counts = source["files"]["counts"]
    processed = datetime.fromisoformat(counts["data_processed"])
    local = processed.astimezone(ZoneInfo(counts["timezone"]))
    return {
        "snapshot": source["snapshot"],
        "reference_ts": local.isoformat(timespec="minutes"),
        "reference_date": local.date().isoformat(),
    }


def create_views(con: duckdb.DuckDBPyConnection,
                 tz: str = "Australia/Melbourne") -> None:
    """Derived views the completeness checks share.

    expected_sensor_hours: one row for every hour each sensor should have
    reported, from its first to its last reading. The newest day is left out
    because it is still being filled (a separate check covers it), and a
    sensor that stopped is covered by its own check, not counted here.
    Counts are in local time, so the hour that doesn't exist when daylight
    saving starts (02:00 on the first Sunday of October in Melbourne) isn't
    expected: a local time that doesn't exist changes when converted to UTC
    and back.
    """
    con.execute(f"""
        create or replace view expected_sensor_hours as
        with bounds as (
            select max(sensing_date) as newest from counts
        ),
        span as (
            select location_id, min(sensing_date) as first_day,
                   least(max(sensing_date), (select newest from bounds) - 1) as last_day
            from counts group by location_id
        )
        select s.location_id, d.day::date as sensing_date, h.hourday
        from span s
        cross join lateral generate_series(s.first_day, s.last_day, interval 1 day) as d(day)
        cross join range(24) as h(hourday)
        where timezone('{tz}', timezone('{tz}', d.day + to_hours(h.hourday::int)))
              = d.day + to_hours(h.hourday::int)
    """)


def load_snapshot(snapshot_dir: Path, db_path: str | Path = ":memory:"):
    """Return (connection, params, source) with the snapshot loaded as the
    tables counts and sensors. The geometry columns are dropped: both tables
    already carry the coordinates the checks need (sensors) or don't need
    them (counts)."""
    source = json.loads((snapshot_dir / "source.json").read_text())
    con = duckdb.connect(str(db_path))
    for table in ("counts", "sensors"):
        path = snapshot_dir / source["files"][table]["file"]
        con.execute(f"create or replace table {table} as "
                    f"select * exclude (location) from read_parquet('{path}')")
    create_views(con, source["files"]["counts"]["timezone"])
    params = snapshot_params(source)
    first, last = con.execute(
        "select min(sensing_date), max(sensing_date) from counts").fetchone()
    params.update(first_date=first.isoformat(), last_date=last.isoformat())
    return con, params, source
