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

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from plotly.offline import get_plotlyjs

import charts


REPO_ROOT = Path(__file__).resolve().parent.parent
SCORES_PATH = REPO_ROOT / "data" / "scores.parquet"
TEAMS_PATH = REPO_ROOT / "teams.yml"
PUBLIC_DIR = REPO_ROOT / "public"
ENTSOE_BASELINE_ID = "entsoe"
TEMPLATE_DIR = REPO_ROOT / "templates"


def load_teams() -> dict[str, str]:
    data = yaml.safe_load(TEAMS_PATH.read_text())
    return {t["id"]: t["display_name"] for t in data.get("teams") or []}


def load_pseudo_ids() -> set[str]:
    """Ids der Pseudo-Teams (``pseudo: true`` in teams.yml).

    Pseudo-Teams submitten keine CSVs; ihre Scores werden zur Build-Zeit
    direkt aus den committeten ENTSO-E-Daten abgeleitet (s.
    ``entsoe_pseudo_scores``). Persistierte Zeilen mit diesen Ids in
    ``scores.parquet`` werden ignoriert — die Ableitung ist autoritativ.
    """
    data = yaml.safe_load(TEAMS_PATH.read_text())
    return {t["id"] for t in (data.get("teams") or []) if t.get("pseudo", False)}


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
    renders them as a dash. Pro Spalte (Zieltag) markiert ``best: True``
    die kleinste MAE (Tagesbester, im Template fett); bei Gleichstand alle
    betroffenen Zellen.
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
                              "carried": False, "best": False})
            else:
                cells.append({
                    "mae": round(float(r["mae"]), 2),
                    "rmse": round(float(r["rmse"]), 2),
                    "mape": round(float(r["mape"]), 2),
                    "carried": bool(r.get("carried_forward", False)),
                    "best": False,
                })
        teams.append({
            "team_id": team_id,
            "display_name": names.get(team_id, team_id),
            "cells": cells,
        })

    # Tagesbester je Spalte: kleinste (gerundete) MAE über alle Teams.
    for j in range(len(dates)):
        col = [t["cells"][j]["mae"] for t in teams
               if t["cells"][j]["mae"] is not None]
        if not col:
            continue
        best = min(col)
        for t in teams:
            c = t["cells"][j]
            c["best"] = c["mae"] is not None and c["mae"] == best

    return {"dates": dates, "teams": teams}


def entsoe_pseudo_scores(
    actuals: pd.DataFrame | None, scored_dates: set[str]
) -> pd.DataFrame:
    """Tages-Scores des Pseudo-Teams ``entsoe`` direkt aus den ENTSO-E-Daten.

    Berechnet MAE/RMSE/MAPE der ``entsoe_forecast_mw``-Spalte aus
    ``data/actual_load.parquet`` gegen den Ist-Load — zur Build-Zeit aus den
    committeten Daten abgeleitet (kein API-Key), nicht in ``scores.parquet``
    persistiert. Die Ableitung ist **autoritativ**: persistierte
    ``entsoe``-Zeilen werden in ``main()`` vorab verworfen.

    Zeitraum-Sync: nur Tage aus ``scored_dates`` (= die Zieltage, für die
    die regulären Teams Scores haben) — entsoe ist damit exakt über
    denselben Zeitraum bewertet wie alle anderen, nie mehr, nie weniger.
    Unvollständige Tage (<24 Stunden Forecast+Load) werden übersprungen
    und geloggt.
    """
    if (actuals is None or actuals.empty
            or "entsoe_forecast_mw" not in actuals.columns):
        return pd.DataFrame()
    df = actuals.dropna(subset=["load_mw", "entsoe_forecast_mw"]).copy()
    if df.empty:
        return pd.DataFrame()
    df["target_date"] = df["timestamp_utc"].str.slice(0, 10)

    rows: list[dict] = []
    for d, g in df.groupby("target_date"):
        if d not in scored_dates:
            continue
        if len(g) < 24:
            print(f"[build] entsoe: {d} übersprungen "
                  f"({len(g)}/24 Stunden Forecast+Load)")
            continue
        actual = g["load_mw"].to_numpy(dtype=float)
        err = g["entsoe_forecast_mw"].to_numpy(dtype=float) - actual
        nz = actual != 0
        rows.append({
            "team_id": ENTSOE_BASELINE_ID,
            "target_date": d,
            "mae": round(float(np.mean(np.abs(err))), 4),
            "rmse": round(float(np.sqrt(np.mean(err ** 2))), 4),
            "mape": round(float(np.mean(np.abs(err[nz] / actual[nz])) * 100), 4)
                    if nz.any() else float("nan"),
            "carried_forward": False,
            "source_date": d,
        })
    return pd.DataFrame(rows)


