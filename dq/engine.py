"""A small dbt-style data-quality engine on DuckDB.

Checks are declared in YAML (see checks/pedestrian.yml). Each check compiles
to one SQL query that returns the *failing* records, so a failure can always
be inspected with the same query that counted it. A second query gives the
number of records checked, so failures can be reported as a rate.

Check types:
- not_null          columns that must have a value
- unique            a column or column combination that must not repeat
- accepted_range    min and/or max for a column
- accepted_values   a fixed list of allowed values
- relationships     every value must exist in a parent table (foreign key)
- expression        a SQL condition every row must satisfy
- freshness         the newest value must be at most max_age_days old
- sql               any query that returns failing records (with an optional
                    checked_sql for the denominator)

Each check has a severity (warn or error). A check passes when it has no
failures, or when its failure rate is within an optional tolerance_pct.
Otherwise its status is its severity.
"""
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

SEVERITIES = ("warn", "error")
REQUIRED = {
    "not_null": ["columns"],
    "unique": ["columns"],
    "accepted_range": ["column"],
    "accepted_values": ["column", "values"],
    "relationships": ["column", "to", "field"],
    "expression": ["expression"],
    "freshness": ["column", "max_age_days"],
    "sql": ["sql"],
}


@dataclass
class Check:
    id: str
    type: str
    description: str
    dimension: str
    severity: str
    table: str | None = None
    tolerance_pct: float = 0.0
    options: dict = field(default_factory=dict)


@dataclass
class Result:
    check: Check
    failing: int
    checked: int
    status: str
    fail_sql: str
    sample: list[dict]

    @property
    def fail_pct(self) -> float:
        return 100.0 * self.failing / self.checked if self.checked else 0.0


def load_suite(path: str | Path) -> list[Check]:
    """Read a YAML suite and validate it before anything runs."""
    spec = yaml.safe_load(Path(path).read_text())
    checks, seen = [], set()
    for raw in spec["checks"]:
        raw = dict(raw)
        cid = raw.pop("id")
        if cid in seen:
            raise ValueError(f"duplicate check id: {cid}")
        seen.add(cid)
        ctype = raw.pop("type")
        if ctype not in REQUIRED:
            raise ValueError(f"{cid}: unknown check type {ctype!r}")
        missing = [k for k in REQUIRED[ctype] if k not in raw]
        if ctype != "sql" and "table" not in raw:
            missing.append("table")
        if missing:
            raise ValueError(f"{cid}: missing {', '.join(missing)}")
        severity = raw.pop("severity", "error")
        if severity not in SEVERITIES:
            raise ValueError(f"{cid}: severity must be one of {SEVERITIES}")
        checks.append(Check(
            id=cid,
            type=ctype,
            description=raw.pop("description").strip(),
            dimension=raw.pop("dimension"),
            severity=severity,
            table=raw.pop("table", None),
            tolerance_pct=float(raw.pop("tolerance_pct", 0.0)),
            options=raw,
        ))
    return checks


def _sql_literal(value) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def compile_check(check: Check, params: dict) -> tuple[str, str, str]:
    """Return (failing-records SQL, failing-count SQL, checked-count SQL)."""
    o, t = check.options, check.table
    checked = f"select count(*) from {t}" if t else "select 1"
    if check.type == "not_null":
        cond = " or ".join(f"{c} is null" for c in o["columns"])
        fail = f"select * from {t} where {cond}"
    elif check.type == "unique":
        cols = ", ".join(o["columns"])
        fail = (f"select {cols}, count(*) as n_rows from {t} "
                f"group by {cols} having count(*) > 1 order by n_rows desc, {cols}")
        # every row that shares its key with another row counts as failing
        count = f"select coalesce(sum(n_rows), 0) from ({fail})"
        return fail, count, checked
    elif check.type == "accepted_range":
        conds = []
        if "min" in o:
            conds.append(f"{o['column']} < {_sql_literal(o['min'])}")
        if "max" in o:
            conds.append(f"{o['column']} > {_sql_literal(o['max'])}")
        if not conds:
            raise ValueError(f"{check.id}: accepted_range needs min or max")
        fail = f"select * from {t} where {' or '.join(conds)}"
    elif check.type == "accepted_values":
        values = ", ".join(_sql_literal(v) for v in o["values"])
        fail = f"select * from {t} where {o['column']} not in ({values})"
    elif check.type == "relationships":
        fail = (f"select c.* from {t} c where c.{o['column']} is not null "
                f"and not exists (select 1 from {o['to']} p "
                f"where p.{o['field']} = c.{o['column']})")
    elif check.type == "expression":
        fail = f"select * from {t} where not coalesce(({o['expression']}), false)"
    elif check.type == "freshness":
        checked = "select 1"
        fail = (f"select max({o['column']}) as latest, "
                f"DATE '{params['reference_date']}' as reference_date, "
                f"date_diff('day', max({o['column']}), DATE '{params['reference_date']}') as age_days "
                f"from {t} having date_diff('day', max({o['column']}), "
                f"DATE '{params['reference_date']}') > {int(o['max_age_days'])}")
    elif check.type == "sql":
        fail = o["sql"].strip().rstrip(";").format(**params)
        if "checked_sql" in o:
            checked = o["checked_sql"].strip().rstrip(";").format(**params)
    else:  # pragma: no cover - load_suite rejects unknown types
        raise ValueError(check.type)
    return fail, f"select count(*) from ({fail})", checked


def status_for(check: Check, failing: int, checked: int) -> str:
    if failing == 0:
        return "pass"
    if checked and 100.0 * failing / checked <= check.tolerance_pct:
        return "pass"
    return check.severity


def run_check(con: duckdb.DuckDBPyConnection, check: Check, params: dict,
              sample_size: int = 5) -> Result:
    fail, count, checked_sql = compile_check(check, params)
    failing = int(con.execute(count).fetchone()[0])
    checked = int(con.execute(checked_sql).fetchone()[0])
    sample = []
    if failing:
        # sorted, so the same data always gives the same sample
        cur = con.execute(f"select * from ({fail}) order by all limit {sample_size}")
        names = [d[0] for d in cur.description]
        sample = [dict(zip(names, row)) for row in cur.fetchall()]
    return Result(check, failing, checked, status_for(check, failing, checked),
                  fail, sample)


def run_suite(con: duckdb.DuckDBPyConnection, checks: list[Check],
              params: dict) -> list[Result]:
    return [run_check(con, c, params) for c in checks]
