"""generate_certificates.py

Urkunden für die Live-Lastprognose-Challenge erzeugen — eine PDF-Seite
pro Team, getrennt nach den beiden Wertungsgruppen ING und AIT (separate
Siegerehrung je Gruppe). Plätze 1-3 je Gruppe erhalten eine Platzierungs-
Urkunde, alle übrigen Teams eine Teilnahme-Urkunde.

Ranking = Live-Leaderboard-Logik (build_leaderboard.py): mittlere MAE
aufsteigend über alle bewerteten Zieltage ab RESTART_DATE, Tie-Break
absteigend nach Anzahl bewerteter Tage. ENTSO-E-Geschwister-Einträge
(``*_entsoe``), Pseudo-, Retired- und Veranstalter-Teams (Gruppe ``--``)
sind nicht preisberechtigt.

Jede Urkunde trägt eine team-individuelle Grafik aus den committeten
ENTSO-E-Daten (Transparency Platform, Actual Total Load DE-LU):
links der beste Prognosetag des Teams (Ist vs. Prognose), rechts der
MAE-Verlauf des Teams im Gruppenfeld inkl. ENTSO-E-Day-ahead-Baseline.

Aufruf (matplotlib ist bewusst KEINE Projekt-Abhängigkeit):

    uv run --with matplotlib python scripts/generate_certificates.py
    uv run --with matplotlib python scripts/generate_certificates.py --date "July 21st 2026"

Eingaben:
  data/scores.parquet, data/actual_load.parquet, teams.yml,
  submissions/<team>/<tag>.csv,
  local/certificates/names.yml     (private Mitgliederliste, s. Vorlage)
  local/certificates/assets/       (signature.png, spotseven_logo.jpg)
Ausgabe:
  local/certificates/out/<GRUPPE>_<rang>_<team_id>.pdf  (+ .typ, .svg)

Benötigt Typst (über Quarto gebündelt: ``quarto typst compile``).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_day import RESTART_DATE  # noqa: E402  — Phasen-Schnitt wie im Leaderboard

REPO_ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = REPO_ROOT / "local" / "certificates"
ASSETS = CERT_DIR / "assets"
NAMES_YML = CERT_DIR / "names.yml"

AWARD_GROUPS = ("ING", "AIT")
ENTSOE_BASELINE_ID = "entsoe"

# TH-Köln-nahe Palette (Balken oben, Akzente)
COL_DARKRED = "#9C1006"
COL_ORANGE = "#EA5B0C"
COL_MAGENTA = "#E6007E"
COL_INK = "#1A1A1A"
MEDAL = {1: "#C9A227", 2: "#8E9196", 3: "#A05822"}  # Gold, Silber, Bronze

ORDINAL = {1: "1st", 2: "2nd", 3: "3rd"}


# ---------------------------------------------------------------------------
# Daten
# ---------------------------------------------------------------------------

def load_standings() -> pd.DataFrame:
    """Finale Rangfolge je Wertungsgruppe (plus Referenz-Zeilen).

    Gleiche Aggregation wie build_leaderboard.aggregate: mean MAE über die
    Live-Phase, Tie-Break n absteigend. Liefert nur preisberechtigte Teams
    (Gruppe ING/AIT, kein ``*_entsoe``, kein pseudo/retired) mit Spalte
    ``rank`` innerhalb der eigenen Gruppe.
    """
    scores = pd.read_parquet(REPO_ROOT / "data" / "scores.parquet")
    live = scores[scores["target_date"] >= str(RESTART_DATE)].copy()

    teams = yaml.safe_load((REPO_ROOT / "teams.yml").read_text())["teams"]
    meta = {t["id"]: t for t in teams}

    g = (live.groupby("team_id")
         .agg(mean_mae=("mae", "mean"), mean_rmse=("rmse", "mean"),
              mean_mape=("mape", "mean"), n_days=("mae", "size"))
         .reset_index())
    g["group"] = g["team_id"].map(lambda t: meta.get(t, {}).get("group"))
    g["display_name"] = g["team_id"].map(
        lambda t: meta.get(t, {}).get("display_name", t))
    eligible = (
        g["group"].isin(AWARD_GROUPS)
        & ~g["team_id"].str.endswith("_entsoe")
        & ~g["team_id"].map(lambda t: bool(meta.get(t, {}).get("pseudo")))
        & ~g["team_id"].map(lambda t: bool(meta.get(t, {}).get("retired")))
    )
    out = g[eligible].sort_values(["mean_mae", "n_days"],
                                  ascending=[True, False]).copy()
    out["rank"] = out.groupby("group").cumcount() + 1
    return out, live, meta


def entsoe_baseline_mae(live: pd.DataFrame) -> float | None:
    """Mittlere Tages-MAE des offiziellen ENTSO-E-Day-ahead-Forecasts.

    Die Pseudo-Team-Scores stehen NICHT in scores.parquet (sie werden vom
    Leaderboard zur Build-Zeit abgeleitet); hier direkt aus
    actual_load.parquet berechnet — gleiche Logik, gleicher Zeitraum.
    """
    actual = pd.read_parquet(REPO_ROOT / "data" / "actual_load.parquet")
    actual["date"] = actual["timestamp_utc"].str[:10]
    days = set(live["target_date"].astype(str))
    sub = actual[actual["date"].isin(days)].dropna(
        subset=["load_mw", "entsoe_forecast_mw"])
    if sub.empty:
        return None
    daily = (sub.assign(ae=(sub["entsoe_forecast_mw"] - sub["load_mw"]).abs())
             .groupby("date")["ae"].mean())
    return float(daily.mean())


def best_day(live: pd.DataFrame, team_id: str) -> tuple[str, float] | None:
    rows = live[(live["team_id"] == team_id)
                & (live["carried_forward"].ne(True))]
    if rows.empty:
        return None
    r = rows.loc[rows["mae"].idxmin()]
    return str(r["target_date"]), float(r["mae"])


# ---------------------------------------------------------------------------
# Grafik (matplotlib — bewusst erst hier importiert)
# ---------------------------------------------------------------------------

def make_figure(team_id: str, display_name: str, group: str,
                live: pd.DataFrame, baseline: float | None,
                out_svg: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    matplotlib.rcParams["font.family"] = ["Helvetica Neue", "Helvetica",
                                          "Arial", "sans-serif"]

    actual = pd.read_parquet(REPO_ROOT / "data" / "actual_load.parquet")
    actual["ts"] = pd.to_datetime(actual["timestamp_utc"])

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(5.4, 5.8), dpi=200,
        gridspec_kw={"height_ratios": [1.0, 1.0], "hspace": 0.52})
    for ax in (ax1, ax2):
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.tick_params(labelsize=7, colors="#555555")
        for s in ("left", "bottom"):
            ax.spines[s].set_color("#BBBBBB")

    # -- Panel 1: bester Prognosetag (Ist vs. Prognose) ---------------------
    bd = best_day(live, team_id)
    if bd is not None:
        day, day_mae = bd
        sub = actual[actual["timestamp_utc"].str.startswith(day)]
        fc_path = REPO_ROOT / "submissions" / team_id / f"{day}.csv"
        hours = range(24)
        ax1.plot(hours, sub["load_mw"] / 1000.0, color=COL_INK, lw=1.8,
                 label="Actual load (ENTSO-E)", zorder=3)
        if fc_path.exists():
            fc = pd.read_csv(fc_path)
            ax1.plot(hours, fc["forecast_mw"] / 1000.0, color=COL_MAGENTA,
                     lw=1.8, ls=(0, (4, 1.6)), label=f"{display_name} forecast",
                     zorder=4)
            ax1.fill_between(hours, sub["load_mw"] / 1000.0,
                             fc["forecast_mw"] / 1000.0,
                             color=COL_MAGENTA, alpha=0.10, zorder=2)
        ax1.set_title(
            f"Best forecast day — {day}  (MAE {day_mae:,.0f} MW)",
            fontsize=8.5, color=COL_INK, loc="left", pad=6)
        ax1.set_xlabel("Hour of day (UTC)", fontsize=7, color="#555555")
        ax1.set_ylabel("Load [GW]", fontsize=7, color="#555555")
        ax1.set_xticks([0, 6, 12, 18, 23])
        ax1.legend(fontsize=6.5, frameon=False, loc="best")

    # -- Panel 2: MAE-Verlauf im Gruppenfeld --------------------------------
    teams_yaml = yaml.safe_load((REPO_ROOT / "teams.yml").read_text())["teams"]
    same_group = [t["id"] for t in teams_yaml
                  if t.get("group") == group
                  and not t["id"].endswith("_entsoe")
                  and not t.get("pseudo") and not t.get("retired")]
    for tid in same_group:
        rows = live[live["team_id"] == tid].sort_values("target_date")
        if rows.empty:
            continue
        x = pd.to_datetime(rows["target_date"])
        y = rows["mae"].rolling(7, min_periods=3).mean() / 1000.0
        if tid == team_id:
            ax2.plot(x, y, color=COL_MAGENTA, lw=2.2, zorder=5,
                     label=f"{display_name}")
        else:
            ax2.plot(x, y, color="#C9C9C9", lw=1.0, zorder=2)
    if baseline is not None:
        ax2.axhline(baseline / 1000.0, color=COL_INK, lw=1.0,
                    ls=(0, (1.5, 2.0)), zorder=3,
                    label="ENTSO-E day-ahead baseline (mean)")
    ax2.set_title(f"MAE over the challenge — {group} group field "
                  "(7-day rolling mean)", fontsize=8.5, color=COL_INK,
                  loc="left", pad=6)
    ax2.set_ylabel("MAE [GW]", fontsize=7, color="#555555")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    ax2.legend(fontsize=6.5, frameon=False, loc="best")

    fig.savefig(out_svg, format="svg", bbox_inches="tight",
                facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Typst
# ---------------------------------------------------------------------------

TYPST_TEMPLATE = r"""
// Auto-generated by scripts/generate_certificates.py — do not edit by hand.
#set page(paper: "a4", flipped: true,
  margin: (top: 0pt, bottom: 0pt, left: 0pt, right: 0pt))
