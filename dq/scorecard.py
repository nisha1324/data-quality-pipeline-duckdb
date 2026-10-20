"""Part 3: a trust scorecard for the reports people build on the feed.

Each business report in checks/reports.yml lists the rules that can
downgrade it. A rule looks at either

- a check result from the Part 1 suite (fires when the check ends with one
  of the listed statuses), or
- a *measure* computed over the report's own time window (fires when the
  value is above or below a threshold). Measures are what a check can't
  say on its own: a sensor that went dark two years ago fails a check but
  doesn't touch a ranking of the last 28 days.

A report's verdict is the worst verdict among the rules that fired:
trusted < caveat < dont_use. If a measure can't be computed on a snapshot
(for example, no previous month), its rule gives a caveat: a report that
can't be checked isn't trusted.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import duckdb
import yaml

VERDICTS = ("trusted", "caveat", "dont_use")
LABEL = {"trusted": "✅ trusted", "caveat": "⚠️ use with caveat", "dont_use": "❌ don't use"}


@dataclass
class Measure:
    value: float | None
    detail: str = ""


@dataclass
class Rule:
    kind: str            # "check" or "measure"
    ref: str             # check id or measure name
    verdict: str
    reason: str
    fix: str = ""
    statuses: tuple = ()
    above: float | None = None
    below: float | None = None

    def threshold(self) -> str:
        if self.kind == "check":
            return "status " + " or ".join(self.statuses)
        return f"> {self.above:g}" if self.above is not None else f"< {self.below:g}"


@dataclass
class Report:
    id: str
    name: str
    question: str
    window: str
    rules: list[Rule] = field(default_factory=list)


@dataclass
class Outcome:
    rule: Rule
    fired: bool
    value: str           # what was observed, for the report
    reason: str = ""


@dataclass
class Verdict:
    report: Report
    verdict: str
    outcomes: list[Outcome]

    @property
    def fired(self) -> list[Outcome]:
        """The rules that fired, worst first. When two tiers of the same
        measure or check fire, only the worse one is kept."""
        fired = sorted((o for o in self.outcomes if o.fired),
                       key=lambda o: -VERDICTS.index(o.rule.verdict))
        seen, out = set(), []
        for o in fired:
            if (o.rule.kind, o.rule.ref) not in seen:
                seen.add((o.rule.kind, o.rule.ref))
                out.append(o)
        return out


def _rule(raw: dict, where: str) -> Rule:
    raw = dict(raw)
    if ("check" in raw) == ("measure" in raw):
        raise ValueError(f"{where}: a rule needs exactly one of check or measure")
    verdict = raw.pop("verdict")
    if verdict not in VERDICTS[1:]:
        raise ValueError(f"{where}: verdict must be caveat or dont_use")
    if "check" in raw:
        statuses = tuple(raw.pop("status"))
        if not set(statuses) <= {"warn", "error"}:
            raise ValueError(f"{where}: status must be warn and/or error")
        return Rule("check", raw.pop("check"), verdict, raw.pop("reason").strip(),
                    raw.pop("fix", "").strip(), statuses=statuses)
    if ("above" in raw) == ("below" in raw):
        raise ValueError(f"{where}: a measure rule needs exactly one of above or below")
    return Rule("measure", raw.pop("measure"), verdict, raw.pop("reason").strip(),
                raw.pop("fix", "").strip(), above=raw.pop("above", None),
                below=raw.pop("below", None))


def load_reports(path: str | Path, check_ids: set[str],
                 measure_names: set[str]) -> tuple[list[Report], list[str]]:
    """Read the report definitions and check every rule points at a check or
    measure that exists, so a typo can't silently make a report trusted."""
    spec = yaml.safe_load(Path(path).read_text())
    sets = {name: [_rule(r, f"rule set {name}") for r in rules]
            for name, rules in spec.get("rule_sets", {}).items()}
    reports, seen = [], set()
    for raw in spec["reports"]:
        if raw["id"] in seen:
            raise ValueError(f"duplicate report id: {raw['id']}")
        seen.add(raw["id"])
        rules = []
        for name in raw.get("include", []):
            if name not in sets:
                raise ValueError(f"{raw['id']}: unknown rule set {name}")
            rules += sets[name]
        rules += [_rule(r, raw["id"]) for r in raw.get("rules", [])]
        for r in rules:
            known = check_ids if r.kind == "check" else measure_names
            if r.ref not in known:
                raise ValueError(f"{raw['id']}: unknown {r.kind} {r.ref}")
        reports.append(Report(raw["id"], raw["name"], raw["question"].strip(),
                              raw["window"], rules))
    return reports, [n.strip() for n in spec.get("notes", [])]


