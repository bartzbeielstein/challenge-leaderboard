"""
build_leaderboard.py

Liest data/scores.parquet, aggregiert pro Team die durchschnittliche
MAE (Summe der MAEs / Anzahl bewerteter Tage) und rendert
public/index.html sowie public/data/scores.json.

Ranking-Logik:
  Hauptranking   = aufsteigend nach mittlerer MAE
  Tie-Break      = absteigend nach Anzahl bewerteter Tage
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape


REPO_ROOT = Path(__file__).resolve().parent.parent
SCORES_PATH = REPO_ROOT / "data" / "scores.parquet"
TEAMS_PATH = REPO_ROOT / "teams.yml"
PUBLIC_DIR = REPO_ROOT / "public"
TEMPLATE_DIR = REPO_ROOT / "templates"


def load_teams() -> dict[str, str]:
    data = yaml.safe_load(TEAMS_PATH.read_text())
    return {t["id"]: t["display_name"] for t in data.get("teams") or []}


def aggregate(scores: pd.DataFrame, names: dict[str, str]) -> pd.DataFrame:
    if scores.empty:
        return pd.DataFrame(columns=[
            "team_id", "display_name", "mean_mae", "sum_mae", "n_submissions",
        ])
    sum_mae = scores.groupby("team_id")["mae"].sum().rename("sum_mae")
    mean_mae = scores.groupby("team_id")["mae"].mean().rename("mean_mae")
    n = scores.groupby("team_id").size().rename("n_submissions")
    out = pd.concat([mean_mae, sum_mae, n], axis=1).reset_index()
    out["display_name"] = out["team_id"].map(names).fillna(out["team_id"])
    out = out.sort_values(
        ["mean_mae", "n_submissions"],
        ascending=[True, False],
        na_position="last",
    ).reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


def render(board: pd.DataFrame) -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    (PUBLIC_DIR / "data").mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(),
    )
    template = env.get_template("leaderboard.html.j2")
    html = template.render(
        rows=board.to_dict(orient="records"),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    (PUBLIC_DIR / "index.html").write_text(html)
    (PUBLIC_DIR / "data" / "scores.json").write_text(
        json.dumps(board.to_dict(orient="records"), indent=2, default=str)
    )


def main() -> None:
    names = load_teams()
    if SCORES_PATH.exists():
        scores = pd.read_parquet(SCORES_PATH)
    else:
        scores = pd.DataFrame(columns=[
            "team_id", "target_date", "scored_at_utc", "mae", "rmse", "mape",
        ])
    board = aggregate(scores, names)
    render(board)
    print(f"[build] Leaderboard mit {len(board)} Teams gerendert -> public/index.html")


if __name__ == "__main__":
    main()
