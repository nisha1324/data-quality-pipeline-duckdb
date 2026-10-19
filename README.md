# Data-quality pipeline on DuckDB: can we trust today's foot-traffic numbers?

> **Status: in progress.** Part 1 (download, check engine, the first 21 checks and report) and Part 2 (what the missing hours are and what they do to the numbers) are done. See the [roadmap](#roadmap).

City teams, retailers, precinct associations and event planners use pedestrian
counts to judge how busy the city is: whether a campaign or event brought people
in, which streets recovered, where to staff a shop. Each of those reports is only
as good as the data under it. This project builds a small **data-quality testing
pipeline** (the same idea as dbt tests or Great Expectations, written from scratch
on DuckDB) and runs it on the City of Melbourne's live pedestrian sensor feed to
answer one business question:

**Which foot-traffic reports can the business trust today, and which need a caveat
or shouldn't be used?**

## Data

| Dataset | What it is | Size (snapshot 2026-10-18) |
|---|---|---|
| [Pedestrian Counting System (counts per hour)](https://data.melbourne.vic.gov.au/explore/dataset/pedestrian-counting-system-monthly-counts-per-hour/information/) | One row per sensor per hour; a rolling two-year window, refreshed daily | 1,626,702 rows (2024-10-19 to 2026-10-18), 26.4 MB Parquet |
| [Pedestrian Counting System – Sensor Locations](https://data.melbourne.vic.gov.au/explore/dataset/pedestrian-counting-system-sensor-locations/information/) | One row per sensor: name, description, indoor/outdoor, status, coordinates | 134 sensors |

Source: City of Melbourne open data portal, fetched through its keyless Explore API
(no sign-up). **Licence:** the portal's [Data Policy](https://data.melbourne.vic.gov.au/pages/about-our-data/)
says its open data is provided under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The sensor locations dataset is labelled CC BY in its metadata; the counts dataset
has no licence field set, so the portal-wide policy applies. Data © City of Melbourne.

The feed changes every day, so `scripts/download_data.py` saves each download as a
dated snapshot (named after the portal's processing time) with a `source.json`
recording the portal's metadata, row counts, sizes and SHA-256 hashes. The figures
below are for snapshot **`2026-10-18T1539Z`** ([`results/snapshot.json`](results/snapshot.json));
a fresh download will give slightly different numbers.

## Approach

```
download_data.py ──► data/raw/<snapshot>/  counts.parquet, sensors.parquet, source.json
                              │
run_checks.py ──► DuckDB (in memory) ──► checks/pedestrian.yml (21 checks)
                              │
                              └──► results/DQ_REPORT.md, dq_results.csv, snapshot.json
```

- **Checks are declared in YAML** ([`checks/pedestrian.yml`](checks/pedestrian.yml)), like
  dbt tests. The engine ([`dq/engine.py`](dq/engine.py)) has eight check types:
  `not_null`, `unique`, `accepted_range`, `accepted_values`, `relationships` (foreign key),
  `expression`, `freshness` and custom `sql`.
- **Every check compiles to one SQL query that returns the failing records.** The same
  query that counts a failure can be run to inspect it, and the report shows the first
  five failing records and the query for every failure.
- **Severity and tolerance:** `error` means the data is wrong for the reports that rely on
  it; `warn` means usable with a known caveat. A check can tolerate a small failure rate
  (hour coverage tolerates 1%).
- **Dimensions:** uniqueness, validity, consistency, completeness, referential integrity
  and freshness.
- **Freshness is measured against the portal's own processing time**, converted to
  Melbourne time, not against the clock of the machine running the checks, so a rerun
  of an old snapshot gives the same answer.
- **Tests:** 30 offline tests. One builds a synthetic snapshot with planted defects
  (colliding ids, an orphan sensor, a missing hour, a dark day, a partial newest day, a
  stopped sensor, a shared sensor name) and checks that the real suite catches each with
  the exact count and passes everything else. Six more check the Part 2 gap
  classes and estimates on a synthetic sensor with planted gaps.

## Results so far (snapshot 2026-10-18)

**21 checks: 12 pass, 8 warn, 1 error.** Full report: [`results/DQ_REPORT.md`](results/DQ_REPORT.md).

*Correction (2026-10-19):* the first version of this report counted 98,004 missing
sensor-hours out of 1,724,352. 199 of them were 02:00 on the two days daylight saving
started (2025-10-05 and 2026-10-04). That hour doesn't exist in Melbourne time, so
it can't have a reading. The expected-hours view now skips local times that don't
exist, and a test covers it.

| Check | Result | What it means for a report |
|---|---|---|
| `counts_id_unique` | ⚠️ 135,070 rows (8.30%) share their `id` with another row | The `id` is the sensor id, hour and date run together with no separator (`counts_id_format` passes on every row), so sensor 1 at 10:00 and sensor 11 at 00:00 both get `110` + date. Joining or de-duplicating on `id` silently merges different sensors. The real key, sensor + date + hour, is unique (`counts_grain_unique` passes). |
| `counts_sensor_exists` | ❌ 20,472 rows (1.26%) belong to sensors 28 and 78, which aren't in the sensor table | These sensors stopped reporting on 2025-11-20 and 2026-02-13 and their rows have no sensor name either. Any report that joins counts to sensors (maps, precincts, named-street rankings) silently drops them; city-wide totals keep them. The two give different answers for the same period. |
| `counts_latest_day_complete` | ⚠️ 2026-10-18 has 354 rows, 15.6% of a usual day (median 2,273) | The newest day only covers 00:00–04:00 (hours 0–3). A "yesterday vs last week" tile built on the latest date would show a fall of about 84% that isn't real. |
| `counts_hour_coverage` | ⚠️ 97,805 of 1,724,153 expected sensor-hours missing (5.67%) | Hourly averages and daily totals are computed over different numbers of hours per sensor. Part 2 looks at where the gaps are and what they do to the numbers. |
| `counts_full_day_gaps` | ⚠️ 458 sensor-days (0.64%) with no reading at all | A dark day looks like a quiet day in that sensor's daily total. |
| `counts_sensor_still_reporting` + `sensors_active_outdoor_reporting` | ⚠️ 3 sensors silent for 7+ days; one of them (108, William St – Little Lonsdale St) is still marked active | Sensor 108 last reported on 2026-07-08 (102 days before the newest data) but its status is still `A`. The status field can't be used to decide which sensors to include. |
| `sensors_name_unique` | ⚠️ 3 sensor names (`BirBridge_T`, `FliS_T`, `Lat526_T`) are each used by 2 sensors | Filtering or joining on the name adds two sensors together. Use `location_id`. |

What passes: one row per sensor per hour, no missing keys or counts, hours 0–23, no
negative counts, directional counts that add up to the total on every row, coordinates
inside a box around the City of Melbourne, no counts before a sensor's installation date, and fresh
data (the newest date is one day before the portal's processing date).

Two things the checks surfaced about the documentation rather than the data: the
counts dataset's description says it's updated monthly and that directional readings
aren't included, but it is refreshed daily and both directions are there. The 34
indoor sensors in the sensor table have no counts in this dataset at all, so a
"sensors with no data" view built from the sensor table would list them; that's
scope, not a fault, and the active-sensor check only looks at outdoor sensors.

**What this means for the business (first pass):** city-wide trends up to the day
before the newest date are usable with a caveat about missing hours; anything labelled
"yesterday" from the newest date isn't; sensor-level and map reports must join on
`location_id`, never on `id` or the sensor name, and should say that two retired
sensors aren't on the map. Part 3 turns this into a scorecard per report.

## Part 2: what the missing hours are (snapshot 2026-10-18)

Full write-up: [`results/completeness.md`](results/completeness.md) (made by
[`scripts/completeness.py`](scripts/completeness.py); logic in
[`dq/completeness.py`](dq/completeness.py)).

Part 1 found 97,805 missing sensor-hours (5.67%). Each run of consecutive missing
hours for one sensor is treated as one gap and classed by its length and by the
readings just before and after it:

| Type of gap | Missing hours | Share |
|---|---|---|
| **Likely zero:** under 24 h, at most 5 people an hour on both sides | 42,027 | 43.0% |
| **Short outage:** under 24 h, next to real activity | 43,434 | 44.4% |
| **Long outage:** 24 h or more | 12,344 | 12.6% |

**The feed mostly doesn't publish zero counts**, even though the dataset description
says *"where no pedestrians have passed underneath a sensor during an hour, a count of
zero will be shown"*. It has 143 hours with a count of 0 but 33,999 with a count of 1. For hours whose neighbours were both published, the
chance of being missing is 43.9% when the busier neighbour had 0–1 people and
0.02% when it had more than 50. An outage doesn't depend on how busy the street is;
a dropped zero does. 68% of all missing hours fall between midnight and 6 am.

![Missing share by neighbouring count](results/charts/02_missing_vs_neighbours.png)

What the gaps do to the numbers people report:

- **Hourly profiles are too high overnight.** A plain average over the published
  hours overstates 01:00–04:00 by 9.0–15.3% (03:00: 28.1 vs 24.4 people per
  sensor-hour once likely zeros count as 0). Between 09:00 and 17:00 the effect is
  at most 0.16%.
- **Totals are short, and by a different amount each month.** Outage hours hold an
  estimated 1.43% of all pedestrians over two years, but by month it ranges from
  0.20% to 4.06%. Nov 2025 to Jun 2026 are all 2.6–4.1% short because sensor 1,
  **Bourke Street Mall (North)**, was dark from 10 Nov 2025 to 30 Jun 2026 (5,570
  hours). That one outage is 57.8% of the estimated missing pedestrians.
- **Month-on-month change is distorted when a sensor comes back.** July 2026 vs
  June looks like **+15.0%** on the published totals; it is +11.2% gap-filled and
  +13.0% like-for-like (the 26 sensors with no outage in either month). Part of the
  "July jump" is Bourke Street Mall coming back online.
- **Street rankings: one sensor reads about half its real total.** Lygon St (East)
  (sensor 37) has 826 outage gaps, most often 9 hours from 14:00 (218 times), so
  its published total is short by an estimated 46.9%.

![Monthly undercount](results/charts/03_monthly_undercount.png)

How far to trust the estimates: outage hours are filled with the median of the same
sensor, weekday and hour 1–4 weeks either side. Run the same way on published
hours, the median error is 13.4% for a single hour and 7.7% for a whole sensor-day
(90th percentile 27.6%, bias −1.1%). That is good enough to size a shortfall in a
total, not to stand in for one sensor on one day. Moving the "quiet" threshold from
2 to 10 people an hour shifts hours between likely zero and short outage, but the
city-wide shortfall stays at 1.43–1.44%.

Also found in this part: the hour daylight saving ends (02:00 happens twice) is
stored as one row, and 03:00 that night holds about half the usual count (3,357 vs
6,021 / 6,265 the weeks either side in 2025), with no documented reason.

**What this means for the business (Part 2):**
- Hourly or night-time-economy reports should count a missing quiet hour as 0 (or
  use medians), otherwise late-night footfall looks 9–15% busier than it is.
- Monthly and year-on-year totals should be gap-filled or like-for-like (the same
  sensors in both periods). The published month-on-month change differed from the
  gap-filled one by up to 3.8 points, because one sensor went dark or came back.
- Street rankings and sensor-level reports need a coverage column, and sensors with
  heavy outages (Lygon St (East), Bourke Street Mall (North) for Nov 2025 – Jun 2026)
  should be flagged or left out.
- For the data owner: publishing zero hours as 0 (as the description says), or a
  "sensor online" flag, would end the guesswork between "nobody walked past" and
  "the sensor was off".

## How to run

```bash
uv venv && source .venv/bin/activate        # or python -m venv .venv
pip install -r requirements.txt
python scripts/download_data.py             # ~26 MB, a few minutes; saves data/raw/<snapshot>/
python scripts/run_checks.py                # writes results/; add --fail-on error to gate a pipeline
python scripts/completeness.py              # Part 2: gap analysis, ~1 minute
pytest                                      # offline, no download needed
```

`run_checks.py` uses the newest snapshot in `data/raw/` unless you pass `--snapshot`.

## Project structure

```
checks/pedestrian.yml     the check suite (YAML)
dq/engine.py              check types → SQL, severity, tolerance, suite validation
dq/load.py                loads a snapshot into DuckDB, freshness reference date, shared views
dq/report.py              Markdown and CSV output
dq/completeness.py        Part 2: gap classes, estimates for outage hours, the zero signature
scripts/download_data.py  snapshot download with metadata and hashes
scripts/run_checks.py     runs the suite and writes results/
scripts/completeness.py   Part 2 report, CSVs and charts
tests/                    engine tests, the suite on a synthetic snapshot with planted defects, gap tests
results/                  DQ_REPORT.md, dq_results.csv, snapshot.json, completeness.md + CSVs, charts/
```

## Roadmap

- [x] **Part 1:** snapshot download, YAML check engine on DuckDB, 21 checks, Markdown report, offline tests
- [x] **Part 2:** completeness deep dive: where the missing hours are (time of day, sensors), whether they look like outages or unpublished zero counts, and how much they move hourly averages and daily totals (plus a fix for the daylight-saving hour in Part 1)
- [ ] **Part 3:** a trust scorecard: map each business report (city-wide trend, yesterday's footfall, street ranking, sensor map, hourly profile) to the checks it depends on, and generate "trusted / use with caveat / don't use" for every snapshot
- [ ] **Part 4:** snapshot-to-snapshot checks (row-count drift, rewritten history) and a scheduled run
- [ ] Final write-up: TL;DR, recommendations with owners, limitations
