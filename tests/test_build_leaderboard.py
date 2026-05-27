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


def test_main_handles_empty_scores(tmp_path):
    _seed_teams(tmp_path)
    # SCORES_PATH does not exist -> main must still render an (empty) page.
    bl.main()
    assert (tmp_path / "public" / "index.html").exists()
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    assert data == []
