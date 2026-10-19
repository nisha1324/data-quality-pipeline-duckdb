"""Part 2: where the missing sensor-hours are, what they probably are, and
what they do to the numbers people report.

Every missing sensor-hour (from the `expected_sensor_hours` view) is grouped
into a *gap*: a run of consecutive missing hours for one sensor. Each gap is
classed by its length and by the published counts just before and after it:

- `long_outage`:  the gap lasts 24 hours or more (the sensor was off);
- `likely_zero`:  a shorter gap where the readings on both sides are at most
                  `quiet_max` people an hour. The street was close to empty,
                  so the most likely reading is 0, which the feed doesn't
                  publish (see `zero_signature`);
- `short_outage`: any other shorter gap, next to real activity.

Outage hours are filled with an estimate: the median of the same sensor at
the same hour on the same weekday, 1 to 4 weeks before and after
(`IMPUTE_OFFSETS`), or, if none of those were published (long outages), the
median of the same sensor, weekday and hour over the whole snapshot.
`likely_zero` hours count as 0.
"""
import duckdb

QUIET_MAX = 5
LONG_HOURS = 24
IMPUTE_OFFSETS = (-28, -21, -14, -7, 7, 14, 21, 28)
CLASSES = ("likely_zero", "short_outage", "long_outage")


def build_gaps(con: duckdb.DuckDBPyConnection, quiet_max: int = QUIET_MAX,
               long_hours: int = LONG_HOURS) -> None:
    """Create the tables gap_hours (one row per missing sensor-hour, with its
    gap's id and class) and gaps (one row per gap)."""
    # Every hour of each sensor's sequence: the expected hours plus any
    # published ones outside them (the newest day), in time order. Using the
    # position in this sequence rather than clock arithmetic keeps a gap
    # across the missing 02:00 on the day daylight saving starts in one piece.
    con.execute("""
        create or replace temp table _seq as
        with keys as (
            select location_id, sensing_date, hourday from expected_sensor_hours
            union
            select location_id, sensing_date, hourday from counts
        )
        select k.location_id, k.sensing_date, k.hourday, c.pedestriancount as n,
               row_number() over w as pos,
               lag(c.pedestriancount) over w as prev_n,
               lead(c.pedestriancount) over w as next_n
        from keys k
        left join counts c using (location_id, sensing_date, hourday)
        window w as (partition by k.location_id order by k.sensing_date, k.hourday)
    """)
    con.execute("""
        create or replace temp table _missing as
        select location_id, sensing_date, hourday, pos, prev_n, next_n,
               pos - row_number() over (partition by location_id order by pos) as island
        from _seq where n is null
    """)
    con.execute(f"""
        create or replace table gaps as
        select row_number() over (order by location_id, min(pos)) as gap_id,
               location_id,
               arg_min(sensing_date + to_hours(hourday::int), pos) as start_hour,
               arg_max(sensing_date + to_hours(hourday::int), pos) as end_hour,
               count(*) as hours,
               arg_min(prev_n, pos) as before_n,
               arg_max(next_n, pos) as after_n,
               case
                   when count(*) >= {int(long_hours)} then 'long_outage'
                   when arg_min(prev_n, pos) <= {int(quiet_max)}
                        and arg_max(next_n, pos) <= {int(quiet_max)} then 'likely_zero'
                   else 'short_outage'
               end as gap_class,
               island
        from _missing group by location_id, island
    """)
    con.execute("""
        create or replace table gap_hours as
        select m.location_id, m.sensing_date, m.hourday, g.gap_id, g.gap_class
        from _missing m join gaps g using (location_id, island)
    """)
    con.execute("alter table gaps drop column island")
    con.execute("drop table _seq; drop table _missing")


def _offsets_sql() -> str:
    return ", ".join(str(d) for d in IMPUTE_OFFSETS)


def impute(con: duckdb.DuckDBPyConnection, target: str) -> None:
    """Create table imputed_<target> with an estimate `est` (and `method`) for
    every (location_id, sensing_date, hourday) row of the table `target`,
    from published counts only. The same-day reading is never used, so this
    also works as a back-test when `target` is the published counts."""
    con.execute(f"""
        create or replace table imputed_{target} as
        with shifted as (
            select c.location_id, c.hourday, c.sensing_date + o.d as target_date,
                   c.pedestriancount
            from counts c cross join (select unnest([{_offsets_sql()}]) as d) o
        ),
        near as (
            select t.location_id, t.sensing_date, t.hourday,
                   median(s.pedestriancount) as est
            from {target} t
            join shifted s on s.location_id = t.location_id
                          and s.hourday = t.hourday
                          and s.target_date = t.sensing_date
            group by all
        ),
        typical as (
            select location_id, isodow(sensing_date) as dow, hourday,
                   median(pedestriancount) as est
            from counts group by all
        )
        select t.location_id, t.sensing_date, t.hourday,
               coalesce(n.est, ty.est) as est,
               case when n.est is not null then 'same weekday +/- 4 weeks'
                    when ty.est is not null then 'same weekday, whole snapshot'
               end as method
        from {target} t
        left join near n using (location_id, sensing_date, hourday)
        left join typical ty on ty.location_id = t.location_id
                            and ty.dow = isodow(t.sensing_date)
                            and ty.hourday = t.hourday
    """)


def zero_signature(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """For every expected hour whose neighbours on both sides were published,
    how often it is missing, by how busy the busier neighbour was. An outage
    doesn't care how busy the street is; an unpublished zero only happens
    when it is nearly empty."""
    return con.execute("""
        with seq as (
            select e.location_id, e.sensing_date, e.hourday,
                   c.pedestriancount as n
            from expected_sensor_hours e
            left join counts c using (location_id, sensing_date, hourday)
        ),
        x as (
            select s.n is null as missing,
                   greatest(b.pedestriancount, a.pedestriancount) as busier
            from seq s
            join counts b on b.location_id = s.location_id
                and b.sensing_date + to_hours(b.hourday::int)
                    = s.sensing_date + to_hours(s.hourday::int) - interval 1 hour
            join counts a on a.location_id = s.location_id
                and a.sensing_date + to_hours(a.hourday::int)
                    = s.sensing_date + to_hours(s.hourday::int) + interval 1 hour
        )
        select case when busier <= 1 then '0-1' when busier <= 2 then '2'
                    when busier <= 5 then '3-5' when busier <= 10 then '6-10'
                    when busier <= 20 then '11-20' when busier <= 50 then '21-50'
                    else '51+' end as neighbours,
               count(*) as hours, sum(missing::int) as missing,
               min(busier) as lo
        from x group by 1 order by lo
    """).fetchall()
