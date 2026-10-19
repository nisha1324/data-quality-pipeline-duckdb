"""A small synthetic snapshot with known, planted defects.

Sensors 1, 11, 2, 3 and 99 report hourly from 2026-09-01 to 2026-10-10.
Planted defects:
- ids are built like the portal's (location_id || hourday || yyyymmdd), so
  sensor 1 at 10:00-19:00 collides with sensor 11 at 00:00-09:00;
- sensor 99 has no row in sensors and no sensor name;
- the newest day (2026-10-10) only has hours 0-3;
- sensor 2 misses one hour (2026-09-05 03:00) and a whole day (2026-09-20),
  and its installation date is after its first count;
- sensor 3 stops after 2026-09-15;
- sensors 11 and 50 share a name, and sensor 50 (outdoor, active) has no counts.
Not a defect: like the real feed, there is no 02:00 row on 2026-10-04, when
daylight saving starts in Melbourne and that hour doesn't exist.
"""
import duckdb
import pytest

from dq.load import create_views

PARAMS = {"snapshot": "test", "reference_ts": "2026-10-11T02:39+11:00",
          "reference_date": "2026-10-11"}


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
        create table counts as
        with s(location_id, sensor_name, last_day) as (values
            (1, 'One_T', DATE '2026-10-10'), (11, 'Dup_T', DATE '2026-10-10'),
            (2, 'Two_T', DATE '2026-10-10'), (3, 'Three_T', DATE '2026-09-15'),
            (99, NULL, DATE '2026-10-10')),
        g as (
            select s.location_id, s.sensor_name, d.day::date as sensing_date, h.hourday
            from s
            cross join lateral generate_series(DATE '2026-09-01', s.last_day, interval 1 day) d(day)
            cross join range(24) h(hourday)
        )
        select (location_id::varchar || hourday::varchar
                || strftime(sensing_date, '%Y%m%d'))::bigint as id,
               location_id, sensing_date, hourday,
               hourday * 3 as direction_1, location_id as direction_2,
               hourday * 3 + location_id as pedestriancount, sensor_name
        from g
        where not (sensing_date = DATE '2026-10-10' and hourday > 3)
          and not (sensing_date = DATE '2026-10-04' and hourday = 2)
          and not (location_id = 2 and sensing_date = DATE '2026-09-05' and hourday = 3)
          and not (location_id = 2 and sensing_date = DATE '2026-09-20')
    """)
    c.execute("""
        create table sensors as select * from (values
            (1, 'One sensor', 'One_T', DATE '2020-01-01', 'Outdoor', 'A', -37.81, 144.96),
            (11, 'Eleven sensor', 'Dup_T', DATE '2020-01-01', 'Outdoor', 'A', -37.81, 144.96),
            (2, 'Two sensor', 'Two_T', DATE '2026-09-03', 'Outdoor', 'A', -37.81, 144.96),
            (3, 'Three sensor', 'Three_T', DATE '2020-01-01', 'Outdoor', 'A', -37.81, 144.96),
            (50, 'Fifty sensor', 'Dup_T', DATE '2020-01-01', 'Outdoor', 'A', -37.81, 144.96),
            (60, 'Library lift', 'Lib_T', DATE '2020-01-01', 'Indoor', 'A', -37.81, 144.96)
        ) t(location_id, sensor_description, sensor_name, installation_date,
            location_type, status, latitude, longitude)
    """)
    create_views(c)
    yield c
    c.close()
