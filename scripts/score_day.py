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
import time
from datetime import date, datetime, timedelta, timezone
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


class GroundTruthNotReady(RuntimeError):
    """ENTSO-E-Actuals für den Zieltag sind (noch) nicht vollständig.

    Signalisiert dem Aufrufer „aufschieben und morgen erneut versuchen“ —
    abgegrenzt von harten Konfigurationsfehlern (fehlender API-Key), die
    sofort und laut scheitern sollen. Erbt von RuntimeError, damit die
    bestehende `except RuntimeError`-Defer-Logik in `main()` greift.
    """


def _download_load_frame(target_date: str) -> pd.DataFrame:
    """Ein ENTSO-E-Abrufversuch → stündlicher Frame auf die 24 Zielstunden.

    Verwendet das Muster aus Kapitel 02: `download_new_data` schreibt
    `interim/energy_load.csv` unter `$SPOTFORECAST2_DATA`, anschließend
    liest `fetch_data` die Datei. Wir nutzen ein temporäres
    Cache-Verzeichnis, damit aufeinanderfolgende Score-Läufe sich nicht
    ins Gehege kommen (Kompatibilität mit GitHub-Actions-Runner).

    `query_load_and_forecast` liefert *beide* Spalten — 'Actual Total Load'
    und die 'Day-ahead ... Forecast'. Der Frame ist auf die 24 Zielstunden
    reindiziert (auf Stunden resampled, falls 15-Min-Auflösung) und kann NaN
    enthalten, wenn der Tag (noch) unvollständig veröffentlicht ist.
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
            # Kein CSV → ENTSO-E lieferte (noch) keine Daten ("No matching
            # data found" kommt als HTTP 200 ohne Inhalt). Als „noch nicht
            # bereit“ behandeln, damit der Tag aufgeschoben statt hart
            # abgebrochen wird.
            raise GroundTruthNotReady(
                f"ENTSO-E-Download lieferte keine CSV unter {interim} "
                f"(Datum {target_date} evtl. außerhalb des "
                f"final-load-Veröffentlichungsfensters)."
            )

        df = fetch_data(filename=str(interim))

    df.index = pd.to_datetime(df.index, utc=True)
    if df.index.inferred_freq != "h":
        df = df.resample("h").mean(numeric_only=True)
    return df.reindex(target_hours)


def _download_actual_load(target_date: str) -> pd.Series:
    """Ein ENTSO-E-Abrufversuch → stündliche Actual-Load-Serie.

    Dünne Schicht über `_download_load_frame`, die die Actual-Load-Spalte
    auswählt. Die auf die 24 Zielstunden reindizierte Serie kann NaN
    enthalten; die Vollständigkeitsprüfung übernimmt `fetch_ground_truth`.
    """
    df = _download_load_frame(target_date)
    load_col = next((c for c in df.columns if "Actual" in c and "Load" in c), None)
    if load_col is None:
        raise RuntimeError(
            f"Keine 'Actual Load'-Spalte gefunden. Vorhandene Spalten: "
            f"{list(df.columns)}"
        )
    return df[load_col].astype(float).rename("load")


def fetch_ground_truth(
    target_date: str,
    *,
    attempts: int = 4,
    base_delay: float = 5.0,
    sleep=time.sleep,
) -> pd.Series:
    """Robuster ENTSO-E-Abruf für den Zieltag (00:00–23:00 UTC).

    Härtung gegenüber dem ENTSO-E-Transparency-Verhalten (siehe
    Recherche): transiente API-/Netzfehler und „HTTP 200 + No matching
    data" treten real auf, ebenso einzelne fehlende Stunden (DST,
    verspätete TSO-Veröffentlichung). Strategie:

      * transienter Fehler  → Retry mit exponentiellem Backoff
      * unvollständiger Tag  → kurzer Retry, dann `GroundTruthNotReady`
        (CR-3: lieber aufschieben als raten — der nächste Lauf holt den
        Tag via Catch-up nach)
      * fehlender API-Key    → sofortiger harter Abbruch (laut scheitern)

    `attempts`/`base_delay`/`sleep` sind injizierbar (Tests setzen
    `sleep` auf No-op).
    """
    if not os.environ.get("ENTSOE_API_KEY"):
        raise RuntimeError("ENTSOE_API_KEY ist nicht gesetzt")

    last_reason = ""
    for attempt in range(1, attempts + 1):
        try:
            y = _download_actual_load(target_date)
        except GroundTruthNotReady as exc:
            last_reason = str(exc)
        except Exception as exc:  # transient: Netz/API/Rate-Limit
            last_reason = f"transienter Abruf-Fehler: {exc}"
        else:
            missing = int(y.isna().sum())
            if missing == 0:
                return y
            last_reason = (
                f"ENTSO-E final-load unvollständig: {missing} fehlende Stunden"
            )

        if attempt < attempts:
            delay = base_delay * 2 ** (attempt - 1)
            print(f"[score_day] {target_date}: Versuch {attempt}/{attempts} "
                  f"nicht erfolgreich ({last_reason}); neuer Versuch in "
                  f"{delay:.0f}s")
            sleep(delay)

    raise GroundTruthNotReady(
        f"ENTSO-E-Actuals für {target_date} nach {attempts} Versuchen nicht "
        f"verfügbar: {last_reason}"
    )


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


def scored_dates() -> set[str]:
    """Set of target_date values that already have a row in scores.parquet."""
    if not SCORES_PATH.exists():
        return set()
    df = pd.read_parquet(SCORES_PATH)
    if "target_date" not in df.columns:
        return set()
    return set(df["target_date"].astype(str))


def days_to_score(target_date: str, catch_up: int) -> list[str]:
    """Days this run should attempt, oldest first.

    Always the primary `target_date`, plus any day in the trailing
    `catch_up`-day window that has no row in scores.parquet yet. This is
    the self-healing property: if a daily cron is skipped (GitHub delays
    or drops scheduled runs under load), the next run still picks up the
    missed day instead of dropping it. Already-scored older days are left
    untouched — catch-up never silently re-scores a day that was graded.
    """
    target = date.fromisoformat(target_date)
    window = {target - timedelta(days=i) for i in range(max(catch_up, 1))}
    already = scored_dates()
    due = {d.isoformat() for d in window if d.isoformat() not in already}
    due.add(target_date)  # primary always (re)scored; append_scores is idempotent
    return sorted(due)


def score_one_day(target_date: str, team_ids: list[str]) -> list[dict]:
    """Fetch actuals for one day and score every team's forecast.

    Raises (via `fetch_ground_truth`) if the day's ENTSO-E final actuals
    are not yet available/complete; the caller defers that day.
    """
    actual = fetch_ground_truth(target_date)
    rows: list[dict] = []
    for team_id, path, carried in collect_forecasts(target_date, team_ids):
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
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Zieltag YYYY-MM-DD (UTC)")
    parser.add_argument(
        "--catch-up", type=int, default=1, metavar="N",
        help="Zusätzlich alle noch ungescorten Tage der letzten N Tage "
             "nachscoren (Standard 1 = nur --date). Heilt ausgefallene "
             "Cron-Läufe selbst.",
    )
    args = parser.parse_args()

    team_ids = load_team_ids()
    due = days_to_score(args.date, args.catch_up)
    if len(due) > 1:
        print(f"[score_day] catch-up={args.catch_up}: zu scoren {', '.join(due)}")

    all_rows: list[dict] = []
    deferred: list[str] = []
    for d in due:
        print(f"[score_day] Lade Ground-Truth für {d} …")
        try:
            all_rows.extend(score_one_day(d, team_ids))
        except RuntimeError as exc:
            # Actuals noch nicht veröffentlicht / unvollständig — der Tag
            # bleibt ungescort und wird vom nächsten Lauf erneut versucht.
            print(f"[score_day] {d}: aufgeschoben — {exc}")
            deferred.append(d)

    if all_rows:
        append_scores(all_rows)
        print(f"[score_day] {len(all_rows)} Zeilen in data/scores.parquet geschrieben.")
    else:
        print("[score_day] Keine bewertbaren Prognosen — nichts geschrieben.")
    if deferred:
        print(f"[score_day] Aufgeschoben (Actuals noch nicht verfügbar): "
              f"{', '.join(deferred)}")

    # Beobachtbarkeit: Wenn der *primäre* Zieltag nicht gescort werden konnte,
    # scheitert der Lauf sichtbar (roter Run als Alarm) — selbst wenn ältere
    # Catch-up-Tage Fortschritt gemacht haben. Diese werden vom nächsten Lauf
    # ohnehin nachgeholt. Ein aufgeschobener *Nebentag* bei gescortem
    # Primärtag ist hingegen kein Fehler (nur Warnung oben).
    if args.date in deferred:
        print(f"::error::Primärer Zieltag {args.date} konnte nicht gescort "
              f"werden (ENTSO-E-Actuals nicht verfügbar). Nächster Lauf holt "
              f"ihn via Catch-up nach.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
