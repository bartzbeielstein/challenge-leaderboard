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
from plotly.offline import get_plotlyjs

import charts


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


def daily_breakdown(
    scores: pd.DataFrame, names: dict[str, str], rank_order: list[str]
) -> dict:
    """Per-team per-day MAE/RMSE/MAPE breakdown for the secondary table.

    ``rank_order`` fixes the row order to match the main leaderboard; dates
    are sorted ascending so the most recent column is rightmost. Cells for
    a (team, date) pair without a score are returned as None — the template
    renders them as a dash.
    """
    if scores.empty:
        return {"dates": [], "teams": []}

    dates = sorted(scores["target_date"].unique().tolist())
    lookup = {(r["team_id"], r["target_date"]): r
              for r in scores.to_dict("records")}

    teams: list[dict] = []
    for team_id in rank_order:
        cells = []
        for d in dates:
            r = lookup.get((team_id, d))
            if r is None:
                cells.append({"mae": None, "rmse": None, "mape": None,
                              "carried": False})
            else:
                cells.append({
                    "mae": round(float(r["mae"]), 2),
                    "rmse": round(float(r["rmse"]), 2),
                    "mape": round(float(r["mape"]), 2),
                    "carried": bool(r.get("carried_forward", False)),
                })
        teams.append({
            "team_id": team_id,
            "display_name": names.get(team_id, team_id),
            "cells": cells,
        })
    return {"dates": dates, "teams": teams}


def build_figures(
    board: pd.DataFrame, daily: dict, scores: pd.DataFrame,
    names: dict[str, str],
) -> dict[str, str]:
    """Baut die eingebetteten Plotly-Fragmente.

    Pfade werden hier aus ``REPO_ROOT`` abgeleitet (nicht modulglobal),
    damit die Tests via ``monkeypatch.setattr(bl, "REPO_ROOT", tmp_path)``
    auch Submissions/Actuals isolieren. Fehlt ``data/actual_load.parquet``,
    liefert ``fig_forecast_vs_actual`` None und das Fragment ist '' — die
    Headline-Figur blendet sich sauber aus (Graceful Degradation).
    """
    actuals_path = REPO_ROOT / "data" / "actual_load.parquet"
    submissions_dir = REPO_ROOT / "submissions"
    actuals = charts.load_actuals(actuals_path)
    subs = charts.load_submissions(submissions_dir, list(names))
    return {
        "forecast": charts.fig_to_html(
            charts.fig_forecast_vs_actual(
                actuals, subs, scores, names, div_id="fig-forecast"),
            "fig-forecast"),
        "leaderboard": charts.fig_to_html(
            charts.fig_mean_mae_bar(board, div_id="fig-leaderboard"),
            "fig-leaderboard"),
        "mae_time": charts.fig_to_html(
            charts.fig_mae_over_time(scores, names, div_id="fig-mae-time"),
            "fig-mae-time"),
    }


def render(board: pd.DataFrame, daily: dict, figs: dict[str, str]) -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    (PUBLIC_DIR / "data").mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(),
    )
    template = env.get_template("leaderboard.html.j2")
    # Die ~4.8 MB Plotly-Bibliothek nur einbetten, wenn überhaupt eine Figur
    # gerendert wird (leeres Leaderboard → keine Charts → kein Bundle).
    plotlyjs = get_plotlyjs() if any(figs.values()) else ""
    html = template.render(
        rows=board.to_dict(orient="records"),
        daily=daily,
        figs=figs,
        plotlyjs=plotlyjs,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    (PUBLIC_DIR / "index.html").write_text(html)
    (PUBLIC_DIR / "data" / "scores.json").write_text(
        json.dumps(board.to_dict(orient="records"), indent=2, default=str)
    )
    (PUBLIC_DIR / "data" / "daily.json").write_text(
        json.dumps(daily, indent=2, default=str)
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
    daily = daily_breakdown(scores, names, list(board["team_id"]))
    figs = build_figures(board, daily, scores, names)
    render(board, daily, figs)
    print(f"[build] Leaderboard mit {len(board)} Teams "
          f"({len(daily['dates'])} bewertete Tage) -> public/index.html")


if __name__ == "__main__":
    main()
