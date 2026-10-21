"""Part 4b: the daily gate. Should today's run fail?

A scheduled run that fails every day gets ignored, and this feed has an
error that won't go away soon: sensors 28 and 78 have counts but no sensor
record, and their rows stay in the two-year window until 2027-28. So the
gate fails only on errors nobody has looked at yet.

A known issue (checks/known_issues.yml) names an error check and an
`accept_where` condition on that check's failing records, plus who owns it
and why it is accepted. The gate counts the failing records the condition
does not cover. Any such record blocks the run, so a *new* sensor without a
record still fails the gate even while 28 and 78 are accepted. A count
baseline ("at most 20,472 rows") would not do this: the accepted rows shrink
as the window moves, and a new problem could hide in the gap.

Warnings never block; they are in the reports. A known issue whose check
passes again is listed as resolved, so the file can be cleaned up.
"""
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from dq.engine import Check, Result

FIELDS = ("check", "accept_where", "owner", "since", "reason")


@dataclass
class KnownIssue:
    check: str
    accept_where: str
    owner: str
    since: str
    reason: str


@dataclass
class GateRow:
    suite: str
    result: Result
    outcome: str            # blocking, known or resolved
    unaccepted: int         # failing records no known issue covers
    issue: KnownIssue | None = None


def load_known_issues(path: str | Path, checks: list[Check]) -> dict[str, KnownIssue]:
    """Read and validate the known issues against the check suites, so a
    typo can't accept an error by accident or point at nothing."""
    by_id = {c.id: c for c in checks}
    spec = yaml.safe_load(Path(path).read_text()) or {}
    issues = {}
    for raw in spec.get("known_issues") or []:
        missing = [f for f in FIELDS if not str(raw.get(f) or "").strip()]
        if missing:
            raise ValueError(f"known issue {raw.get('check')!r}: missing {', '.join(missing)}")
        extra = set(raw) - set(FIELDS)
        if extra:
            raise ValueError(f"known issue {raw['check']!r}: unknown field(s) {', '.join(sorted(extra))}")
        cid = raw["check"]
        if cid not in by_id:
            raise ValueError(f"known issue {cid!r}: no such check")
        if by_id[cid].severity != "error":
            raise ValueError(f"known issue {cid!r}: only error checks can be accepted "
                             "(warnings never block the gate)")
        if cid in issues:
            raise ValueError(f"known issue {cid!r}: listed twice")
        issues[cid] = KnownIssue(**{f: str(raw[f]).strip() for f in FIELDS})
    return issues


def unaccepted_count(con: duckdb.DuckDBPyConnection, result: Result,
                     issue: KnownIssue | None) -> int:
    """Failing records of `result` that the known issue does not cover."""
    if issue is None:
        return result.failing
    return int(con.execute(
        f"select count(*) from ({result.fail_sql}) "
        f"where not coalesce(({issue.accept_where}), false)").fetchone()[0])


def evaluate(con: duckdb.DuckDBPyConnection, suite: str, results: list[Result],
             issues: dict[str, KnownIssue]) -> list[GateRow]:
    """One row per error result and per known issue that no longer fires.
    `con` must be the connection the results were computed on."""
    rows = []
    for r in results:
        issue = issues.get(r.check.id)
        if r.status == "error":
            n = unaccepted_count(con, r, issue)
            rows.append(GateRow(suite, r, "blocking" if n else "known", n, issue))
        elif issue is not None:
            rows.append(GateRow(suite, r, "resolved", 0, issue))
    return rows


def passed(rows: list[GateRow]) -> bool:
    return not any(row.outcome == "blocking" for row in rows)
