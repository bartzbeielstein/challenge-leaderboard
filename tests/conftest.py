"""Shared fixtures for the challenge-leaderboard test suite.

Tests are hermetic: no ENTSO-E network calls, no GitHub API, no
mutations of the real `data/scores.parquet`. All filesystem work
happens under `tmp_path`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def make_submission_csv(tmp_path):
    """Factory: write a 24-row submission CSV under tmp_path/<rel>."""

    def _factory(rel: str, *, date: str | None = None,
                 forecast=None, rows: int | None = None,
                 columns: list[str] | None = None) -> Path:
        target_date = date or rel.split("/")[-1].replace(".csv", "")
        n = rows if rows is not None else 24
        cols = columns or ["timestamp_utc", "forecast_mw"]
        stamps = pd.date_range(
            f"{target_date}T00:00:00Z", periods=n, freq="h", tz="UTC"
        ).strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
        fc = forecast if forecast is not None else [1000.0 + i * 10.0 for i in range(n)]
        df = pd.DataFrame({cols[0]: stamps, cols[1]: fc})
        out = tmp_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False)
        return out

    return _factory


@pytest.fixture
def teams_yml(tmp_path) -> Path:
    """Minimal teams.yml under tmp_path with team_4 (bartzbeielstein) and friends."""
    p = tmp_path / "teams.yml"
    p.write_text(
        yaml.safe_dump({
            "teams": [
                {"id": "team_4", "display_name": "Team 4",
                 "github_handles": ["bartzbeielstein"]},
                {"id": "hot_rod", "display_name": "Hot Rod",
                 "github_handles": ["someone-else"]},
                {"id": "neura", "display_name": "Team Neura",
                 "github_handles": ["nobody"]},
                {"id": "entsoe", "display_name": "ENTSO-E",
                 "pseudo": True},
            ]
        })
    )
    return p
