"""
score_day.py

Tagesschritt der Bewertungs-Pipeline. Lädt den ENTSO-E-final-Load für
den Zieltag, liest alle Submissions submissions/*/<D>.csv ein, berechnet
MAE/RMSE/MAPE pro Team und hängt die Zeilen an data/scores.parquet an.

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
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent
SUBMISSIONS_DIR = REPO_ROOT / "submissions"
SCORES_PATH = REPO_ROOT / "data" / "scores.parquet"
COUNTRY = "DE"


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

    start = datetime.fromisoformat(f"{target_date}T00:00:00").replace(tzinfo=timezone.utc)
    end = start + timedelta(hours=23)

    with tempfile.TemporaryDirectory() as tmp:
        data_home = Path(tmp) / "spotforecast2_data"
        (data_home / "raw").mkdir(parents=True, exist_ok=True)
        os.environ["SPOTFORECAST2_DATA"] = str(data_home)

        from spotforecast2_safe.data.fetch_data import fetch_data, get_data_home
        from spotforecast2_safe.downloader.entsoe import download_new_data

        download_new_data(
            api_key=api_key,
            country_code=COUNTRY,
            start=start.strftime("%Y%m%d%H%M"),
            end=end.strftime("%Y%m%d%H%M"),
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

    target_hours = pd.date_range(start, periods=24, freq="h", tz="UTC")
    y = y.reindex(target_hours)

    if y.isna().any():
        # CR-3: lieber abbrechen als raten — Scoring wird auf nächsten
        # Tag verschoben (Action retried morgen).
        raise RuntimeError(
            f"ENTSO-E final-load enthält NaN für {target_date}: "
            f"{int(y.isna().sum())} fehlende Stunden"
        )
    return y


def score_submission(forecast: pd.Series, actual: pd.Series) -> dict:
    err = forecast.values - actual.values
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    nonzero = actual.values != 0
    mape = float(np.mean(np.abs(err[nonzero] / actual.values[nonzero])) * 100) \
        if nonzero.any() else float("nan")
    return {"mae": round(mae, 4), "rmse": round(rmse, 4), "mape": round(mape, 4)}


def collect_submissions(target_date: str) -> list[tuple[str, Path]]:
    pattern = f"*/{target_date}.csv"
    files = sorted(SUBMISSIONS_DIR.glob(pattern))
    return [(p.parent.name, p) for p in files]


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

    submissions = collect_submissions(target_date)
    if not submissions:
        print(f"[score_day] Keine Submissions für {target_date} — fertig.")
        return 0

    rows: list[dict] = []
    for team_id, path in submissions:
        try:
            sub = pd.read_csv(path)
            forecast = pd.Series(sub["forecast_mw"].values,
                                  index=pd.to_datetime(sub["timestamp_utc"]))
            forecast.index = forecast.index.tz_convert("UTC")
            actual_aligned = actual.reindex(forecast.index)
            if actual_aligned.isna().any():
                print(f"[score_day] {team_id}: timestamp-Misalignment, übersprungen")
                continue
            metrics = score_submission(forecast, actual_aligned)
        except Exception as exc:
            print(f"[score_day] {team_id}: Fehler ({exc}); übersprungen")
            continue
        rows.append({
            "team_id": team_id,
            "target_date": target_date,
            "scored_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **metrics,
        })
        print(f"[score_day] {team_id}: MAE={metrics['mae']:.2f} MW "
              f"RMSE={metrics['rmse']:.2f} MAPE={metrics['mape']:.2f}%")

    if rows:
        append_scores(rows)
        print(f"[score_day] {len(rows)} Zeilen in data/scores.parquet geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