def evaluate(reports: list[Report], results: dict, measures: dict[str, Measure]) -> list[Verdict]:
    """results: check id -> engine Result. measures: name -> Measure."""
    verdicts = []
    for rep in reports:
        outcomes = []
        for r in rep.rules:
            if r.kind == "check":
                res = results[r.ref]
                fired = res.status in r.statuses
                value = f"{res.status}: {res.failing:,} failing ({res.fail_pct:.2f}%)"
                reason = r.reason.format(failing=res.failing, fail_pct=res.fail_pct)
            else:
                m = measures[r.ref]
                if m.value is None:
                    outcomes.append(Outcome(
                        Rule(r.kind, r.ref, "caveat", r.reason, r.fix, above=r.above,
                             below=r.below),
                        True, "not available",
                        f"`{r.ref}` can't be measured on this snapshot ({m.detail})"))
                    continue
                fired = (r.above is not None and m.value > r.above) or \
                        (r.below is not None and m.value < r.below)
                shown = f"{m.value:,}" if isinstance(m.value, int) else f"{m.value:,.2f}"
                value = shown + (f" ({m.detail})" if m.detail else "")
                reason = r.reason.format(value=m.value, detail=m.detail)
            outcomes.append(Outcome(r, fired, value, reason if fired else ""))
        worst = max((VERDICTS.index(o.rule.verdict) for o in outcomes if o.fired), default=0)
        verdicts.append(Verdict(rep, VERDICTS[worst], outcomes))
    return verdicts


# ---- measures -------------------------------------------------------------

def windows(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[date, date]]:
    """The time windows the reports look at, all relative to the snapshot:
    the newest day, the last complete day (the one before), the 28 days
    ending on the last complete day, the last two whole months and the same
    month a year earlier."""
    newest = con.execute("select max(sensing_date) from counts").fetchone()[0]
    last = newest - timedelta(days=1)
    month_end = last if (last + timedelta(days=1)).day == 1 else last.replace(day=1) - timedelta(days=1)
    month = (month_end.replace(day=1), month_end)
    prev_end = month[0] - timedelta(days=1)
    ya0 = month[0].replace(year=month[0].year - 1)
    return {
        "newest_day": (newest, newest),
        "last_day": (last, last),
        "last_28_days": (last - timedelta(days=27), last),
        "month": month,
        "prev_month": (prev_end.replace(day=1), prev_end),
        "year_ago_month": (ya0, (ya0 + timedelta(days=32)).replace(day=1) - timedelta(days=1)),
    }


def _rows_pct_of_usual(con, day: date) -> float | None:
    rows, usual = con.execute("""
        with daily as (select sensing_date, count(*) as n from counts group by 1)
        select (select n from daily where sensing_date = $day),
               (select median(n) from daily
                where sensing_date between $day - 28 and $day - 1)
    """, {"day": day}).fetchone()
    return 100.0 * (rows or 0) / usual if usual else None


def _sensor_label(row) -> str:
    return f"{row[1] or '(no sensor record)'} ({row[0]})"


