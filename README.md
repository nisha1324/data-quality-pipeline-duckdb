# Data-quality pipeline on DuckDB: can we trust today's foot-traffic numbers?

> **Status: in progress.** Part 1 (download, check engine, the first 21 checks and report) is done. See the [roadmap](#roadmap).

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
- **Tests:** 24 offline tests. One builds a synthetic snapshot with planted defects
  (colliding ids, an orphan sensor, a missing hour, a dark day, a partial newest day, a
  stopped sensor, a shared sensor name) and checks that the real suite catches each with
  the exact count and passes everything else.

## Results so far (snapshot 2026-10-18)

**21 checks: 12 pass, 8 warn, 1 error.** Full report: [`results/DQ_REPORT.md`](results/DQ_REPORT.md).

| Check | Result | What it means for a report |
|---|---|---|
| `counts_id_unique` | ⚠️ 135,070 rows (8.30%) share their `id` with another row | The `id` is the sensor id, hour and date run together with no separator (`counts_id_format` passes on every row), so sensor 1 at 10:00 and sensor 11 at 00:00 both get `110` + date. Joining or de-duplicating on `id` silently merges different sensors. The real key, sensor + date + hour, is unique (`counts_grain_unique` passes). |
| `counts_sensor_exists` | ❌ 20,472 rows (1.26%) belong to sensors 28 and 78, which aren't in the sensor table | These sensors stopped reporting on 2025-11-20 and 2026-02-13 and their rows have no sensor name either. Any report that joins counts to sensors (maps, precincts, named-street rankings) silently drops them; city-wide totals keep them. The two give different answers for the same period. |
| `counts_latest_day_complete` | ⚠️ 2026-10-18 has 354 rows, 15.6% of a usual day (median 2,273) | The newest day only covers 00:00–04:00 (hours 0–3). A "yesterday vs last week" tile built on the latest date would show a fall of about 84% that isn't real. |
| `counts_hour_coverage` | ⚠️ 98,004 of 1,724,352 expected sensor-hours missing (5.68%) | Hourly averages and daily totals are computed over different numbers of hours per sensor. Part 2 looks at where the gaps are and what they do to the numbers. |
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

## How to run

```bash
uv venv && source .venv/bin/activate        # or python -m venv .venv
pip install -r requirements.txt
python scripts/download_data.py             # ~26 MB, a few minutes; saves data/raw/<snapshot>/
python scripts/run_checks.py                # writes results/; add --fail-on error to gate a pipeline
pytest                                      # offline, no download needed
```

`run_checks.py` uses the newest snapshot in `data/raw/` unless you pass `--snapshot`.

## Project structure

```
checks/pedestrian.yml     the check suite (YAML)
dq/engine.py              check types → SQL, severity, tolerance, suite validation
dq/load.py                loads a snapshot into DuckDB, freshness reference date, shared views
dq/report.py              Markdown and CSV output
scripts/download_data.py  snapshot download with metadata and hashes
scripts/run_checks.py     runs the suite and writes results/
tests/                    engine tests + the suite on a synthetic snapshot with planted defects
results/                  DQ_REPORT.md, dq_results.csv, snapshot.json
```

## Roadmap

- [x] **Part 1:** snapshot download, YAML check engine on DuckDB, 21 checks, Markdown report, offline tests
- [ ] **Part 2:** completeness deep dive: where the missing hours are (time of day, sensors), whether they look like outages or unpublished zero counts, and how much they move hourly averages and daily totals
- [ ] **Part 3:** a trust scorecard: map each business report (city-wide trend, yesterday's footfall, street ranking, sensor map, hourly profile) to the checks it depends on, and generate "trusted / use with caveat / don't use" for every snapshot
- [ ] **Part 4:** snapshot-to-snapshot checks (row-count drift, rewritten history) and a scheduled run
- [ ] Final write-up: TL;DR, recommendations with owners, limitations
