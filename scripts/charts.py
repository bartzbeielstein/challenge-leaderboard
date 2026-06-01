"""charts.py

Plotly-Figuren für das Leaderboard. Reine Funktionen: nehmen bereits
geladene Daten entgegen und liefern ``go.Figure | None`` (None, wenn die
Datengrundlage fehlt — der Build blendet die Figur dann sauber aus).
Kein Datei-Schreiben, kein API-Key. Eingebettet wird über
``fig_to_html`` (vgl. ``figure.py`` aus dem energy-demand-forecast-Projekt:
``lines+markers``, Metriken in der Legende, schlankes Layout).

Determinismus (CR-2): explizite ``div_id`` (statt zufälliger UUID),
sortierte Iteration über Teams/Tage, gerundete Legenden-Metriken.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

# Dezente, deterministische Palette für Team-Linien (Plotly-Default-Reihenfolge
# ist stabil; hier explizit für Wiedererkennbarkeit über Figuren hinweg).
TEAM_COLORS = [
    "#4f46e5", "#0891b2", "#16a34a", "#d97706", "#dc2626",
    "#9333ea", "#0d9488", "#ca8a04", "#db2777", "#2563eb",
    "#65a30d", "#e11d48", "#7c3aed", "#059669", "#ea580c",
]
ACTUAL_COLOR = "#111827"
_LEGEND = dict(orientation="h", xanchor="center", x=0.5, yanchor="top", y=-0.18)


def _team_color(i: int) -> str:
    return TEAM_COLORS[i % len(TEAM_COLORS)]


def _base_layout(fig: go.Figure, *, title: str, yaxis_title: str) -> None:
    fig.update_layout(
        title=title,
        autosize=True,
        height=480,
        margin=dict(l=60, r=30, t=60, b=80),
        legend=_LEGEND,
        yaxis_title=yaxis_title,
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(family="-apple-system, Segoe UI, Roboto, Helvetica, Arial, "
                         "sans-serif"),
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eef0f3")
    fig.update_yaxes(showgrid=True, gridcolor="#eef0f3")


# --------------------------------------------------------------------------
# Daten-Loader (nur committete Dateien, kein API-Key)
# --------------------------------------------------------------------------

def load_actuals(path: Path) -> pd.DataFrame | None:
    """``data/actual_load.parquet`` → DataFrame[ts(datetime, UTC), date, load_mw].

    None, wenn die Datei fehlt (Graceful Degradation: Prognose-vs-Ist-Figur
    wird dann ausgelassen).
    """
    if not Path(path).exists():
        return None
    df = pd.read_parquet(path)
    if df.empty or "timestamp_utc" not in df.columns:
        return None
    df = df.copy()
    df["ts"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
    df["load_mw"] = df["load_mw"].astype(float)
    return df.sort_values("ts").reset_index(drop=True)


def load_submissions(
    submissions_dir: Path, team_ids: list[str]
) -> dict[str, dict[str, pd.DataFrame]]:
    """Lädt ``submissions/<team_id>/<date>.csv`` für die gegebenen Teams.

    Liefert ``{team_id: {date_str: DataFrame[ts(datetime), forecast_mw]}}``.
    Fehlende Verzeichnisse/Dateien werden ausgelassen; Iteration sortiert
    (Determinismus).
    """
    submissions_dir = Path(submissions_dir)
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for team_id in sorted(team_ids):
        team_dir = submissions_dir / team_id
        if not team_dir.is_dir():
            continue
        per_date: dict[str, pd.DataFrame] = {}
        for csv in sorted(team_dir.glob("*.csv")):
            date_str = csv.stem
            try:
                df = pd.read_csv(csv)
                df["ts"] = pd.to_datetime(df["timestamp_utc"], utc=True)
                df["forecast_mw"] = df["forecast_mw"].astype(float)
            except Exception:
                continue
            per_date[date_str] = df[["ts", "forecast_mw"]].sort_values("ts")
        if per_date:
            out[team_id] = per_date
    return out


# --------------------------------------------------------------------------
# Chart 1: Prognose vs. Ist-Last (Headline, figure.py-Stil)
# --------------------------------------------------------------------------

def fig_forecast_vs_actual(
    actuals: pd.DataFrame | None,
    submissions: dict[str, dict[str, pd.DataFrame]],
    scores: pd.DataFrame,
    names: dict[str, str],
    *,
    div_id: str = "fig-forecast",
) -> go.Figure | None:
    """Ist-Last + 24h-Prognose jedes Teams für einen wählbaren Zieltag.

    Datumsauswahl per Plotly-``updatemenus``-Dropdown: alle verfügbaren
    Tage werden vorgerendert, der Button schaltet die ``visible``-Maske um
    (voll statische Seite, kein eigenes JS). Default sichtbar: jüngster Tag.
    None, wenn keine Actuals vorliegen oder kein Tag mit Submissions
    überlappt.
    """
    if actuals is None or actuals.empty:
        return None

    actual_by_date = {d: g for d, g in actuals.groupby("date")}
    # Tage mit Actuals UND mindestens einer Submission, aufsteigend.
    sub_dates = {d for per in submissions.values() for d in per}
    days = sorted(d for d in actual_by_date if d in sub_dates)
    if not days:
        return None

    mae_lookup: dict[tuple[str, str], float] = {}
    if scores is not None and not scores.empty:
        for r in scores.to_dict("records"):
            mae_lookup[(r["team_id"], str(r["target_date"]))] = float(r["mae"])

    fig = go.Figure()
    day_trace_idx: dict[str, list[int]] = {d: [] for d in days}
    idx = 0
    for d in days:
        a = actual_by_date[d]
        fig.add_trace(go.Scatter(
            x=a["ts"], y=a["load_mw"], mode="lines+markers",
            name="Ist-Last",
            line=dict(color=ACTUAL_COLOR, width=3),
            marker=dict(size=5),
            visible=False,
        ))
        day_trace_idx[d].append(idx)
        idx += 1
        for ci, team_id in enumerate(sorted(submissions)):
            if d not in submissions[team_id]:
                continue
            sdf = submissions[team_id][d]
            label = names.get(team_id, team_id)
            mae = mae_lookup.get((team_id, d))
            if mae is not None:
                label = f"{label} · MAE {mae:.0f}"
            fig.add_trace(go.Scatter(
                x=sdf["ts"], y=sdf["forecast_mw"], mode="lines+markers",
                name=label,
                line=dict(color=_team_color(ci), width=1.6),
                marker=dict(size=4),
                visible=False,
            ))
            day_trace_idx[d].append(idx)
            idx += 1

    default_day = days[-1]
    for i in day_trace_idx[default_day]:
        fig.data[i].visible = True

    n = len(fig.data)
    if len(days) > 1:
        buttons = []
        for d in days:
            vis = [False] * n
            for i in day_trace_idx[d]:
                vis[i] = True
            buttons.append(dict(
                label=d, method="update",
                args=[{"visible": vis},
                      {"title": f"Prognose vs. Ist-Last — {d}"}],
            ))
        fig.update_layout(updatemenus=[dict(
            buttons=buttons, direction="down", showactive=True,
            x=1.0, xanchor="right", y=1.18, yanchor="top",
            active=len(days) - 1,
        )])

    _base_layout(fig, title=f"Prognose vs. Ist-Last — {default_day}",
                 yaxis_title="Last [MW]")
    return fig


# --------------------------------------------------------------------------
# Chart 2: Mittlere MAE je Team (horizontales Balkendiagramm)
# --------------------------------------------------------------------------

def fig_mean_mae_bar(
    board: pd.DataFrame, *, div_id: str = "fig-leaderboard"
) -> go.Figure | None:
    """Horizontale Balken der mittleren MAE je Team (Rang 1 oben, grün=gut)."""
    if board is None or board.empty:
        return None
    names = board["display_name"].tolist()
    mae = board["mean_mae"].astype(float).tolist()
    fig = go.Figure(go.Bar(
        x=mae, y=names, orientation="h",
        text=[f"{v:.0f}" for v in mae], textposition="auto",
        marker=dict(
            color=mae, colorscale="RdYlGn", reversescale=True,
            line=dict(color="rgba(0,0,0,0.08)", width=1),
            showscale=False,
        ),
        hovertemplate="%{y}<br>Ø MAE = %{x:.1f} MW<extra></extra>",
    ))
    # board ist aufsteigend nach mean_mae (Rang 1 zuerst) → reversed, damit
    # der beste Balken oben steht.
    fig.update_yaxes(autorange="reversed")
    _base_layout(fig, title="Mittlere MAE je Team", yaxis_title="")
    fig.update_layout(showlegend=False, hovermode="closest",
                      xaxis_title="Ø MAE [MW]")
    return fig


# --------------------------------------------------------------------------
# Chart 3: MAE-Verlauf je Team (Liniendiagramm)
# --------------------------------------------------------------------------

def fig_mae_over_time(
    scores: pd.DataFrame, names: dict[str, str], *, div_id: str = "fig-mae-time"
) -> go.Figure | None:
    """Tages-MAE je Team über die Zeit; LOCF-Punkte als offene Marker."""
    if scores is None or scores.empty:
        return None
    df = scores.copy()
    df["target_date"] = df["target_date"].astype(str)
    df["mae"] = df["mae"].astype(float)
    if "carried_forward" not in df.columns:
        df["carried_forward"] = False
    # Team-Reihenfolge: aufsteigend nach mittlerer MAE (wie Leaderboard).
    order = (df.groupby("team_id")["mae"].mean().sort_values().index.tolist())

    fig = go.Figure()
    for ci, team_id in enumerate(order):
        g = df[df["team_id"] == team_id].sort_values("target_date")
        symbols = ["circle-open" if c else "circle"
                   for c in g["carried_forward"]]
        fig.add_trace(go.Scatter(
            x=g["target_date"], y=g["mae"], mode="lines+markers",
            name=names.get(team_id, team_id),
            line=dict(color=_team_color(ci), width=2),
            marker=dict(size=8, symbol=symbols,
                        line=dict(color=_team_color(ci), width=1.5)),
            hovertemplate=(names.get(team_id, team_id)
                           + "<br>%{x}: MAE %{y:.0f} MW<extra></extra>"),
        ))
    _base_layout(fig, title="MAE-Verlauf je Team", yaxis_title="MAE [MW]")
    fig.update_xaxes(type="category")
    return fig


# --------------------------------------------------------------------------
# Einbettung
# --------------------------------------------------------------------------

def fig_to_html(fig: go.Figure | None, div_id: str) -> str:
    """'' wenn ``fig is None``, sonst eingebettetes HTML-Fragment.

    ``include_plotlyjs=False`` — die Bibliothek wird einmal im
    ``<head>`` des Templates eingebettet (self-contained). Stabile
    ``div_id`` für bitweise reproduzierbare Ausgabe (CR-2).
    """
    if fig is None:
        return ""
    return fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        div_id=div_id,
        config={"displaylogo": False, "responsive": True},
    )