def _compare(con, before: tuple[date, date], after: tuple[date, date],
             prefix: str) -> dict[str, Measure]:
    """How far outages move the change between two periods (published vs
    gap-filled totals), the larger of the two periods' outage shares, and how
    far a change in the sensor mix moves it (published vs like-for-like: only
    sensors that report in both periods)."""
    p = {"a0": before[0], "a1": before[1], "b0": after[0], "b1": after[1]}
    label = f"{before[0]:%Y-%m} vs {after[0]:%Y-%m}"
    rows = con.execute("""
        with pub as (
            select sensing_date >= $b0 as cur, sum(pedestriancount) as n from counts
            where sensing_date between $a0 and $a1 or sensing_date between $b0 and $b1
            group by 1
        ),
        est as (
            select sensing_date >= $b0 as cur, sum(est) as n from gap_est
            where sensing_date between $a0 and $a1 or sensing_date between $b0 and $b1
            group by 1
        )
        select pub.cur, pub.n, coalesce(est.n, 0) from pub left join est using (cur)
    """, p).fetchall()
    by = {cur: (n, e) for cur, n, e in rows}
    out = {}
    if set(by) == {False, True}:
        (pp, pe), (cp_, ce) = by[False], by[True]
        published = cp_ / pp - 1
        filled = (cp_ + ce) / (pp + pe) - 1
        out[f"{prefix}_outage_pct"] = Measure(
            100.0 * max(pe / (pp + pe), ce / (cp_ + ce)),
            f"{before[0]:%Y-%m} {100 * pe / (pp + pe):.2f}%, "
            f"{after[0]:%Y-%m} {100 * ce / (cp_ + ce):.2f}%")
        out[f"{prefix}_distortion_pts"] = Measure(
            100.0 * abs(published - filled),
            f"{label}: published {100 * published:+.1f}%, gap-filled {100 * filled:+.1f}%")
    else:
        out[f"{prefix}_outage_pct"] = Measure(None, f"no counts for {label}")
        out[f"{prefix}_distortion_pts"] = Measure(None, f"no counts for {label}")
    changed = con.execute("""
        select c.location_id, any_value(s.sensor_description)
        from counts c left join sensors s using (location_id)
        where c.sensing_date between $a0 and $a1 or c.sensing_date between $b0 and $b1
        group by 1 having count(distinct c.sensing_date >= $b0) = 1 order by 1
    """, p).fetchall()
    shown = "; ".join(_sensor_label(r) for r in changed[:4]) + ("; …" if len(changed) > 4 else "")
    if not changed:
        out[f"{prefix}_sensor_mix_pts"] = Measure(0, f"{label}: the same sensors in both")
    elif set(by) != {False, True}:
        out[f"{prefix}_sensor_mix_pts"] = Measure(None, f"no counts for {label}")
    else:
        # the same change over the sensors that report in both periods
        n, lfl = con.execute("""
            with both_periods as (
                select location_id from counts
                where sensing_date between $a0 and $a1 or sensing_date between $b0 and $b1
                group by 1 having count(distinct sensing_date >= $b0) = 2
            )
            select count(distinct location_id),
                   sum(pedestriancount) filter (where sensing_date >= $b0)
                   / sum(pedestriancount) filter (where sensing_date <= $a1) - 1
            from counts join both_periods using (location_id)
            where sensing_date between $a0 and $a1 or sensing_date between $b0 and $b1
        """, p).fetchone()
        out[f"{prefix}_sensor_mix_pts"] = Measure(
            100.0 * abs(published - lfl),
            f"{label}: published {100 * published:+.1f}% vs {100 * lfl:+.1f}% over the "
            f"{n} sensors in both; {len(changed)} in only one: {shown}")
    return out


