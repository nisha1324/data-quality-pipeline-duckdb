"""Part 4: what changed between two snapshots of the same feed.

The counts are a rolling window that the portal rebuilds every day. A report
published from an earlier download can only be reproduced if a later download
agrees with it on the days both cover. This module loads two snapshots side
by side so the drift suite (checks/drift.yml) can compare them with the same
engine as the single-snapshot checks:

    old_counts, new_counts      the hourly counts of each snapshot
    old_sensors, new_sensors    the sensor table of each snapshot
    old_counts_columns, ...     column names and types, for schema drift
    overlap_rows                every sensor-hour on the days both snapshots
                                cover, matched on sensor + date + hour, with
                                the count in each (null = no row)

The overlap is split in two. The old snapshot's newest day was still being
filled when it was downloaded, so new rows there are expected (part
`filling`). Anything that changes on the days before it (part `settled`)
rewrites history that reports may already have used.
"""
from datetime import datetime
from pathlib import Path
import json

import duckdb

from dq.engine import Check, Result, run_check

TABLES = ("counts", "sensors")
SCHEMA_CHECKS = ("drift_counts_schema_same", "drift_sensors_schema_same")


def snapshot_pair(raw: Path) -> tuple[Path, Path]:
    """The two newest downloaded snapshots in raw/, oldest first."""
    snaps = sorted(p for p in raw.iterdir() if (p / "source.json").exists())
    if len(snaps) < 2:
        raise SystemExit("need two snapshots; run scripts/download_data.py on another day")
    return snaps[-2], snaps[-1]


def load_pair(old_dir: Path, new_dir: Path) -> tuple[duckdb.DuckDBPyConnection, dict]:
    """Return (connection, params) with both snapshots loaded."""
    con = duckdb.connect()
    names = {}
    for side, folder in (("old", old_dir), ("new", new_dir)):
        source = json.loads((folder / "source.json").read_text())
        names[side] = source["snapshot"]
        for table in TABLES:
            path = folder / source["files"][table]["file"]
            con.execute(f"create table {side}_{table} as "
                        f"select * exclude (location) from read_parquet('{path}')")
    create_column_views(con)
    return con, pair_params(con, names["old"], names["new"])


def create_column_views(con: duckdb.DuckDBPyConnection) -> None:
    for side in ("old", "new"):
        for table in TABLES:
            con.execute(f"""
                create or replace view {side}_{table}_columns as
                select column_name, column_type from (describe {side}_{table})
            """)


def create_overlap_view(con: duckdb.DuckDBPyConnection) -> None:
    """Needs the same key and count columns in both snapshots, so it is only
    created once the schema checks have passed."""
    con.execute("""
        create or replace view overlap_rows as
        with bounds as (
            select greatest((select min(sensing_date) from old_counts),
                            (select min(sensing_date) from new_counts)) as first_day,
                   (select max(sensing_date) from old_counts) as old_newest
        )
        select location_id, sensing_date, hourday,
               o.pedestriancount as old_count, n.pedestriancount as new_count,
               case when sensing_date = bounds.old_newest then 'filling'
                    else 'settled' end as part
        from old_counts o
        full join new_counts n using (location_id, sensing_date, hourday)
        cross join bounds
        where sensing_date between bounds.first_day and bounds.old_newest
    """)


def run_drift(con: duckdb.DuckDBPyConnection, checks: list[Check],
              params: dict) -> tuple[list[Result], list[Check]]:
    """Run the schema checks first. Only if they pass are the snapshots
    compared row by row; otherwise the other checks are returned as skipped
    (a query on a renamed column would fail, not report)."""
    schema = [c for c in checks if c.id in SCHEMA_CHECKS]
    rest = [c for c in checks if c.id not in SCHEMA_CHECKS]
    results = [run_check(con, c, params) for c in schema]
    if any(r.status == "error" for r in results):
        return results, rest
    create_overlap_view(con)
    return results + [run_check(con, c, params) for c in rest], []


def pair_params(con: duckdb.DuckDBPyConnection, old: str, new: str) -> dict:
    old_first, old_last, new_first, new_last = con.execute("""
        select (select min(sensing_date) from old_counts),
               (select max(sensing_date) from old_counts),
               (select min(sensing_date) from new_counts),
               (select max(sensing_date) from new_counts)
    """).fetchone()
    fmt = "%Y-%m-%dT%H%MZ"
    hours = (datetime.strptime(new, fmt) - datetime.strptime(old, fmt)).total_seconds() / 3600
    return {
        "old_snapshot": old, "new_snapshot": new, "hours_apart": round(hours, 1),
        "old_first": old_first.isoformat(), "old_last": old_last.isoformat(),
        "new_first": new_first.isoformat(), "new_last": new_last.isoformat(),
    }


def daily_drift(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Per day of the overlap with any difference, oldest first: rows and
    pedestrians in each snapshot, and how many sensor-hours appeared,
    disappeared or changed their count."""
    return con.execute("""
        select sensing_date, part,
               count(old_count) as old_rows, count(new_count) as new_rows,
               coalesce(sum(old_count), 0) as old_pedestrians,
               coalesce(sum(new_count), 0) as new_pedestrians,
               count(*) filter (where old_count is null) as appeared,
               count(*) filter (where new_count is null) as disappeared,
               count(*) filter (where old_count <> new_count) as changed
        from overlap_rows
        group by sensing_date, part
        having count(*) filter (where old_count is distinct from new_count) > 0
        order by sensing_date
    """).fetchall()


def first_vs_final(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """For each hour the old snapshot had already published on its newest
    day: sensors and pedestrians as first published, how many of those
    counts changed, the same sensors' total now, and every sensor now."""
    return con.execute("""
        select hourday,
               count(old_count) as first_rows,
               sum(old_count) as first_pedestrians,
               count(*) filter (where old_count <> new_count) as revised,
               sum(new_count) filter (where old_count is not null) as same_sensors_now,
               count(new_count) as rows_now,
               sum(new_count) as pedestrians_now
        from overlap_rows
        where part = 'filling'
        group by hourday
        having count(old_count) > 0
        order by hourday
    """).fetchall()
