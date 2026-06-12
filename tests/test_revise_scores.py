"""Tests for scripts/revise_scores.py — hermetic, no network, no API key.

Pins the revision contract:
  * nur bereits benotete Tage werden geprüft (Erstbewertung = Catch-up),
  * unveränderte Tage bleiben unangetastet (Schwellwert),
  * revidierte Tage werden neu bewertet UND der committete Ist-Load
    nachgezogen,
  * unvollständige/fehlgeschlagene frische Abrufe stufen einen benoteten
    Tag niemals herab.
"""
from __future__ import annotations

import pandas as pd
import pytest

import fetch_actuals as fa
import revise_scores as rs
import score_day as sd


DAY = "2026-06-11"


def _hours(day: str) -> pd.DatetimeIndex:
    return pd.date_range(f"{day}T00:00:00Z", periods=24, freq="h", tz="UTC")


def _frame(day: str, load: float, forecast: float | None = None,
           nan_hours: int = 0) -> pd.DataFrame:
    """Fake-Ergebnis von score_day._download_load_frame (beide Spalten)."""
    loads = [load] * 24
    for i in range(nan_hours):
        loads[i] = float("nan")
    return pd.DataFrame({
        "Actual Total Load": loads,
        "Day-ahead Total Load Forecast": [forecast or load] * 24,
    }, index=_hours(day))


def _write_actuals(day: str, load: float) -> None:
    stamps = _hours(day).strftime(fa.TS_FORMAT)
    fa.ACTUALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "timestamp_utc": list(stamps),
        "load_mw": [load] * 24,
        "entsoe_forecast_mw": [load] * 24,
    }).to_parquet(fa.ACTUALS_PATH, index=False)


def _write_scores(day: str, mae: float = 555.0) -> None:
    sd.SCORES_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{
        "team_id": "team_4", "target_date": day,
        "scored_at_utc": "2026-06-12T10:00:00+00:00", "source_date": day,
        "carried_forward": False, "mae": mae, "rmse": mae, "mape": 1.0,
        "bias": 0.0, "upr": 50.0,
    }]).to_parquet(sd.SCORES_PATH, index=False)


@pytest.fixture(autouse=True)
def isolate_paths(monkeypatch, tmp_path, teams_yml):
    monkeypatch.setattr(sd, "SCORES_PATH", tmp_path / "data" / "scores.parquet")
    monkeypatch.setattr(sd, "SUBMISSIONS_DIR", tmp_path / "submissions")
    monkeypatch.setattr(sd, "TEAMS_PATH", teams_yml)
    monkeypatch.setattr(fa, "ACTUALS_PATH",
                        tmp_path / "data" / "actual_load.parquet")


# --------------------------------------------------------------------------
# Fensterbildung
# --------------------------------------------------------------------------

def test_revision_window_only_already_scored_days():
    _write_scores("2026-06-10")
    # 06-11 und 06-09 sind unbewertet -> nicht Sache der Revision.
    assert rs.revision_window("2026-06-11", 3) == ["2026-06-10"]


def test_revision_window_empty_without_scores():
    assert rs.revision_window("2026-06-11", 7) == []


# --------------------------------------------------------------------------
# Revisionserkennung + Neubewertung
# --------------------------------------------------------------------------

def test_revision_rescores_and_updates_actuals(monkeypatch,
                                               make_submission_csv):
    _write_scores(DAY)
    _write_actuals(DAY, 1000.0)
    make_submission_csv(f"submissions/team_4/{DAY}.csv",
                        forecast=[1000.0] * 24)
    # ENTSO-E hat den Tag von 1000 auf 1100 MW korrigiert.
    monkeypatch.setattr(sd, "_download_load_frame",
                        lambda d: _frame(d, 1100.0, forecast=1050.0))

    assert rs.revise(DAY, 1, 1.0) == [DAY]

    scores = pd.read_parquet(sd.SCORES_PATH)
    row = scores[(scores["team_id"] == "team_4")
                 & (scores["target_date"] == DAY)]
    assert len(row) == 1                       # idempotent ersetzt, kein Duplikat
    assert row.iloc[0]["mae"] == 100.0         # |1000 - 1100|
    actuals = pd.read_parquet(fa.ACTUALS_PATH)
    assert (actuals["load_mw"] == 1100.0).all()
    assert (actuals["entsoe_forecast_mw"] == 1050.0).all()


def test_unchanged_day_within_tolerance_keeps_scores(monkeypatch,
                                                     make_submission_csv):
    _write_scores(DAY, mae=555.0)
    _write_actuals(DAY, 1000.0)
    make_submission_csv(f"submissions/team_4/{DAY}.csv",
                        forecast=[1000.0] * 24)
    # 0.5 MW Abweichung = Resampling-Rauschen, keine Korrektur.
    monkeypatch.setattr(sd, "_download_load_frame",
                        lambda d: _frame(d, 1000.5))

    assert rs.revise(DAY, 1, 1.0) == []
    assert pd.read_parquet(sd.SCORES_PATH).iloc[0]["mae"] == 555.0
    assert (pd.read_parquet(fa.ACTUALS_PATH)["load_mw"] == 1000.0).all()


def test_missing_stored_day_counts_as_revision(monkeypatch,
                                               make_submission_csv):
    _write_scores(DAY)   # benotet, aber kein committeter Ist-Load
    make_submission_csv(f"submissions/team_4/{DAY}.csv",
                        forecast=[1000.0] * 24)
    monkeypatch.setattr(sd, "_download_load_frame",
                        lambda d: _frame(d, 1100.0))

    assert rs.revise(DAY, 1, 1.0) == [DAY]
    assert fa.ACTUALS_PATH.exists()


# --------------------------------------------------------------------------
# Nie herabstufen
# --------------------------------------------------------------------------

def test_incomplete_fresh_day_never_downgrades(monkeypatch,
                                               make_submission_csv):
    _write_scores(DAY, mae=555.0)
    _write_actuals(DAY, 1000.0)
    make_submission_csv(f"submissions/team_4/{DAY}.csv",
                        forecast=[1000.0] * 24)
    monkeypatch.setattr(sd, "_download_load_frame",
                        lambda d: _frame(d, 1100.0, nan_hours=2))

    assert rs.revise(DAY, 1, 1.0) == []
    assert pd.read_parquet(sd.SCORES_PATH).iloc[0]["mae"] == 555.0
    assert (pd.read_parquet(fa.ACTUALS_PATH)["load_mw"] == 1000.0).all()


def test_failed_fetch_skips_day(monkeypatch):
    _write_scores(DAY, mae=555.0)
    _write_actuals(DAY, 1000.0)

    def boom(_):
        raise RuntimeError("ENTSO-E down")

    monkeypatch.setattr(sd, "_download_load_frame", boom)
    assert rs.revise(DAY, 1, 1.0) == []
    assert pd.read_parquet(sd.SCORES_PATH).iloc[0]["mae"] == 555.0
