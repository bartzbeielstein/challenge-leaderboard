"""Tests for scripts/fetch_actuals.py with ENTSO-E mocked out.

Pure-logic coverage: the script's network path reuses
``score_day.fetch_ground_truth`` (already tested in test_score_day.py),
so here we mock it and exercise date discovery, idempotent merging and
the not-ready skip. Hermetic — no network, no API key, all under tmp_path.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import fetch_actuals as fa
import score_day as sd


def _fake_frame(target_date: str) -> pd.DataFrame:
    """Stand-in for score_day._download_load_frame: actual + forecast columns."""
    idx = pd.date_range(f"{target_date}T00:00:00Z", periods=24, freq="h", tz="UTC")
    return pd.DataFrame({
        "Actual Total Load": [1000.0 + i for i in range(24)],
        "Day-ahead Total Load Forecast": [990.0 + i for i in range(24)],
    }, index=idx)


@pytest.fixture(autouse=True)
def isolate_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "ACTUALS_PATH", tmp_path / "data" / "actual_load.parquet")
    monkeypatch.setattr(sd, "SUBMISSIONS_DIR", tmp_path / "submissions")
    monkeypatch.setattr(fa, "_today_utc", lambda: date(2026, 6, 1))
    # Default-mock the forecast download so tests stay hermetic even when a real
    # ENTSOE_API_KEY is present in the environment (no network in CI/local).
    monkeypatch.setattr(sd, "_download_load_frame", _fake_frame)
    (tmp_path / "submissions").mkdir()
    (tmp_path / "data").mkdir()
    yield


def _write_submission(tmp_path, team: str, date_str: str):
    d = tmp_path / "submissions" / team
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{date_str}.csv").write_text("timestamp_utc,forecast_mw\n")


def _series(date_str: str):
    idx = pd.date_range(f"{date_str}T00:00:00Z", periods=24, freq="h", tz="UTC")
    return pd.Series([1000.0 + i for i in range(24)], index=idx, name="load")


# --------------------------------------------------------------------------
# discover_dates
# --------------------------------------------------------------------------

def test_discover_dates_unions_submissions_and_scored(tmp_path, monkeypatch):
    _write_submission(tmp_path, "team_4", "2026-05-26")
    _write_submission(tmp_path, "team_4", "2026-05-27")
    _write_submission(tmp_path, "hot_rod", "2026-05-26")
    monkeypatch.setattr(sd, "scored_dates", lambda: {"2026-05-25"})
    assert fa.discover_dates() == ["2026-05-25", "2026-05-26", "2026-05-27"]


def test_discover_dates_clamps_future(tmp_path, monkeypatch):
    _write_submission(tmp_path, "team_4", "2026-05-30")
    _write_submission(tmp_path, "team_4", "2026-12-31")  # after today (06-01)
    monkeypatch.setattr(sd, "scored_dates", lambda: set())
    assert fa.discover_dates() == ["2026-05-30"]


def test_discover_dates_ignores_non_date_csvs(tmp_path, monkeypatch):
    d = tmp_path / "submissions" / "team_4"
    d.mkdir(parents=True)
    (d / "notes.csv").write_text("x\n")
    (d / "2026-05-26.csv").write_text("timestamp_utc,forecast_mw\n")
    monkeypatch.setattr(sd, "scored_dates", lambda: set())
    assert fa.discover_dates() == ["2026-05-26"]


# --------------------------------------------------------------------------
# dates_in_range
# --------------------------------------------------------------------------

def test_dates_in_range_inclusive():
    assert fa.dates_in_range("2026-05-26", "2026-05-28") == [
        "2026-05-26", "2026-05-27", "2026-05-28"]


def test_dates_in_range_reversed_is_empty():
    assert fa.dates_in_range("2026-05-28", "2026-05-26") == []


# --------------------------------------------------------------------------
# already_complete
# --------------------------------------------------------------------------

def test_already_complete_only_full_days(tmp_path):
    rows = []
    for h in range(24):  # full day
        rows.append({"timestamp_utc": f"2026-05-26T{h:02d}:00:00Z", "load_mw": 1.0})
    for h in range(5):   # partial day
        rows.append({"timestamp_utc": f"2026-05-27T{h:02d}:00:00Z", "load_mw": 1.0})
    pd.DataFrame(rows).to_parquet(fa.ACTUALS_PATH, index=False)
    assert fa.already_complete(fa.ACTUALS_PATH) == {"2026-05-26"}


def test_already_complete_missing_file_is_empty(tmp_path):
    assert fa.already_complete(fa.ACTUALS_PATH) == set()


# --------------------------------------------------------------------------
# fetch_one_day
# --------------------------------------------------------------------------

def test_fetch_one_day_skips_when_not_ready(monkeypatch):
    def boom(_d):
        raise sd.GroundTruthNotReady("noch nicht da")
    monkeypatch.setattr(sd, "fetch_ground_truth", boom)
    assert fa.fetch_one_day("2026-05-26") is None


def test_fetch_one_day_builds_tidy_frame(monkeypatch):
    monkeypatch.setattr(sd, "fetch_ground_truth", lambda d: _series(d))
    df = fa.fetch_one_day("2026-05-26")
    assert list(df.columns) == ["timestamp_utc", "load_mw", "entsoe_forecast_mw"]
    assert len(df) == 24
    assert df["timestamp_utc"].iloc[0] == "2026-05-26T00:00:00Z"
    assert df["load_mw"].iloc[0] == 1000.0
    assert df["entsoe_forecast_mw"].iloc[0] == 990.0   # from _fake_frame


def test_fetch_one_day_forecast_nan_when_download_fails(monkeypatch):
    monkeypatch.setattr(sd, "fetch_ground_truth", lambda d: _series(d))

    def boom(_d):
        raise RuntimeError("ENTSOE_API_KEY ist nicht gesetzt")
    monkeypatch.setattr(sd, "_download_load_frame", boom)
    df = fa.fetch_one_day("2026-05-26")
    assert df["load_mw"].iloc[0] == 1000.0          # actual unaffected
    assert df["entsoe_forecast_mw"].isna().all()    # forecast best-effort -> NaN


# --------------------------------------------------------------------------
# merge_actuals
# --------------------------------------------------------------------------

def test_merge_actuals_idempotent_and_last_wins(monkeypatch):
    monkeypatch.setattr(sd, "fetch_ground_truth", lambda d: _series(d))
    first = fa.fetch_one_day("2026-05-26")
    assert fa.merge_actuals([first]) == 24
    # Re-fetch same day with different values -> dedup on timestamp, last wins.
    monkeypatch.setattr(sd, "fetch_ground_truth",
                        lambda d: _series(d) + 500.0)
    second = fa.fetch_one_day("2026-05-26")
    assert fa.merge_actuals([second]) == 24      # still 24 rows, not 48
    out = pd.read_parquet(fa.ACTUALS_PATH)
    assert out["load_mw"].iloc[0] == 1500.0      # overwritten


def test_merge_actuals_empty_writes_nothing():
    assert fa.merge_actuals([]) == 0
    assert not fa.ACTUALS_PATH.exists()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def test_main_writes_parquet_for_discovered_days(tmp_path, monkeypatch):
    _write_submission(tmp_path, "team_4", "2026-05-26")
    _write_submission(tmp_path, "team_4", "2026-05-27")
    monkeypatch.setattr(sd, "scored_dates", lambda: set())
    monkeypatch.setattr(sd, "fetch_ground_truth", lambda d: _series(d))
    monkeypatch.setattr("sys.argv", ["fetch_actuals.py"])
    assert fa.main() == 0
    out = pd.read_parquet(fa.ACTUALS_PATH)
    assert len(out) == 48
    assert set(out["timestamp_utc"].str.slice(0, 10)) == {"2026-05-26", "2026-05-27"}


def test_main_skips_already_complete_days(tmp_path, monkeypatch):
    _write_submission(tmp_path, "team_4", "2026-05-26")
    monkeypatch.setattr(sd, "scored_dates", lambda: set())
    monkeypatch.setattr(sd, "fetch_ground_truth", lambda d: _series(d))
    monkeypatch.setattr("sys.argv", ["fetch_actuals.py"])
    fa.main()
    # Second run: day already complete -> fetch_ground_truth must NOT be called.
    def boom(_d):
        raise AssertionError("should have been skipped")
    monkeypatch.setattr(sd, "fetch_ground_truth", boom)
    assert fa.main() == 0
