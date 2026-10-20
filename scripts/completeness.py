"""Part 2: completeness deep dive.

Where are the missing sensor-hours, are they outages or unpublished zero
counts, and how much do they move hourly averages and daily/monthly totals?

Usage:
    python scripts/completeness.py                 # newest snapshot in data/raw/
    python scripts/completeness.py --snapshot data/raw/2026-10-18T1539Z

Outputs (in results/): completeness.md, completeness_hourly.csv,
completeness_monthly.csv, completeness_sensors.csv and charts 01-03 in
results/charts/.
"""
from pathlib import Path
import argparse
import csv
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq import completeness as cp  # noqa: E402
from dq.load import latest_snapshot, load_snapshot  # noqa: E402
from dq.report import md_table  # noqa: E402

RESULTS = ROOT / "results"
CHARTS = RESULTS / "charts"

# Reference palette (categorical slots 1-3 in fixed order) and chart ink.
COLOR = {"likely_zero": "#2a78d6", "short_outage": "#eb6834", "long_outage": "#1baf7a"}
LABEL = {"likely_zero": "Likely zero (quiet on both sides)",
         "short_outage": "Short outage (< 24 h, next to activity)",
         "long_outage": "Long outage (24 h or more)"}
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"


def pct(a, b) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def class_summary(con) -> dict:
    rows = con.execute("""
        select gap_class, count(*) as gaps, sum(hours) as hours
        from gaps group by 1
    """).fetchall()
    return {c: (g, int(h)) for c, g, h in rows}


def city_totals(con) -> None:
    """Daily city-wide totals over the days the expected hours cover (the
    newest, partial day is left out): published vs gap-filled."""
    con.execute("""
        create or replace table daily as
        with pub as (
            select c.sensing_date, sum(c.pedestriancount) as published
            from counts c
            where c.sensing_date < (select max(sensing_date) from counts)
            group by 1
        ),
        est as (
            select sensing_date, sum(est) as missing_est from gap_est group by 1
        )
        select p.sensing_date, p.published, coalesce(e.missing_est, 0) as missing_est
        from pub p left join est e using (sensing_date)
    """)


