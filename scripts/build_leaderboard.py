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
from datetime import date, datetime, timezone
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
WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def load_teams() -> dict[str, str]:
    data = yaml.safe_load(TEAMS_PATH.read_text())
    return {t["id"]: t["display_name"] for t in data.get("teams") or []}


def load_groups() -> dict[str, str]:
    """Mapping team_id -> Gruppenkürzel (Schlüssel ``group`` in teams.yml).

    Der Schlüssel ist optional; Teams ohne ``group`` (und Pseudo-Teams)
    erscheinen nicht im Mapping → die Leaderboard-Spalte zeigt einen Strich.
    """
    data = yaml.safe_load(TEAMS_PATH.read_text())
    return {t["id"]: t["group"]
            for t in (data.get("teams") or []) if t.get("group")}


def load_model_cards() -> list[dict[str, str | None]]:
    """Model-Card-Einträge für die Sektion „About the Models".

    Quelle ist ``teams.yml`` (Single Source of Truth): jedes reguläre Team
    erhält eine Zeile (``display_name`` + Links), in Dateireihenfolge. Fehlt
    der optionale Schlüssel ``model_card_link``, ist der Link ``None`` —
    das Template rendert dann „missing" mit Warn-Icon, damit sichtbar
    bleibt, wer noch keine Model Card veröffentlicht hat. Der optionale
    Schlüssel ``software_link`` (Spalte „Software") verweist auf das
    Reproduzierbarkeits-ZIP der Prognose-Software; ohne ihn rendert die
    Spalte einen Strich (keine Warnung — freiwillige Angabe). Der optionale
    Schlüssel ``certified`` (Spalte „Certified") trägt den vom Veranstalter
    gepflegten Reproduktions-Status: ``"Yes"`` → ✅, sonst (``"No"``/fehlt)
    ein Strich. Der optionale Schlüssel ``openssf`` (Spalte „OPENSSF")
    verlinkt die OpenSSF-Scorecard; fehlt er, rendert die Spalte „missing"
    mit Warn-Icon (analog zur Model Card). Pseudo-Teams (z. B. ``entsoe``)
    submitten kein eigenes Modell und entfallen.
    """
    data = yaml.safe_load(TEAMS_PATH.read_text())
    return [
        {"display_name": t["display_name"],
         "model_card_link": t.get("model_card_link"),
         "software_link": t.get("software_link"),
         "certified": t.get("certified"),
         "openssf": t.get("openssf")}
        for t in (data.get("teams") or []) if not t.get("pseudo", False)
    ]