#set text(font: ({FONT_LIST}), fill: rgb("{COL_INK}"), size: 11pt)

// ---- Kopf-Farbbalken (TH-Köln-Anmutung) ----
#place(top, dx: 0pt, dy: 0pt,
  grid(columns: (30%, 40%, 30%), rows: 8pt,
    rect(fill: rgb("{COL_DARKRED}"), width: 100%, height: 100%),
    rect(fill: rgb("{COL_ORANGE}"), width: 100%, height: 100%),
    rect(fill: rgb("{COL_MAGENTA}"), width: 100%, height: 100%)))

#pad(x: 46pt, top: 40pt, bottom: 0pt)[

#text(size: 33pt, weight: 800, tracking: 0.4pt)[Certificate]
#v(2pt)
#text(size: 10.5pt, fill: rgb("#666666"))[Live Load-Forecasting Challenge 2026 · TH Köln]

#v(14pt)

#grid(columns: (54%, 46%), column-gutter: 26pt,
  // ------------------- linke Spalte: Text -------------------
  [
    #set par(leading: 0.62em)
    This is to certify that

    #v(5pt)
    {MEMBER_BLOCK}
    #v(5pt)

    as #text(weight: 700)[{TEAM_NAME}]

    participated in the *Live Load-Forecasting Challenge 2026*
    ({PERIOD}){ACHIEVED_CLAUSE}

    {PLACE_BLOCK}

    #v(4pt)
    Every day before 23:59 UTC the team submitted a 24-hour
    day-ahead forecast of the German electricity grid load
    (bidding zone DE-LU), which was scored against the actual
    load published by the ENTSO-E Transparency Platform.
    Over *{N_DAYS} scored days* the team achieved a mean absolute
    error of #text(weight: 700)[{MAE} MW]{BASELINE_CLAUSE}.

    #v(6pt)
    Congratulations on this excellent performance!

    #v(10pt)
    #image("{SIGNATURE}", width: 150pt)
    #v(2pt)
    Gummersbach, {DATE}
  ],
  // ------------------- rechte Spalte: Grafik -------------------
  [
    #v(6pt)
    #image("{FIGURE}", width: 100%)
    #v(2pt)
    #text(size: 6.5pt, fill: rgb("#888888"))[
      Data: ENTSO-E Transparency Platform — Actual Total Load DE-LU (6.1.A);
      challenge leaderboard, TH Köln. Ranking: mean absolute error,
      {GROUP} group.
    ]
  ])
]