def write_csv(path: Path, header: list[str], rows: list) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.tick_params(colors=MUTED, labelcolor=INK2, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def chart_by_hour(by_hour: list, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 5.2), facecolor=SURFACE)
    style(ax)
    bottom = [0] * 24
    for c in cp.CLASSES:
        vals = [row[c] for row in by_hour]
        ax.bar(range(24), vals, bottom=bottom, width=0.8, color=COLOR[c],
               edgecolor=SURFACE, linewidth=2, label=LABEL[c])
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_xticks(range(24), [f"{h:02d}" for h in range(24)])
    ax.set_xlabel("Hour of day (Melbourne time)", color=INK2)
    ax.set_ylabel("Missing sensor-hours", color=INK2)
    ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:,.0f}"))
    ax.legend(frameon=False, loc="upper center", labelcolor=INK2, fontsize=9)
    total = sum(bottom)
    night = sum(bottom[:6])
    fig.suptitle(f"{100 * night / total:.0f}% of missing hours fall between midnight and 6 am",
                 x=0.06, ha="left", color=INK, fontsize=13, fontweight="bold")
    ax.set_title("Missing sensor-hours by hour of day and type of gap, "
                 "all sensors, two years", loc="left", color=INK2, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def chart_signature(sig: list, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8.5, 4.8), facecolor=SURFACE)
    style(ax)
    labels = [s[0] for s in sig]
    rates = [100 * s[2] / s[1] for s in sig]
    bars = ax.bar(labels, rates, width=0.7, color=COLOR["likely_zero"])
    for b, r in zip(bars, rates):
        ax.text(b.get_x() + b.get_width() / 2, r + 0.8, f"{r:.1f}%" if r >= 0.1 else f"{r:.2f}%",
                ha="center", va="bottom", color=INK2, fontsize=9)
    ax.set_ylim(0, max(rates) * 1.15)
    ax.set_xlabel("Pedestrians in the busier of the two neighbouring hours", color=INK2)
    ax.set_ylabel("Share of hours that are missing (%)", color=INK2)
    fig.suptitle("An hour is far more likely to be missing when the street is empty",
                 x=0.07, ha="left", color=INK, fontsize=13, fontweight="bold")
    ax.set_title("Hours whose previous and next hour were both published; "
                 "an outage wouldn't depend on traffic", loc="left", color=INK2, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def chart_monthly(monthly: list, path: Path, span: tuple | None = None) -> None:
    """span: (first month, last month, label) drawn as a bracket over the bars."""
    fig, ax = plt.subplots(figsize=(10, 4.8), facecolor=SURFACE)
    style(ax)
    labels = [m[0] for m in monthly]
    under = [100 * m[3] for m in monthly]
    ax.bar(range(len(labels)), under, width=0.7, color=COLOR["short_outage"])
    ax.set_xticks(range(len(labels)), [l[2:] for l in labels], rotation=90)
    ax.set_xlabel("Month (first and last are part months)", color=INK2)
    ax.set_ylabel("Pedestrians missing from the total (%)", color=INK2)
    top = max(range(len(under)), key=under.__getitem__)
    ax.annotate(f"{under[top]:.1f}%", (top, under[top]), xytext=(0, 4),
                textcoords="offset points", ha="center", color=INK2, fontsize=9)
    ax.set_ylim(0, max(under) * 1.15)
    if span:
        i0, i1 = labels.index(span[0]), labels.index(span[1])
        y = max(under[i0:i1 + 1]) * 1.07
        ax.plot([i0 - 0.35, i1 + 0.35], [y, y], color=INK2, linewidth=1)
        for i in (i0, i1):
            ax.plot([i + (-0.35 if i == i0 else 0.35)] * 2, [y, y * 0.97], color=INK2, linewidth=1)
        ax.text(i0 - 0.35, y * 1.015, span[2], ha="left", va="bottom", color=INK2, fontsize=9)
        ax.set_ylim(0, y * 1.12)
    fig.suptitle("Outages take a different share out of each month's total",
                 x=0.06, ha="left", color=INK, fontsize=13, fontweight="bold")
    ax.set_title("Estimated pedestrians lost to outage hours, as a share of the "
                 "gap-filled monthly city-wide total", loc="left", color=INK2, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Completeness deep dive.")
    parser.add_argument("--snapshot", type=Path, help="snapshot folder (default: newest)")
    args = parser.parse_args()
    snapshot = args.snapshot or latest_snapshot()
    con, params, source = load_snapshot(snapshot)

    expected = con.execute("select count(*) from expected_sensor_hours").fetchone()[0]
    cp.build_gaps(con)
    classes = class_summary(con)
    missing = sum(h for _, h in classes.values())
    cp.estimate_gaps(con)
    city_totals(con)

    zeros, ones = con.execute("""
        select count(*) filter (where pedestriancount = 0),
               count(*) filter (where pedestriancount = 1) from counts
    """).fetchone()
    sig = cp.zero_signature(con)

    # Missing hours by hour of day and class.
    raw = dict(((h, c), n) for h, c, n in con.execute(
        "select hourday, gap_class, count(*) from gap_hours group by all").fetchall())
    by_hour = [{"hour": h, **{c: raw.get((h, c), 0) for c in cp.CLASSES}} for h in range(24)]

    # Hourly profile: mean per published sensor-hour vs with likely zeros as 0.
    profile = con.execute("""
        with pub as (
            select hourday, sum(pedestriancount) as total, count(*) as n
            from counts
            where sensing_date < (select max(sensing_date) from counts)
            group by 1
        ),
        z as (
            select hourday, count(*) as zeros from gap_hours
            where gap_class = 'likely_zero' group by 1
        )
        select p.hourday, p.n, coalesce(z.zeros, 0),
               p.total / p.n as published_mean,
               p.total / (p.n + coalesce(z.zeros, 0)) as zero_filled_mean
        from pub p left join z using (hourday) order by 1
    """).fetchall()

    # Daily and monthly totals.
    days = con.execute("""
        select count(*),
               quantile_cont(missing_est / (published + missing_est), 0.5),
               quantile_cont(missing_est / (published + missing_est), 0.9),
               max(missing_est / (published + missing_est)),
               arg_max(sensing_date, missing_est / (published + missing_est)),
               count(*) filter (where missing_est / (published + missing_est) > 0.05),
               sum(missing_est) / sum(published + missing_est)
        from daily
    """).fetchone()
    monthly = con.execute("""
        select strftime(sensing_date, '%Y-%m') as month, sum(published),
               sum(missing_est),
               sum(missing_est) / sum(published + missing_est) as under
        from daily group by 1 order by 1
    """).fetchall()
    # Month-on-month change, published vs gap-filled (whole months only).
    whole = monthly[1:-1]
    mom = []
    for prev, cur in zip(whole, whole[1:]):
        p_chg = cur[1] / prev[1] - 1
        f_chg = (cur[1] + cur[2]) / (prev[1] + prev[2]) - 1
        mom.append((cur[0], p_chg, f_chg, f_chg - p_chg))
    worst_mom = max(mom, key=lambda m: abs(m[3]))

    # Like-for-like: the same month pair over sensors with no outage hour in
    # either month (the retail idea of comparing like-for-like stores).
    m_prev, m_cur = whole[[m[0] for m in whole].index(worst_mom[0]) - 1][0], worst_mom[0]
    lfl = con.execute("""
        with o as (
            select distinct location_id from gap_est
            where gap_class <> 'likely_zero'
              and strftime(sensing_date, '%Y-%m') in (?, ?)
        ),
        both_months as (
            select location_id from counts
            where strftime(sensing_date, '%Y-%m') in (?, ?)
            group by 1 having count(distinct strftime(sensing_date, '%Y-%m')) = 2
        )
        select count(distinct c.location_id),
               sum(c.pedestriancount) filter (where strftime(c.sensing_date, '%Y-%m') = ?),
               sum(c.pedestriancount) filter (where strftime(c.sensing_date, '%Y-%m') = ?)
        from counts c join both_months using (location_id)
        where c.location_id not in (select location_id from o)
    """, [m_prev, m_cur, m_prev, m_cur, m_prev, m_cur]).fetchone()
    lfl_chg = lfl[2] / lfl[1] - 1

    # The sensors behind most of the estimated shortfall: their outage gaps,
    # the longest one, and the most common shape (start hour and length).
    drivers = con.execute("""
        with t as (
            select location_id, sum(est) as est from gap_est
            where gap_class <> 'likely_zero' group by 1
        ),
        o as (
            select * from gaps where gap_class <> 'likely_zero'
        ),
        shape as (
            select location_id, hour(start_hour) as start_h, hours, count(*) as n,
                   row_number() over (partition by location_id
                                      order by count(*) desc, hours desc, hour(start_hour)) as r
            from o group by location_id, hour(start_hour), hours
        ),
        longest as (
            select location_id, count(*) as gaps,
                   arg_max(start_hour, (hours, start_hour)) as l_start,
                   arg_max(end_hour, (hours, start_hour)) as l_end,
                   max(hours) as l_hours
            from o group by 1
        )
        select t.location_id,
               coalesce(s.sensor_description, '(no sensor record)'),
               t.est / (select sum(est) from t) as share,
               l.gaps, l.l_start, l.l_end, l.l_hours, sh.start_h, sh.hours, sh.n
        from t join longest l using (location_id)
        join shape sh on sh.location_id = t.location_id and sh.r = 1
        left join sensors s on s.location_id = t.location_id
        order by t.est desc limit 3
    """).fetchall()

    no_history = con.execute("""
        select count(*), count(distinct location_id) from gap_est
        where no_history and gap_class <> 'likely_zero'
    """).fetchone()
    outage_methods = con.execute("""
        select i.method, count(*) from imputed_gap_hours i
        join gap_hours g using (location_id, sensing_date, hourday)
        where g.gap_class <> 'likely_zero' group by 1 order by 1 nulls last
    """).fetchall()

    # Per-sensor view.
    sensors = con.execute("""
        with e as (select location_id, count(*) as expected
                   from expected_sensor_hours group by 1),
        g as (select location_id,
                     count(*) filter (where gap_class = 'likely_zero') as zero_h,
                     count(*) filter (where gap_class = 'short_outage') as short_h,
                     count(*) filter (where gap_class = 'long_outage') as long_h,
                     sum(est) as missing_est
              from gap_est group by 1),
        p as (select location_id, sum(pedestriancount) as published,
                     any_value(sensor_name) as sensor_name
              from counts
              where sensing_date < (select max(sensing_date) from counts)
              group by 1)
        select e.location_id, coalesce(s.sensor_description, p.sensor_name, '(no sensor record)'),
               e.expected, g.zero_h, g.short_h, g.long_h,
               (g.zero_h + g.short_h + g.long_h) / e.expected as missing_share,
               g.missing_est / (p.published + g.missing_est) as undercount
        from e join g using (location_id) join p using (location_id)
        left join sensors s using (location_id)
        order by missing_share desc, e.location_id
    """).fetchall()

    # Back-test the estimate on published hours (the same-day value is never used).
    cp.impute(con, "counts")
    backtest = con.execute("""
        with h as (
            select c.pedestriancount as n, i.est
            from counts c join imputed_counts i using (location_id, sensing_date, hourday)
            where i.est is not null
        ),
        d as (
            select c.location_id, c.sensing_date, sum(c.pedestriancount) as n,
                   sum(i.est) as est, count(i.est) as k
            from counts c join imputed_counts i using (location_id, sensing_date, hourday)
            group by 1, 2 having count(*) = 24 and count(i.est) = 24
        )
        select (select quantile_cont(abs(est - n) / n, 0.5) from h where n >= 20),
               (select count(*) from h where n >= 20),
               (select quantile_cont(abs(est - n) / n, 0.5) from d where n > 0),
               (select quantile_cont(abs(est - n) / n, 0.9) from d where n > 0),
               (select sum(est) / sum(n) - 1 from d where n > 0),
               (select count(*) from d where n > 0)
    """).fetchone()

    # Sensitivity of the split and of the city-wide undercount to quiet_max.
    sens = []
    for q in (2, cp.QUIET_MAX, 10):
        cp.build_gaps(con, quiet_max=q)
        cp.estimate_gaps(con)
        city_totals(con)
        cs = class_summary(con)
        under = con.execute(
            "select sum(missing_est) / sum(published + missing_est) from daily").fetchone()[0]
        sens.append((q, cs["likely_zero"][1], cs["short_outage"][1], cs["long_outage"][1], under))

    # DST end: the repeated hour.
    dst_end = con.execute("""
        with d(day) as (
            select distinct sensing_date from counts
            where month(sensing_date) = 4 and isodow(sensing_date) = 7
              and day(sensing_date) <= 7
        )
        select d.day,
               sum(c.pedestriancount) filter (where c.sensing_date = d.day),
               sum(c.pedestriancount) filter (where c.sensing_date = d.day - 7),
               sum(c.pedestriancount) filter (where c.sensing_date = d.day + 7)
        from d join counts c on c.hourday = 3
            and c.sensing_date in (d.day - 7, d.day, d.day + 7)
        group by 1 order by 1
    """).fetchall()

    # ---- outputs ----
    RESULTS.mkdir(exist_ok=True)
    CHARTS.mkdir(exist_ok=True)
    write_csv(RESULTS / "completeness_hourly.csv",
              ["hour", "missing_likely_zero", "missing_short_outage", "missing_long_outage",
               "published_hours", "published_mean", "zero_filled_mean"],
              [[b["hour"], b["likely_zero"], b["short_outage"], b["long_outage"],
                p[1], f"{p[3]:.2f}", f"{p[4]:.2f}"] for b, p in zip(by_hour, profile)])
    write_csv(RESULTS / "completeness_monthly.csv",
              ["month", "published", "missing_est", "undercount_pct"],
              [[m[0], int(m[1]), round(m[2]), f"{100 * m[3]:.2f}"] for m in monthly])
    write_csv(RESULTS / "completeness_sensors.csv",
              ["location_id", "sensor", "expected_hours", "likely_zero_hours",
               "short_outage_hours", "long_outage_hours", "missing_pct", "undercount_pct"],
              [[s[0], s[1], s[2], s[3], s[4], s[5], f"{100 * s[6]:.2f}",
                f"{100 * s[7]:.2f}"] for s in sensors])
    chart_by_hour(by_hour, CHARTS / "01_missing_by_hour.png")
    chart_signature(sig, CHARTS / "02_missing_vs_neighbours.png")
    top = drivers[0]
    span = None
    if top[6] >= 24 * 30:  # a sensor dark for a month or more
        span = (f"{top[4]:%Y-%m}", f"{top[5]:%Y-%m}",
                f"{top[1]} (sensor {top[0]}) dark {top[4]:%d %b %Y} to {top[5]:%d %b %Y}")
    chart_monthly(monthly, CHARTS / "03_monthly_undercount.png", span)

    biggest = max(profile, key=lambda p: p[3] / p[4])
    day_profile = [p for p in profile if 9 <= p[0] <= 17]
    day_gap = max(p[3] / p[4] - 1 for p in day_profile)
    out = [
        "# Completeness deep dive",
        "",
        f"Snapshot `{params['snapshot']}` ({params['first_date']} to {params['last_date']}; "
        "the newest, partial day is left out). Generated by `scripts/completeness.py`.",
        "",
        "## 1. What the missing hours are",
        "",
        f"{missing:,} of {expected:,} expected sensor-hours are missing ({pct(missing, expected)}). "
        "Each run of consecutive missing hours for one sensor is one gap, classed by its "
        f"length and by the published counts just before and after it (quiet = at most "
        f"{cp.QUIET_MAX} people an hour):",
        "",
        md_table(["Type of gap", "Gaps", "Missing hours", "Share of missing hours"],
                 [[LABEL[c], f"{classes[c][0]:,}", f"{classes[c][1]:,}",
                   pct(classes[c][1], missing)] for c in cp.CLASSES]),
        "",
        "## 2. Evidence that quiet gaps are unpublished zeros",
        "",
        f"- The feed has **{zeros:,}** hours with a count of 0 but **{ones:,}** with a count of 1. "
        "Where a street is quiet enough to log exactly one person in an hour that often, an "
        "hour with nobody should be about as common, so most zero hours aren't being "
        "published.",
        "- For every expected hour whose previous and next hour were both published, the "
        "chance that it is missing rises steeply as the street gets quieter. An outage "
        "(power, network, a broken sensor) doesn't depend on how many people walk past; a "
        "dropped zero does.",
        "",
        md_table(["Busier neighbouring hour (people)", "Hours", "Missing", "Missing %"],
                 [[s[0], f"{s[1]:,}", f"{s[2]:,}", f"{100 * s[2] / s[1]:.2f}%"] for s in sig]),
        "",
        "![Missing share by neighbouring count](charts/02_missing_vs_neighbours.png)",
        "",
        "![Missing hours by hour of day](charts/01_missing_by_hour.png)",
        "",
        "## 3. Effect on hourly averages",
        "",
        "Average pedestrians per sensor-hour, by hour of day: over published hours only "
        "(what a naive `avg()` gives) and with likely-zero gaps counted as 0. Outage hours "
        "are left out of both.",
        "",
        md_table(["Hour", "Published only", "Likely zeros as 0", "Published-only overstates by"],
                 [[f"{p[0]:02d}:00", f"{p[3]:.1f}", f"{p[4]:.1f}",
                   f"{100 * (p[3] / p[4] - 1):.1f}%"] for p in profile]),
        "",
        f"The biggest overstatement is at {biggest[0]:02d}:00 "
        f"({100 * (biggest[3] / biggest[4] - 1):.1f}%); between 09:00 and 17:00 it is at most "
        f"{100 * day_gap:.2f}%.",
        "",
        "## 4. Effect on daily and monthly totals",
        "",
        "Likely zeros don't change a total. Outage hours do: each is filled with the median "
        "of the same sensor at the same hour on the same weekday 1–4 weeks either side, or, "
        "for long outages with no such readings, the same weekday and hour over the whole "
        "snapshot.",
        "",
        md_table(["Estimate used for outage hours", "Hours"],
                 [[m or "none: no published reading for that sensor, weekday and hour "
                   "in two years (counted as 0)", f"{n:,}"] for m, n in outage_methods]),
        "",
        f"The {no_history[0]:,} outage hours with no history are at {no_history[1]} sensors that "
        "have never published that weekday and hour, which itself points to a count of 0.",
        "",
        f"- Over the whole snapshot, outage hours hold an estimated **{100 * days[6]:.2f}%** "
        "of all pedestrians.",
        f"- Across {days[0]:,} days, the city-wide daily total is short by a median of "
        f"{100 * days[1]:.2f}% (90th percentile {100 * days[2]:.2f}%); "
        f"{days[5]:,} days are short by more than 5%; the worst is {days[4]} at "
        f"{100 * days[3]:.1f}%.",
        f"- By month the shortfall ranges from {100 * min(m[3] for m in monthly):.2f}% to "
        f"{100 * max(m[3] for m in monthly):.2f}%. The biggest gap between the published and "
        f"the gap-filled month-on-month change is {worst_mom[0]}: "
        f"{100 * worst_mom[1]:+.1f}% published vs {100 * worst_mom[2]:+.1f}% gap-filled "
        f"({100 * worst_mom[3]:+.1f} points).",
        f"- Like-for-like, over the {lfl[0]} sensors with no outage hour in {m_prev} or "
        f"{m_cur}, the change is {100 * lfl_chg:+.1f}%. A month-on-month figure from the "
        "raw totals mixes real change with sensors going dark or coming back.",
        "",
        "Most of the shortfall comes from a few sensors:",
        "",
        md_table(["Sensor", "Description", "Share of estimated missing pedestrians",
                  "Outage gaps", "Longest outage", "Most common outage"],
                 [[d[0], d[1], f"{100 * d[2]:.1f}%", f"{d[3]:,}",
                   f"{d[6]:,} h ({d[4]:%Y-%m-%d %H:%M} to {d[5]:%Y-%m-%d %H:%M})",
                   f"{d[8]:,} h from {d[7]:02d}:00, {d[9]:,} times" if d[9] > 1 else "n/a"]
                  for d in drivers]),
        "",
        md_table(["Month", "Published", "Estimated missing", "Short by"],
                 [[m[0], f"{int(m[1]):,}", f"{round(m[2]):,}", f"{100 * m[3]:.2f}%"]
                  for m in monthly]),
        "",
        "![Monthly undercount](charts/03_monthly_undercount.png)",
        "",
        "### How good is the estimate?",
        "",
        "The same method was run on every *published* hour (it never uses the same day's "
        "reading), and compared with what was actually counted:",
        "",
        f"- one hour (hours with 20+ people, {backtest[1]:,} of them): median error "
        f"{100 * backtest[0]:.1f}%;",
        f"- a whole sensor-day (all 24 hours estimated, {backtest[5]:,} sensor-days): median "
        f"error {100 * backtest[2]:.1f}%, 90th percentile {100 * backtest[3]:.1f}%, overall bias "
        f"{100 * backtest[4]:+.1f}%.",
        "",
        "So the estimate is good enough to size the shortfall in a total, not to stand in "
        "for a specific sensor's specific day.",
        "",
        "## 5. Sensors with the most missing hours",
        "",
        md_table(["Sensor", "Description", "Missing", "Likely zero h", "Short outage h",
                  "Long outage h", "Total short by"],
                 [[s[0], s[1], f"{100 * s[6]:.1f}%", f"{s[3]:,}", f"{s[4]:,}", f"{s[5]:,}",
                   f"{100 * s[7]:.1f}%"] for s in sensors[:12]]),
        "",
        "Full list: `completeness_sensors.csv`.",
        "",
        "## 6. Sensitivity to the quiet threshold",
        "",
        md_table(["Quiet = at most", "Likely zero h", "Short outage h", "Long outage h",
                  "City-wide total short by"],
                 [[f"{s[0]} people/h", f"{s[1]:,}", f"{s[2]:,}", f"{s[3]:,}",
                   f"{100 * s[4]:.2f}%"] for s in sens]),
        "",
        "The threshold moves hours between `likely_zero` and `short_outage`, but those "
        "hours are quiet either way, so the city-wide shortfall barely moves.",
        "",
        "## 7. Also noticed: the hour daylight saving ends",
        "",
        "On the first Sunday of April, 02:00–02:59 happens twice. The feed has one row per "
        "hour that day, and 03:00 holds about half the usual count:",
        "",
        md_table(["Day", "03:00 city-wide", "Week before", "Week after"],
                 [[d[0], f"{int(d[1]):,}", f"{int(d[2]):,}", f"{int(d[3]):,}"]
                  for d in dst_end]),
        "",
        "The cause isn't documented. Night-time figures for those two days should be "
        "treated with care.",
        "",
    ]
    (RESULTS / "completeness.md").write_text("\n".join(out))
    print("\n".join(out[:20]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