def load_model_card_status() -> dict[str, bool | None]:
    """Model-Card-Status je Team für die Status-Spalte im Leaderboard.

    Gleiche Quelle wie die Sektion „About the Models" (``model_card_link``
    in ``teams.yml``) — beide bleiben damit automatisch synchron. ``True``
    = Link veröffentlicht (grüner Haken), ``False`` = fehlt (Warn-Icon),
    ``None`` für Pseudo-Teams (kein eigenes Modell → Strich statt
    Warnung).
    """
    data = yaml.safe_load(TEAMS_PATH.read_text())
    return {
        t["id"]: (None if t.get("pseudo", False)
                  else bool(t.get("model_card_link")))
        for t in (data.get("teams") or [])
    }


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
            "team_id", "display_name", "mean_mae", "mean_rmse", "mean_mape",
            "mean_bias", "mean_upr", "n_submissions",
        ])
    g = scores.groupby("team_id")
    mean_mae = g["mae"].mean().rename("mean_mae")
    # Sekundärmetriken (Anzeige) — das Ranking bleibt bei mean_mae.
    # Jeweils Mittel der Tageswerte, analog zur mittleren MAE:
    #   RMSE  — quadratische Fehlergröße (bestraft Ausreißer/Peaks stärker)
    #   MAPE  — De-facto-Referenzmetrik der Lastprognose-Praxis
    #   Bias  — Ø(Prognose − Ist), signiert; negativ = Unterprognose
    #   UPR   — Anteil Stunden mit Prognose < Ist [%]
    # (vgl. Möbius et al. 2023, arXiv:2302.11017)
    mean_rmse = g["rmse"].mean().rename("mean_rmse")
    mean_mape = g["mape"].mean().rename("mean_mape")
    mean_bias = g["bias"].mean().rename("mean_bias")
    mean_upr = g["upr"].mean().rename("mean_upr")
    n = g.size().rename("n_submissions")
    out = pd.concat([mean_mae, mean_rmse, mean_mape, mean_bias, mean_upr, n],
                    axis=1).reset_index()
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
    renders them as a dash. Pro Spalte (Zieltag) markieren ``best_<metrik>``
    die Tagesbesten (im Template fett); bei Gleichstand alle betroffenen
    Zellen. „Bester" heißt: kleinster Wert (MAE/RMSE/MAPE), am nächsten an
    0 (Bias) bzw. am nächsten an 50 % (UPR = ausgewogen).
    """
    if scores.empty:
        return {"dates": [], "date_meta": [], "teams": []}

    metrics = ["mae", "rmse", "mape", "bias", "upr"]
    dates = sorted(scores["target_date"].unique().tolist())
    # Anzeige-Metadaten je Zieltag: deutsches Kurzlabel („Di, 26.5.26")
    # und ISO-Wochen-Schlüssel für den Wochen-Umschalter im Template.
    date_meta: list[dict] = []
    for d in dates:
        dt = date.fromisoformat(str(d))
        iso = dt.isocalendar()
        date_meta.append({
            "date": str(d),
            "label": f"{WEEKDAYS_DE[dt.weekday()]}, "
                     f"{dt.day}.{dt.month}.{dt.year % 100:02d}",
            "week": f"{iso[0]}-W{iso[1]:02d}",
        })
    lookup = {(r["team_id"], r["target_date"]): r
              for r in scores.to_dict("records")}

    teams: list[dict] = []
    for team_id in rank_order:
        cells = []
        for d in dates:
            r = lookup.get((team_id, d))
            cell = {"carried": bool(r.get("carried_forward", False))
                    if r is not None else False}
            for m in metrics:
                v = r.get(m) if r is not None else None
                cell[m] = round(float(v), 2) \
                    if v is not None and pd.notna(v) else None
                cell[f"best_{m}"] = False
            cells.append(cell)
        teams.append({
            "team_id": team_id,
            "display_name": names.get(team_id, team_id),
            "cells": cells,
        })

    # Tagesbester je Spalte und Metrik: kleinster Schlüsselwert über alle
    # Teams. Schlüssel = Wert selbst (kleiner = besser) bzw. Distanz zum
    # Idealwert (Bias → 0, UPR → 50 %).
    keyfns = {"mae": lambda v: v, "rmse": lambda v: v, "mape": lambda v: v,
              "bias": abs, "upr": lambda v: abs(v - 50.0)}
    for m in metrics:
        key = keyfns[m]
        for j in range(len(dates)):
            col = [t["cells"][j][m] for t in teams
                   if t["cells"][j][m] is not None]
            if not col:
                continue
            best_key = min(key(v) for v in col)
            for t in teams:
                c = t["cells"][j]
                c[f"best_{m}"] = c[m] is not None and key(c[m]) == best_key

    return {"dates": dates, "date_meta": date_meta, "teams": teams}


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
            # Bias/UPR wie in score_day.score_submission (err = Prognose−Ist).
            "bias": round(float(np.mean(err)), 4),
            "upr": round(float(np.mean(err < 0) * 100), 4),
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
        "leaderboard_rmse": charts.fig_to_html(
            charts.fig_mean_rmse_bar(board, div_id="fig-leaderboard-rmse"),
            "fig-leaderboard-rmse"),
        "leaderboard_mape": charts.fig_to_html(
            charts.fig_mean_mape_bar(board, div_id="fig-leaderboard-mape"),
            "fig-leaderboard-mape"),
        "leaderboard_bias": charts.fig_to_html(
            charts.fig_mean_bias_bar(board, div_id="fig-leaderboard-bias"),
            "fig-leaderboard-bias"),
        "leaderboard_upr": charts.fig_to_html(
            charts.fig_mean_upr_bar(board, div_id="fig-leaderboard-upr"),
            "fig-leaderboard-upr"),
        "mae_time": charts.fig_to_html(
            charts.fig_mae_over_time(scores, names, div_id="fig-mae-time"),
            "fig-mae-time"),
    }


def annotate_leaderboard_best(rows: list[dict]) -> list[dict]:
    """Spaltenbeste im Leaderboard markieren (Fettdruck im Template).

    „Bester" je Spalte: kleinster Wert (Mean MAE/RMSE/MAPE), am nächsten
    an 0 (Bias), am nächsten an 50 % (UPR) bzw. größter Wert (Bewertete
    Tage). Verglichen wird auf Anzeige-Genauigkeit gerundet; Gleichstände
    markieren alle betroffenen Zeilen.
    """
    specs = [
        ("mean_mae", "best_mae", lambda v: round(v, 2)),
        ("mean_rmse", "best_rmse", lambda v: round(v, 2)),
        ("mean_mape", "best_mape", lambda v: round(v, 2)),
        ("mean_bias", "best_bias", lambda v: abs(round(v, 2))),
        ("mean_upr", "best_upr", lambda v: abs(round(v, 1) - 50.0)),
        ("n_submissions", "best_days", lambda v: -v),
    ]
    for col, flag, key in specs:
        keyed = [key(r[col]) for r in rows
                 if r.get(col) is not None and pd.notna(r[col])]
        best = min(keyed) if keyed else None
        for r in rows:
            v = r.get(col)
            r[flag] = (v is not None and pd.notna(v)
                       and best is not None and key(v) == best)
    return rows


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
    model_cards: list[dict[str, str]] | None = None,
    model_card_status: dict[str, bool | None] | None = None,
    groups: dict[str, str] | None = None,
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
    rows = annotate_leaderboard_best(board.to_dict(orient="records"))
    # Status-Spalte: Model Card vorhanden? Gleiche Quelle wie „About the
    # Models" (teams.yml) — None für Pseudo-Teams und unbekannte Ids.
    status = model_card_status or {}
    grp = groups or {}
    for r in rows:
        r["model_card_status"] = status.get(r["team_id"])
        r["group"] = grp.get(r["team_id"])
    html = template.render(
        rows=rows,
        daily=daily,
        figs=figs,
        plotlyjs=plotlyjs,
        logo_uri=logo_uri,
        model_cards=model_cards or [],
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    (PUBLIC_DIR / "index.html").write_text(html)
    # Reproduktions-Zertifikat-Vorlage zum Download bereitstellen: die
    # Markdown-Vorlage 1:1 nach public/ kopieren (von „About the Models" via
    # ./Certificate.md verlinkt). Fehlt sie, wird der Schritt übersprungen —
    # Graceful Degradation wie bei load_logo_uri().
    cert_src = TEMPLATE_DIR / "Certificate.md"
    if cert_src.exists():
        (PUBLIC_DIR / "Certificate.md").write_text(cert_src.read_text())
    (PUBLIC_DIR / "data" / "scores.json").write_text(
        json.dumps(rows, indent=2, default=str)
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
    render(board, daily, figs, logo_uri, load_model_cards(),
           load_model_card_status(), load_groups())
    print(f"[build] Leaderboard mit {len(board)} Teams "
          f"({len(daily['dates'])} bewertete Tage) -> public/index.html")


if __name__ == "__main__":
    main()
