"""Download a snapshot of the City of Melbourne pedestrian counting data.

Two datasets from the City of Melbourne open data portal (Opendatasoft
Explore API v2.1, no key or sign-up needed):
- "Pedestrian Counting System (counts per hour)": one row per sensor per
  hour, a rolling window of about two years, refreshed daily (~1.6M rows,
  ~26 MB as Parquet; the download takes a few minutes).
- "Pedestrian Counting System - Sensor Locations": one row per sensor.

The counts are a live feed, so every download is a new snapshot. Each one is
saved to data/raw/<snapshot>/ (named after the portal's "data processed"
time, in UTC) with a source.json that records what was fetched: the portal's
own metadata (processing time, record count, licence), the file sizes and
SHA-256 hashes. The script stops if a downloaded file's row count doesn't
match the record count the portal reports, so a truncated download can't
reach the checks.

Licence: the portal's Data Policy page says its open data is provided under
CC BY 4.0. The sensor locations dataset is labelled "CC BY" in its metadata;
the counts dataset has no licence field set, so the portal-wide policy is
what applies.
"""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import time
import urllib.request

import duckdb

API = "https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets"
DATASETS = {
    "counts": "pedestrian-counting-system-monthly-counts-per-hour",
    "sensors": "pedestrian-counting-system-sensor-locations",
}
RAW = Path(__file__).resolve().parents[1] / "data" / "raw"


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def fetch(url: str, dest: Path, retries: int = 3) -> None:
    tmp = dest.with_suffix(".part")
    for attempt in range(1, retries + 1):
        try:
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(dest)
            return
        except OSError as exc:
            print(f"  retry {attempt} after error: {exc}")
            time.sleep(5 * attempt)
    tmp.unlink(missing_ok=True)
    raise SystemExit(f"could not download {url}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true",
                        help="download again even if this snapshot exists")
    args = parser.parse_args()

    metas = {name: get_json(f"{API}/{ds}")["metas"]["default"]
             for name, ds in DATASETS.items()}
    processed = datetime.fromisoformat(metas["counts"]["data_processed"])
    snapshot = processed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    out = RAW / snapshot
    if (out / "source.json").exists() and not args.force:
        print(f"snapshot {snapshot} already downloaded: {out}")
        return
    out.mkdir(parents=True, exist_ok=True)

    files = {}
    for name, ds in DATASETS.items():
        dest = out / f"{name}.parquet"
        print(f"downloading {ds} ...")
        fetch(f"{API}/{ds}/exports/parquet", dest)
        rows = duckdb.sql(f"select count(*) from read_parquet('{dest}')").fetchone()[0]
        expected = metas[name]["records_count"]
        if rows != expected:
            raise SystemExit(f"{name}: {rows:,} rows downloaded but the portal "
                             f"reports {expected:,}; try again later")
        files[name] = {
            "dataset": ds,
            "url": f"{API}/{ds}/exports/parquet",
            "file": dest.name,
            "rows": rows,
            "bytes": dest.stat().st_size,
            "sha256": sha256(dest),
            "data_processed": metas[name]["data_processed"],
            "metadata_processed": metas[name]["metadata_processed"],
            "licence": metas[name]["license"],
            "timezone": metas[name]["timezone"],
        }
        print(f"  {rows:,} rows, {files[name]['bytes'] / 1e6:.1f} MB")

    source = {
        "snapshot": snapshot,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "portal_licence_policy": "CC BY 4.0 (https://data.melbourne.vic.gov.au/pages/about-our-data/)",
        "files": files,
    }
    (out / "source.json").write_text(json.dumps(source, indent=2) + "\n")
    print(f"saved snapshot {snapshot} to {out}")


if __name__ == "__main__":
    main()
