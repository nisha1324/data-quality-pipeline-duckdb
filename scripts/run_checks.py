"""Run the data-quality suite on a downloaded snapshot and write the report.

Usage:
    python scripts/run_checks.py                  # newest snapshot in data/raw/
    python scripts/run_checks.py --snapshot data/raw/2026-10-18T1539Z
    python scripts/run_checks.py --fail-on error  # exit 1 if any check errors

Outputs (in results/): DQ_REPORT.md, dq_results.csv and snapshot.json (the
snapshot's source metadata, so the report can be traced to what was
downloaded).
"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq.engine import load_suite, run_suite  # noqa: E402
from dq.load import latest_snapshot, load_snapshot  # noqa: E402
from dq.report import write_csv, write_markdown  # noqa: E402

RESULTS = ROOT / "results"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the data-quality checks.")
    parser.add_argument("--snapshot", type=Path, help="snapshot folder (default: newest)")
    parser.add_argument("--suite", type=Path, default=ROOT / "checks" / "pedestrian.yml")
    parser.add_argument("--fail-on", choices=["warn", "error"],
                        help="exit with status 1 if any check ends at this level or worse")
    args = parser.parse_args()

    snapshot = args.snapshot or latest_snapshot()
    checks = load_suite(args.suite)
    con, params, source = load_snapshot(snapshot)
    results = run_suite(con, checks, params)

    RESULTS.mkdir(exist_ok=True)
    write_csv(results, RESULTS / "dq_results.csv")
    write_markdown(results, params, source, RESULTS / "DQ_REPORT.md")
    (RESULTS / "snapshot.json").write_text(json.dumps(source, indent=2) + "\n")

    for r in results:
        print(f"{r.status:5}  {r.check.id:34} {r.failing:>9,} / {r.checked:,}")
    levels = {"warn": ("warn", "error"), "error": ("error",)}
    if args.fail_on and any(r.status in levels[args.fail_on] for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