// ---- Fußzeile ----
#place(bottom, dx: 0pt, dy: 0pt,
  pad(x: 46pt, bottom: 22pt,
    [#line(length: 100%, stroke: 0.6pt + rgb("#BBBBBB"))
     #v(6pt)
     #grid(columns: (60%, 20%, 20%), align: (left, center, right),
       text(size: 7pt, fill: rgb("#555555"))[
         Prof. Dr. Thomas Bartz-Beielstein \
         Director — SpotSeven Lab, Institute for Data Science,
         Engineering, and Analytics \
         THK-AI Research Cluster, Technische Hochschule Köln
       ],
       image("{SPT_LOGO}", width: 64pt),
       [
         #text(size: 11pt, weight: 700, fill: rgb("#E2001A"))[Technology] \
         #text(size: 11pt, weight: 700, fill: rgb("{COL_MAGENTA}"))[Arts Sciences] \
         #text(size: 12pt, weight: 800)[TH Köln]
       ])]))
"""


def member_block(members: list[str]) -> str:
    if not members:
        return "#text(weight: 700, size: 13pt)[the members of the team]"
    lines = " \\\n    ".join(
        f"#text(weight: 700, size: 13pt)[{m}]" for m in members)
    return lines


def render_typ(*, team_name: str, group: str, rank: int, total: int,
               members: list[str], mae: float, n_days: int,
               baseline: float | None, period: str, date_str: str,
               figure: Path, outdir: Path) -> str:
    if rank in ORDINAL:
        achieved = " and achieved"
        place_block = (
            f'#v(4pt)\n'
            f'#text(size: 21pt, weight: 800)[{ORDINAL[rank]} place] '
            f'#text(size: 13pt, weight: 600)[in the {group} group]\n'
            f'#box(width: 120pt, height: 3.5pt, fill: rgb("{MEDAL[rank]}"), '
            f'radius: 2pt)')
    else:
        achieved = f" in the {group} group"
        place_block = ""
    if baseline is not None and mae < baseline:
        baseline_clause = (
            ", outperforming the official ENTSO-E day-ahead "
            f"forecast ({baseline:,.0f} MW) over the same period")
    else:
        baseline_clause = ""
    return (TYPST_TEMPLATE
            .replace("{FONT_LIST}", '"Helvetica Neue", "Libertinus Serif"')
            .replace("{COL_INK}", COL_INK)
            .replace("{COL_DARKRED}", COL_DARKRED)
            .replace("{COL_ORANGE}", COL_ORANGE)
            .replace("{COL_MAGENTA}", COL_MAGENTA)
            .replace("{MEMBER_BLOCK}", member_block(members))
            .replace("{TEAM_NAME}", team_name)
            .replace("{PERIOD}", period)
            .replace("{ACHIEVED_CLAUSE}", achieved)
            .replace("{PLACE_BLOCK}", place_block)
            .replace("{N_DAYS}", str(n_days))
            .replace("{MAE}", f"{mae:,.0f}")
            .replace("{BASELINE_CLAUSE}", baseline_clause)
            .replace("{SIGNATURE}", str(ASSETS / "signature.png"))
            .replace("{FIGURE}", figure.name)
            .replace("{SPT_LOGO}", str(ASSETS / "spotseven_logo.jpg"))
            .replace("{GROUP}", group)
            .replace("{DATE}", date_str))


def compile_typ(typ_path: Path) -> None:
    typst = shutil.which("typst")
    cmd = ([typst] if typst else ["quarto", "typst"]) + [
        "compile", "--root", "/",
        "--font-path", "/System/Library/Fonts",
        "--font-path", "/Library/Fonts",
        str(typ_path)]
    subprocess.run(cmd, check=True, capture_output=True, text=True,
                   cwd=typ_path.parent)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="July 21st 2026",
                    help="Ausstellungsdatum auf der Urkunde")
    ap.add_argument("--outdir", default=str(CERT_DIR / "out"))
    ap.add_argument("--only-team", default=None,
                    help="nur dieses team_id erzeugen (Test)")
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    standings, live, meta = load_standings()
    baseline = entsoe_baseline_mae(live)
    names = (yaml.safe_load(NAMES_YML.read_text())
             if NAMES_YML.exists() else {})

    d0 = pd.to_datetime(live["target_date"].min()).strftime("%-d %B")
    d1 = pd.to_datetime(live["target_date"].max()).strftime("%-d %B %Y")
    period = f"{d0} – {d1}"

    print(f"Live-Phase: {period}; ENTSO-E-Baseline "
          f"{baseline:,.0f} MW" if baseline else f"Live-Phase: {period}")
    generated = []
    for _, row in standings.iterrows():
        tid = row["team_id"]
        if args.only_team and tid != args.only_team:
            continue
        grp, rank = row["group"], int(row["rank"])
        total = int((standings["group"] == grp).sum())
        members = (names.get(tid) or {}).get("members") or []
        if not members:
            print(f"  WARNUNG: keine Mitgliedernamen für {tid} "
                  "(names.yml) — Urkunde nur auf Teamnamen ausgestellt")
        fig_path = outdir / f"{grp}_{rank}_{tid}.svg"
        make_figure(tid, row["display_name"], grp, live, baseline, fig_path)
        typ = render_typ(
            team_name=row["display_name"], group=grp, rank=rank, total=total,
            members=members, mae=float(row["mean_mae"]),
            n_days=int(row["n_days"]), baseline=baseline, period=period,
            date_str=args.date, figure=fig_path, outdir=outdir)
        typ_path = outdir / f"{grp}_{rank}_{tid}.typ"
        typ_path.write_text(typ)
        compile_typ(typ_path)
        pdf = typ_path.with_suffix(".pdf")
        generated.append(pdf)
        marker = f"  Platz {rank}" if rank <= 3 else "  Teilnahme"
        print(f"{marker}  {grp}: {row['display_name']:24s} "
              f"MAE {row['mean_mae']:8.2f} MW (n={int(row['n_days'])}) "
              f"-> {pdf.name}")

    print(f"\n{len(generated)} Urkunde(n) in {outdir}")


if __name__ == "__main__":
    main()