def compute_measures(con: duckdb.DuckDBPyConnection) -> dict[str, Measure]:
    """Every measure the reports can use. Needs the gap_est table
    (dq.completeness.build_gaps + estimate_gaps)."""
    w = windows(con)
    m: dict[str, Measure] = {}
    newest, last = w["newest_day"][0], w["last_day"][0]
    w0, w1 = w["last_28_days"]

    m["newest_day_rows_pct"] = Measure(_rows_pct_of_usual(con, newest), f"{newest}")
    m["last_day_rows_pct"] = Measure(_rows_pct_of_usual(con, last), f"{last}")

    pub, est = con.execute("""
        select (select sum(pedestriancount) from counts where sensing_date = $d),
               (select coalesce(sum(est), 0) from gap_est where sensing_date = $d)
    """, {"d": last}).fetchone()
    m["last_day_outage_pct"] = Measure(100.0 * est / (pub + est) if pub else None, f"{last}")

    # Sensors that went silent: no reading on the day or since. (A sensor
    # that is back on the newest day is an outage, already in gap_est.)
    dropped = con.execute("""
        select c.location_id, any_value(s.sensor_description)
        from counts c left join sensors s using (location_id)
        where c.sensing_date between $d - 7 and $d - 1
          and c.location_id not in (select location_id from counts where sensing_date >= $d)
        group by 1 order by 1
    """, {"d": last}).fetchall()
    m["last_day_dropped_sensors"] = Measure(
        len(dropped), "; ".join(_sensor_label(r) for r in dropped) or "none")

    # Month on month, and the same month a year earlier.
    m.update(_compare(con, w["prev_month"], w["month"], "mom"))
    m.update(_compare(con, w["year_ago_month"], w["month"], "yoy"))

    # Street ranking over the last 28 complete days.
    win = {"w0": w0, "w1": w1}
    short = con.execute("""
        with p as (
            select location_id, sum(pedestriancount) as pub from counts
            where sensing_date between $w0 and $w1 group by 1
        ),
        e as (
            select location_id, sum(est) as est from gap_est
            where sensing_date between $w0 and $w1 group by 1
        )
        select p.location_id, s.sensor_description,
               100.0 * coalesce(e.est, 0) / (p.pub + coalesce(e.est, 0)) as short_pct,
               rank() over (order by p.pub desc) as rank_published,
               rank() over (order by p.pub + coalesce(e.est, 0) desc) as rank_filled
        from p left join e using (location_id) left join sensors s using (location_id)
        order by short_pct desc, p.location_id
    """, win).fetchall()
    over = [r for r in short if r[2] > 1]
    m["ranking_max_shortfall_pct"] = Measure(
        short[0][2] if short else None,
        f"{len(over)} of {len(short)} sensors short by more than 1%"
        + (": " + "; ".join(f"{_sensor_label(r)} {r[2]:.1f}%, ranked {r[3]} vs {r[4]} gap-filled"
                            for r in over[:3])
           + ("; …" if len(over) > 3 else "") if over else ""))
    partial = con.execute("""
        select location_id, any_value(s.sensor_description)
        from counts c left join sensors s using (location_id)
        group by 1
        having bool_or(sensing_date between $w0 and $w1)
           and (min(sensing_date) > $w0 or max(sensing_date) < $w1)
        order by 1
    """, win).fetchall()
    m["ranking_partial_sensors"] = Measure(
        len(partial), "; ".join(_sensor_label(r) for r in partial) or "none")
    orphans = con.execute("""
        select distinct location_id from counts
        where sensing_date between $w0 and $w1
          and location_id not in (select location_id from sensors)
        order by 1
    """, win).fetchall()
    m["window_orphan_sensors"] = Measure(
        len(orphans), ", ".join(str(r[0]) for r in orphans) or "none")
    shared = con.execute("""
        select distinct s.sensor_name from counts c join sensors s using (location_id)
        where c.sensing_date between $w0 and $w1
          and s.sensor_name in (select sensor_name from sensors group by 1 having count(*) > 1)
        order by 1
    """, win).fetchall()
    m["window_shared_names"] = Measure(
        len(shared), ", ".join(r[0] for r in shared) or "none")

    # Hourly profile over the last 28 complete days: a plain average over the
    # published hours vs one that counts likely-zero gaps as 0.
    prof = con.execute("""
        with p as (
            select hourday, sum(pedestriancount) as total, count(*) as n from counts
            where sensing_date between $w0 and $w1 group by 1
        ),
        z as (
            select hourday, count(*) as zeros from gap_est
            where gap_class = 'likely_zero' and sensing_date between $w0 and $w1 group by 1
        )
        select p.hourday, 100.0 * (coalesce(z.zeros, 0)) / p.n as over_pct
        from p left join z using (hourday) order by over_pct desc, p.hourday limit 1
    """, win).fetchone()
    # mean over n published hours vs over n + zeros hours: overstated by zeros / n
    m["profile_max_overstatement_pct"] = Measure(
        prof[1] if prof else None, f"worst hour {prof[0]:02d}:00" if prof else "no counts")
    return m
