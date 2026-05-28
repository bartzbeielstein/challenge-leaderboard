"""
score_day.py

Tagesschritt der Bewertungs-Pipeline. Lädt den ENTSO-E-final-Load für
den Zieltag, ermittelt pro registriertem Team die zu bewertende
Prognose (frische Submission für den Zieltag oder — falls keine vorliegt
— die letzte vorhandene Submission als LOCF) und hängt MAE/RMSE/MAPE
an data/scores.parquet an.

Aufruf:
    python scripts/score_day.py --date 2026-05-15

Voraussetzung:
    ENTSOE_API_KEY in der Umgebung

CR-2 (Determinismus): PYTHONHASHSEED, deterministische Iteration über
sorted(...) der Submissions, pinned spotforecast2-safe via uv.lock.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SUBMISSIONS_DIR = REPO_ROOT / "submissions"
SCORES_PATH = REPO_ROOT / "data" / "scores.parquet"
TEAMS_PATH = REPO_ROOT / "teams.yml"
COUNTRY = "DE"
DATE_CSV_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.csv$")


def scoring_window(target_date: str) -> tuple[datetime, datetime, pd.DatetimeIndex]:
    """UTC-Fenster für den Zieltag.

    Liefert (fetch_start, fetch_end, target_hours):
      - target_hours: die 24 zu bewertenden Stunden D 00:00–23:00 UTC.
      - fetch_start/fetch_end: ENTSO-E-Download-Fenster mit Puffer (6 h
        davor, 5 h danach), damit (i) die letzten 15-Min-Werte für die
        23:00-Stunde komplett sind und (ii) ein Quartal-Ausreißer am
        Tagesrand nicht den ganzen Tag NaN macht.

    Alles UTC — keine lokale Zeitzone (CR: UTC-only).
    """
    start = datetime.fromisoformat(f"{target_date}T00:00:00").replace(tzinfo=timezone.utc)
    fetch_start = start - timedelta(hours=6)
    fetch_end = start + timedelta(hours=29)  # 24h Zieltag + 5h Puffer
    target_hours = pd.date_range(start, periods=24, freq="h", tz="UTC")
    return fetch_start, fetch_end, target_hours


def fetch_ground_truth(target_date: str) -> pd.Series:
    """Pull ENTSO-E final-load für den Zieltag (00:00–23:00 UTC).

    Verwendet das Muster aus Kapitel 02: `download_new_data` schreibt
    `interim/energy_load.csv` unter `$SPOTFORECAST2_DATA`, anschließend
    liest `fetch_data` die Datei. Wir nutzen ein temporäres
    Cache-Verzeichnis, damit aufeinanderfolgende Score-Läufe sich nicht
    ins Gehege kommen (Kompatibilität mit GitHub-Actions-Runner).
    """
    api_key = os.environ.get("ENTSOE_API_KEY")
    if not api_key:
        raise RuntimeError("ENTSOE_API_KEY ist nicht gesetzt")

    fetch_start, fetch_end, target_hours = scoring_window(target_date)

    with tempfile.TemporaryDirectory() as tmp:
        data_home = Path(tmp) / "spotforecast2_data"
        (data_home / "raw").mkdir(parents=True, exist_ok=True)
        os.environ["SPOTFORECAST2_DATA"] = str(data_home)

        from spotforecast2_safe.data.fetch_data import fetch_data, get_data_home
        from spotforecast2_safe.downloader.entsoe import download_new_data

        download_new_data(
            api_key=api_key,
            country_code=COUNTRY,
            start=fetch_start.strftime("%Y%m%d%H%M"),
            end=fetch_end.strftime("%Y%m%d%H%M"),
            force=True,
        )

        interim = get_data_home() / "interim" / "energy_load.csv"
        if not interim.exists():
            raise RuntimeError(
                f"ENTSO-E-Download lieferte keine CSV unter {interim}. "
                f"Token gültig? Datum {target_date} außerhalb des "
                f"final-load-Veröffentlichungsfensters?"
            )

        df = fetch_data(filename=str(interim))

    df.index = pd.to_datetime(df.index, utc=True)
    load_col = next((c for c in df.columns if "Actual" in c and "Load" in c), None)
    if load_col is None:
        raise RuntimeError(
            f"Keine 'Actual Load'-Spalte gefunden. Vorhandene Spalten: "
            f"{list(df.columns)}"
        )
    y = df[load_col].astype(float).rename("load")
    if y.index.inferred_freq != "h":
        y = y.resample("h").mean()

    y = y.reindex(target_hours)

    if y.isna().any():
        # CR-3: lieber abbrechen als raten — Scoring wird auf nächsten
        # Tag verschoben (Action retried morgen).
        raise RuntimeError(
            f"ENTSO-E final-load enthält NaN für {target_date}: "
            f"{int(y.isna().sum())} fehlende Stunden"
        )
    return y


def score_submission(forecast_values: np.ndarray, actual: pd.Series) -> dict:
    err = forecast_values - actual.values
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    nonzero = actual.values != 0
    mape = float(np.mean(np.abs(err[nonzero] / actual.values[nonzero])) * 100) \
        if nonzero.any() else float("nan")
    return {"mae": round(mae, 4), "rmse": round(rmse, 4), "mape": round(mape, 4)}


def load_team_ids() -> list[str]:
    data = yaml.safe_load(TEAMS_PATH.read_text()) or {}
    return sorted(t["id"] for t in (data.get("teams") or []))


def collect_forecasts(
    target_date: str, team_ids: list[str]
) -> list[tuple[str, Path, bool]]:
    """Pro Team: (team_id, csv-Pfad, carried_forward).

    Frische Submission für den Zieltag bevorzugt; sonst LOCF auf die
    letzte Submission vor target_date. Teams ohne irgendeine
    Submission werden übersprungen.
    """
    out: list[tuple[str, Path, bool]] = []
    for team_id in team_ids:
        team_dir = SUBMISSIONS_DIR / team_id
        if not team_dir.is_dir():
            continue
        exact = team_dir / f"{target_date}.csv"
        if exact.exists():
            out.append((team_id, exact, False))
            continue
        prior = sorted(
            p for p in team_dir.glob("*.csv")
            if DATE_CSV_RE.match(p.name) and p.stem < target_date
        )
        if not prior:
            continue
        out.append((team_id, prior[-1], True))
    return out


def append_scores(rows: list[dict]) -> None:
    new_df = pd.DataFrame(rows)
    if SCORES_PATH.exists():
        existing = pd.read_parquet(SCORES_PATH)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        SCORES_PATH.parent.mkdir(parents=True, exist_ok=True)
        combined = new_df
    # Idempotenz: doppelte (team_id, target_date)-Paare entfernen, letztes wins
    combined = combined.drop_duplicates(
        subset=["team_id", "target_date"], keep="last"
    )
    combined.to_parquet(SCORES_PATH, index=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Zieltag YYYY-MM-DD (UTC)")
    args = parser.parse_args()
    target_date = args.date

    print(f"[score_day] Lade Ground-Truth für {target_date} …")
    actual = fetch_ground_truth(target_date)

    team_ids = load_team_ids()
    forecasts = collect_forecasts(target_date, team_ids)
    if not forecasts:
        print(f"[score_day] Keine bewertbaren Prognosen für {target_date} — fertig.")
        return 0

    rows: list[dict] = []
    for team_id, path, carried in forecasts:
        try:
            sub = pd.read_csv(path)
            forecast_values = sub["forecast_mw"].to_numpy(dtype=float)
            if len(forecast_values) != 24:
                print(f"[score_day] {team_id}: 24 Zeilen erwartet, "
                      f"{len(forecast_values)} gefunden ({path.name}); übersprungen")
                continue
            metrics = score_submission(forecast_values, actual)
        except Exception as exc:
            print(f"[score_day] {team_id}: Fehler ({exc}); übersprungen")
            continue
        rows.append({
            "team_id": team_id,
            "target_date": target_date,
            "scored_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_date": path.stem,
            "carried_forward": carried,
            **metrics,
        })
        tag = f" [LOCF aus {path.stem}]" if carried else ""
        print(f"[score_day] {team_id}: MAE={metrics['mae']:.2f} MW "
              f"RMSE={metrics['rmse']:.2f} MAPE={metrics['mape']:.2f}%{tag}")

    if rows:
        append_scores(rows)
        print(f"[score_day] {len(rows)} Zeilen in data/scores.parquet geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
