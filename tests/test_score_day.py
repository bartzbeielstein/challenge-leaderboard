"""Tests for scripts/score_day.py with ENTSO-E mocked out.

Covers the **post-LOCF refactor** behaviour (commit 26463924432 on
2026-05-26 17:21 UTC, "score: LOCF für fehlende Submissions"). Before
that commit, score_day.py only scored exact-date submissions and
silently skipped every team without one — that bug is what stalled
the leaderboard. The tests below pin the LOCF semantics so that any
regression is caught immediately.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import score_day as sd


@pytest.fixture(autouse=True)
def isolate_paths(monkeypatch, tmp_path):
    """Redirect SUBMISSIONS_DIR / SCORES_PATH / TEAMS_PATH to tmp_path."""
    monkeypatch.setattr(sd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sd, "SUBMISSIONS_DIR", tmp_path / "submissions")
    monkeypatch.setattr(sd, "SCORES_PATH", tmp_path / "data" / "scores.parquet")
    monkeypatch.setattr(sd, "TEAMS_PATH", tmp_path / "teams.yml")
    (tmp_path / "submissions").mkdir()
    (tmp_path / "data").mkdir()
    yield


@pytest.fixture
def write_submission(tmp_path):
    def _w(team: str, date: str, forecast=None):
        d = tmp_path / "submissions" / team
        d.mkdir(parents=True, exist_ok=True)
        stamps = pd.date_range(f"{date}T00:00:00Z", periods=24, freq="h",
                                tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        df = pd.DataFrame({
            "timestamp_utc": stamps,
            "forecast_mw": forecast or [1000.0] * 24,
        })
        p = d / f"{date}.csv"
        df.to_csv(p, index=False)
        return p

    return _w


@pytest.fixture
def fake_ground_truth(monkeypatch):
    """Replace fetch_ground_truth with a deterministic 24-hour series."""

    def _series(values=None):
        idx = pd.date_range("2026-05-26T00:00:00Z", periods=24,
                             freq="h", tz="UTC")
        return pd.Series(values if values is not None else [1200.0] * 24,
                          index=idx, name="load")

    def _install(target_values=None):
        series = _series(target_values)
        monkeypatch.setattr(sd, "fetch_ground_truth", lambda _date: series)
        return series

    return _install


@pytest.fixture
def teams_yml_file(tmp_path):
    import yaml
    (tmp_path / "teams.yml").write_text(yaml.safe_dump({
        "teams": [
            {"id": "team_4", "display_name": "Team 4", "github_handles": ["a"]},
            {"id": "hot_rod", "display_name": "Hot Rod", "github_handles": ["b"]},
            {"id": "ghost", "display_name": "Ghost", "github_handles": ["c"]},
        ]
    }))


def test_score_submission_zero_error_is_zero_mae():
    actual = pd.Series([1000.0] * 24,
                        index=pd.date_range("2026-05-26", periods=24, freq="h"))
    out = sd.score_submission(np.array([1000.0] * 24), actual)
    assert out["mae"] == 0
    assert out["rmse"] == 0
    assert out["mape"] == 0


def test_collect_forecasts_exact_match(write_submission, teams_yml_file):
    write_submission("team_4", "2026-05-26")
    forecasts = sd.collect_forecasts("2026-05-26", ["team_4", "hot_rod", "ghost"])
    assert forecasts == [(
        "team_4",
        sd.SUBMISSIONS_DIR / "team_4" / "2026-05-26.csv",
        False,
    )]


def test_collect_forecasts_locf_carries_forward(write_submission, teams_yml_file):
    # team_4 only submitted earlier; LOCF must pick the most recent prior.
    write_submission("team_4", "2026-05-13")
    write_submission("team_4", "2026-05-22")
    write_submission("hot_rod", "2026-05-26")  # exact; not LOCF
    forecasts = sd.collect_forecasts(
        "2026-05-26", ["team_4", "hot_rod", "ghost"]
    )
    by_team = {t: (p.name, c) for t, p, c in forecasts}
    assert by_team["team_4"] == ("2026-05-22.csv", True)
    assert by_team["hot_rod"] == ("2026-05-26.csv", False)
    assert "ghost" not in by_team   # no submissions at all -> skipped


def test_collect_forecasts_ignores_future_dates(write_submission, teams_yml_file):
    # Only future submission exists -> team should be skipped (the bug
    # before LOCF was that all teams without an exact match were skipped;
    # the bug after LOCF would be picking up a *future* submission).
    write_submission("team_4", "2026-05-27")
    assert sd.collect_forecasts("2026-05-26", ["team_4"]) == []


def test_main_writes_parquet_with_expected_rows(
    write_submission, fake_ground_truth, teams_yml_file, monkeypatch
):
    fake_ground_truth([1000.0] * 24)
    write_submission("team_4", "2026-05-26", forecast=[1000.0] * 24)
    write_submission("hot_rod", "2026-05-26", forecast=[1100.0] * 24)

    monkeypatch.setattr("sys.argv", ["score_day.py", "--date", "2026-05-26"])
    rc = sd.main()
    assert rc == 0

    df = pd.read_parquet(sd.SCORES_PATH)
    rows = {r["team_id"]: r for r in df.to_dict("records")}
    assert set(rows) == {"team_4", "hot_rod"}
    assert rows["team_4"]["mae"] == 0.0
    assert rows["hot_rod"]["mae"] == 100.0
    assert rows["team_4"]["carried_forward"] is False
    assert rows["hot_rod"]["carried_forward"] is False


def test_main_is_idempotent(
    write_submission, fake_ground_truth, teams_yml_file, monkeypatch
):
    fake_ground_truth([1000.0] * 24)
    write_submission("team_4", "2026-05-26")
    monkeypatch.setattr("sys.argv", ["score_day.py", "--date", "2026-05-26"])
    sd.main()
    sd.main()  # second run must not duplicate the row
    df = pd.read_parquet(sd.SCORES_PATH)
    assert len(df) == 1


def test_main_with_no_submissions_writes_nothing(
    fake_ground_truth, teams_yml_file, monkeypatch
):
    fake_ground_truth([1000.0] * 24)
    monkeypatch.setattr("sys.argv", ["score_day.py", "--date", "2026-05-26"])
    rc = sd.main()
    assert rc == 0
    assert not sd.SCORES_PATH.exists()


# --------------------------------------------------------------------------
# Catch-up: a skipped daily cron must self-heal on the next run.
# --------------------------------------------------------------------------

def _gt_series(date_str: str = "2026-05-28"):
    idx = pd.date_range(f"{date_str}T00:00:00Z", periods=24, freq="h", tz="UTC")
    return pd.Series([1000.0] * 24, index=idx, name="load")


def test_scored_dates_empty_when_no_file():
    assert sd.scored_dates() == set()


def test_scored_dates_reads_target_dates(
    write_submission, fake_ground_truth, teams_yml_file, monkeypatch
):
    fake_ground_truth([1000.0] * 24)
    write_submission("team_4", "2026-05-26")
    monkeypatch.setattr("sys.argv", ["score_day.py", "--date", "2026-05-26"])
    sd.main()
    assert sd.scored_dates() == {"2026-05-26"}


def test_days_to_score_no_catchup_is_single_day():
    assert sd.days_to_score("2026-05-28", catch_up=1) == ["2026-05-28"]


def test_days_to_score_includes_unscored_window_days(monkeypatch):
    # 26 already scored; window for catch_up=3 is {26,27,28}; 27 missed, 28 primary.
    monkeypatch.setattr(sd, "scored_dates", lambda: {"2026-05-26"})
    assert sd.days_to_score("2026-05-28", catch_up=3) == ["2026-05-27", "2026-05-28"]


def test_days_to_score_primary_always_included_even_if_scored(monkeypatch):
    monkeypatch.setattr(sd, "scored_dates", lambda: {"2026-05-28"})
    assert sd.days_to_score("2026-05-28", catch_up=1) == ["2026-05-28"]


def test_days_to_score_never_rescore_old_scored_day(monkeypatch):
    # 27 already scored must NOT reappear; only the unscored primary 28.
    monkeypatch.setattr(sd, "scored_dates", lambda: {"2026-05-27"})
    assert sd.days_to_score("2026-05-28", catch_up=2) == ["2026-05-28"]


def test_main_catch_up_self_heals_missed_day(
    write_submission, teams_yml_file, monkeypatch
):
    # 27 already scored, 28 missed; a --catch-up run for 28 scores the missed
    # day and does NOT re-score the already-graded 27.
    write_submission("team_4", "2026-05-27")
    write_submission("team_4", "2026-05-28")
    monkeypatch.setattr(sd, "scored_dates", lambda: {"2026-05-27"})
    monkeypatch.setattr(sd, "fetch_ground_truth", lambda d: _gt_series(d))
    monkeypatch.setattr("sys.argv",
                        ["score_day.py", "--date", "2026-05-28", "--catch-up", "2"])
    assert sd.main() == 0
    scored = set(pd.read_parquet(sd.SCORES_PATH).target_date)
    assert scored == {"2026-05-28"}


def test_main_catch_up_defers_day_without_actuals_but_makes_progress(
    write_submission, teams_yml_file, monkeypatch
):
    # Nothing scored yet; actuals ready for 27 but not for 28 (primary).
    # 27 is scored, 28 is deferred; progress was made -> rc 0, 28 retried later.
    write_submission("team_4", "2026-05-27")
    write_submission("team_4", "2026-05-28")
    monkeypatch.setattr(sd, "scored_dates", lambda: set())

    def fake_fetch(d):
        if d == "2026-05-28":
            raise RuntimeError("ENTSO-E final-load enthält NaN für 2026-05-28: 2 fehlende Stunden")
        return _gt_series(d)

    monkeypatch.setattr(sd, "fetch_ground_truth", fake_fetch)
    monkeypatch.setattr("sys.argv",
                        ["score_day.py", "--date", "2026-05-28", "--catch-up", "2"])
    assert sd.main() == 0
    scored = set(pd.read_parquet(sd.SCORES_PATH).target_date)
    assert scored == {"2026-05-27"}


def test_main_returns_1_when_primary_deferred_and_nothing_scored(
    teams_yml_file, monkeypatch
):
    monkeypatch.setattr(sd, "scored_dates", lambda: set())

    def boom(_d):
        raise RuntimeError("actuals not ready")

    monkeypatch.setattr(sd, "fetch_ground_truth", boom)
    monkeypatch.setattr("sys.argv",
                        ["score_day.py", "--date", "2026-05-28", "--catch-up", "1"])
    assert sd.main() == 1
