"""Tests for scripts/build_leaderboard.py."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import build_leaderboard as bl


@pytest.fixture(autouse=True)
def isolate_paths(monkeypatch, tmp_path, repo_root):
    monkeypatch.setattr(bl, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bl, "SCORES_PATH", tmp_path / "data" / "scores.parquet")
    monkeypatch.setattr(bl, "TEAMS_PATH", tmp_path / "teams.yml")
    monkeypatch.setattr(bl, "PUBLIC_DIR", tmp_path / "public")
    # Re-use the real Jinja template (live behaviour) — that's the
    # contract we care about; only inputs/outputs are isolated.
    monkeypatch.setattr(bl, "TEMPLATE_DIR", repo_root / "templates")
    (tmp_path / "data").mkdir()
    yield


def _seed(tmp_path: Path, rows):
    df = pd.DataFrame(rows)
    df.to_parquet(tmp_path / "data" / "scores.parquet", index=False)


def _seed_teams(tmp_path: Path):
    import yaml
    (tmp_path / "teams.yml").write_text(yaml.safe_dump({
        "teams": [
            {"id": "team_4", "display_name": "Team 4", "github_handles": []},
            {"id": "hot_rod", "display_name": "Hot Rod", "github_handles": []},
            {"id": "neura", "display_name": "Team Neura", "github_handles": []},
        ]
    }))


def test_aggregate_ranks_by_mean_mae_then_n_submissions_desc(tmp_path):
    _seed_teams(tmp_path)
    scores = pd.DataFrame([
        {"team_id": "team_4", "mae": 1000.0},
        {"team_id": "team_4", "mae": 3000.0},  # mean 2000, n=2
        {"team_id": "hot_rod", "mae": 2000.0},  # mean 2000, n=1
        {"team_id": "neura", "mae": 500.0},     # mean 500, n=1
    ])
    names = bl.load_teams()
    out = bl.aggregate(scores, names)
    assert list(out["team_id"]) == ["neura", "team_4", "hot_rod"]
    # tie at 2000 -> more submissions ranks higher
    assert list(out["rank"]) == [1, 2, 3]


def test_main_writes_html_and_json(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 200.0,
         "rmse": 200.0, "mape": 0.2},
    ])
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert "Team 4" in html
    assert "Hot Rod" in html
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    assert [r["team_id"] for r in data] == ["team_4", "hot_rod"]
    assert data[0]["rank"] == 1


def test_daily_breakdown_pivots_by_team_and_date():
    scores = pd.DataFrame([
        {"team_id": "team_4", "target_date": "2026-05-12",
         "mae": 1931.26, "rmse": 2362.61, "mape": 3.66, "carried_forward": False},
        {"team_id": "team_4", "target_date": "2026-05-26",
         "mae": 3466.19, "rmse": 4090.82, "mape": 6.74, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26",
         "mae": 1961.51, "rmse": 2236.25, "mape": 3.77, "carried_forward": True},
    ])
    names = {"team_4": "Team 4", "hot_rod": "Hot Rod"}
    out = bl.daily_breakdown(scores, names, ["hot_rod", "team_4"])
    assert out["dates"] == ["2026-05-12", "2026-05-26"]
    assert [t["team_id"] for t in out["teams"]] == ["hot_rod", "team_4"]
    assert out["teams"][0]["cells"][0]["mae"] is None
    assert out["teams"][0]["cells"][1]["mae"] == 1961.51
    assert out["teams"][0]["cells"][1]["carried"] is True
    assert out["teams"][1]["cells"][0]["mae"] == 1931.26
    assert out["teams"][1]["cells"][1]["carried"] is False


def test_main_writes_daily_section_in_html(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-12", "mae": 1931.26,
         "rmse": 2362.61, "mape": 3.66, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 1961.51,
         "rmse": 2236.25, "mape": 3.77, "carried_forward": False},
    ])
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert "Tagesfehler je Team" in html
    assert "2026-05-12" in html
    assert "2026-05-26" in html
    daily = json.loads((tmp_path / "public" / "data" / "daily.json").read_text())
    assert daily["dates"] == ["2026-05-12", "2026-05-26"]


def test_main_handles_empty_scores(tmp_path):
    _seed_teams(tmp_path)
    # SCORES_PATH does not exist -> main must still render an (empty) page.
    bl.main()
    assert (tmp_path / "public" / "index.html").exists()
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    assert data == []


# --------------------------------------------------------------------------
# Charts: embedded, with graceful degradation for the actuals-dependent one.
# --------------------------------------------------------------------------

def _write_actuals(tmp_path, date: str):
    ts = pd.date_range(f"{date}T00:00:00Z", periods=24, freq="h", tz="UTC")
    pd.DataFrame({
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "load_mw": [1000.0 + i for i in range(24)],
    }).to_parquet(tmp_path / "data" / "actual_load.parquet", index=False)


def _write_submission(tmp_path, team: str, date: str):
    d = tmp_path / "submissions" / team
    d.mkdir(parents=True, exist_ok=True)
    ts = pd.date_range(f"{date}T00:00:00Z", periods=24, freq="h", tz="UTC")
    pd.DataFrame({
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forecast_mw": [1010.0 + i for i in range(24)],
    }).to_csv(d / f"{date}.csv", index=False)


def test_main_embeds_charts_but_not_forecast_without_actuals(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 200.0,
         "rmse": 200.0, "mape": 0.2, "carried_forward": False},
    ])
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert 'id="fig-leaderboard"' in html
    assert 'id="fig-mae-time"' in html
    assert 'id="fig-forecast"' not in html       # no actuals -> self-disabled
    assert "Prognose vs. Ist-Last" not in html
    # The Plotly library bundle is embedded exactly once.
    assert html.count("plotly.js v") == 1


def test_main_renders_forecast_chart_when_actuals_present(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
    ])
    _write_actuals(tmp_path, "2026-05-26")
    _write_submission(tmp_path, "team_4", "2026-05-26")
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert 'id="fig-forecast"' in html
    assert "Prognose vs. Ist-Last" in html


def test_main_empty_scores_does_not_embed_plotly_bundle(tmp_path):
    _seed_teams(tmp_path)
    bl.main()  # no scores -> no charts -> no 4.8 MB bundle
    html = (tmp_path / "public" / "index.html").read_text()
    assert "plotly.js v" not in html
