"""Run the real check suite on the synthetic snapshot in conftest.py and
check that each planted defect is caught with the exact count, and that
the clean parts pass."""
from pathlib import Path

import pytest

from dq.engine import load_suite, run_suite
from tests.conftest import PARAMS

SUITE = Path(__file__).resolve().parents[1] / "checks" / "pedestrian.yml"

# check id -> (failing, checked, status)
EXPECTED = {
    # 39 full days x 10 colliding hours x 2 sensors
    "counts_id_unique": (780, None, "warn"),
    # sensor 99: 39 full days + 4 hours on the newest day
    "counts_sensor_exists": (39 * 24 + 4, None, "error"),
    "counts_sensor_name_present": (39 * 24 + 4, None, "warn"),
    "counts_latest_day_complete": (1, 1, "warn"),
    # sensor 2: 1 hour + 1 day; expected hours = 4 sensors x 39 days x 24
    # + sensor 3's 15 days x 24 = 4,104, so 25 missing (0.61%) is within the
    # 1% tolerance
    "counts_hour_coverage": (25, 4 * 39 * 24 + 15 * 24, "pass"),
    "counts_full_day_gaps": (1, 4 * 39 + 15, "warn"),
    "counts_sensor_still_reporting": (1, 5, "warn"),
    "sensors_installed_before_counts": (1, 4, "warn"),
    "sensors_name_unique": (2, 6, "warn"),
    "sensors_active_outdoor_reporting": (2, 5, "warn"),
}


@pytest.fixture
def results(con):
    return {r.check.id: r for r in run_suite(con, load_suite(SUITE), PARAMS)}


def test_planted_defects_are_caught(results):
    for cid, (failing, checked, status) in EXPECTED.items():
        r = results[cid]
        assert r.failing == failing, cid
        if checked is not None:
            assert r.checked == checked, cid
        assert r.status == status, cid


def test_everything_else_passes(results):
    for cid, r in results.items():
        if cid not in EXPECTED:
            assert (r.failing, r.status) == (0, "pass"), cid


def test_stopped_sensor_is_reported_with_its_last_day(results):
    row = results["counts_sensor_still_reporting"].sample[0]
    assert row["location_id"] == 3
    assert str(row["last_reading"]) == "2026-09-15"
    assert row["days_silent"] == 25