def build_figures(
    board: pd.DataFrame, daily: dict, scores: pd.DataFrame,
    names: dict[str, str], actuals: pd.DataFrame | None,
) -> dict[str, str]:
    """Baut die eingebetteten Plotly-Fragmente.

    ``actuals`` wird vom Aufrufer geladen (für die Baseline wiederverwendet).
    Submissions-Pfad wird aus ``REPO_ROOT`` abgeleitet, damit die Tests via
    ``monkeypatch.setattr(bl, "REPO_ROOT", tmp_path)`` isolieren. Liegt keine
    Actuals-Datei vor, ist ``actuals`` None und ``fig_forecast_vs_actual``
    liefert None — die Headline-Figur blendet sich sauber aus.
    """
    submissions_dir = REPO_ROOT / "submissions"
    # Pseudo-Teams haben keine Submissions — ausschließen, damit ein evtl.
    # liegengebliebenes Verzeichnis keine doppelte Chart-Spur erzeugt (die
    # gestrichelte ENTSO-E-Spur kommt direkt aus actual_load.parquet).
    pseudo_ids = load_pseudo_ids()
    subs = charts.load_submissions(
        submissions_dir, [tid for tid in names if tid not in pseudo_ids])
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


def load_logo_uri(path: Path) -> str:
    """Logo als base64-Data-URI (self-contained, wie das Plotly-Bundle).

    '' wenn die Datei fehlt — der Hero rendert dann ohne Logo (Graceful
    Degradation). Vermeidet einen separaten Asset-Copy-Schritt nach
    ``public/`` und funktioniert offline.
    """
    if not Path(path).exists():
        return ""
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def render(
    board: pd.DataFrame, daily: dict, figs: dict[str, str], logo_uri: str = "",
) -> None:
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
        logo_uri=logo_uri,
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
    # Pseudo-Teams: evtl. persistierte Zeilen verwerfen — die Build-Zeit-
    # Ableitung aus den ENTSO-E-Daten ist autoritativ. Der Datums-Raum
    # (scored_dates) wird DANACH bestimmt, damit ein Pseudo-Team nie den
    # eigenen Bewertungszeitraum definiert (Sync mit den echten Teams).
    pseudo_ids = load_pseudo_ids()
    if not scores.empty and pseudo_ids:
        scores = scores[~scores["team_id"].isin(pseudo_ids)].copy()
    scored = set(scores["target_date"].astype(str)) if not scores.empty else set()
    actuals = charts.load_actuals(REPO_ROOT / "data" / "actual_load.parquet")
    # ENTSO-E-Day-ahead-Prognose als gerankter Pseudo-Team-Eintrag.
    pseudo = entsoe_pseudo_scores(actuals, scored)
    if not pseudo.empty:
        scores = pd.concat([scores, pseudo], ignore_index=True)
    board = aggregate(scores, names)
    daily = daily_breakdown(scores, names, list(board["team_id"]))
    figs = build_figures(board, daily, scores, names, actuals)
    logo_uri = load_logo_uri(REPO_ROOT / "logo" / "spotlogo.png")
    render(board, daily, figs, logo_uri)
    print(f"[build] Leaderboard mit {len(board)} Teams "
          f"({len(daily['dates'])} bewertete Tage) -> public/index.html")


if __name__ == "__main__":
    main()
