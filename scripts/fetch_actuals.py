"""fetch_actuals.py

Lokaler Download des ENTSO-E-Actual-Total-Load (DE) für die
Visualisierung. Schreibt eine *committebare* Zeitreihe nach
``data/actual_load.parquet``, die der GitHub-Pages-Build dann ohne
API-Key lesen kann (der `ENTSOE_API_KEY` liegt nur auf der lokalen
Maschine, nicht auf dem GitHub-Runner).

Der Scorer (`score_day.py`) lädt den Actual-Load bereits, verwirft ihn
aber nach der Metrik-Berechnung. Dieses Skript nutzt **dieselbe,
getestete Download-Logik** (`score_day.fetch_ground_truth`) und
persistiert das Ergebnis stattdessen.

Aufruf (lokal, Key gesetzt):
    uv run python scripts/fetch_actuals.py            # alle relevanten Tage
    uv run python scripts/fetch_actuals.py --from 2026-05-26 --to 2026-06-01
    uv run python scripts/fetch_actuals.py --force    # bereits vollständige Tage neu laden

Anschließend `data/actual_load.parquet` commiten und per PR nach `main`
bringen (als Admin mergen — der PR berührt keine `submissions/**`-Datei,
also nickt `validate-pr.yml` ihn pass-through ab, `auto-merge.yml`
mergt ihn bewusst nicht automatisch).

Datei-Schema (eine Zeile pro UTC-Stunde):
    timestamp_utc : str   ISO8601 UTC, z. B. '2026-06-01T13:00:00Z'
    load_mw       : float Actual Total Load [MW]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# score_day.py liegt im selben Verzeichnis; dessen Download-Pfad wird
# wiederverwendet statt den ENTSO-E-Aufruf zu duplizieren.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import score_day as sd  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
ACTUALS_PATH = REPO_ROOT / "data" / "actual_load.parquet"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _today_utc() -> date:
    """Heutiges UTC-Datum (separat gekapselt, damit Tests es ersetzen können)."""
    return datetime.now(timezone.utc).date()


def discover_dates(*, today: date | None = None) -> list[str]:
    """Standard-Datumsmenge ohne CLI-Argumente.

    Vereinigung aus (i) allen Submission-Zieltagen unter
    ``submissions/*/<YYYY-MM-DD>.csv`` und (ii) bereits gescorten Tagen
    (``score_day.scored_dates()``), begrenzt auf ``<= heute (UTC)`` —
    Actuals für die Zukunft existieren noch nicht. Aufsteigend sortiert.
    """
    today = today or _today_utc()
    dates: set[str] = set()
    if sd.SUBMISSIONS_DIR.is_dir():
        for team_dir in sd.SUBMISSIONS_DIR.iterdir():
            if not team_dir.is_dir():
                continue
            for csv in team_dir.glob("*.csv"):
                if sd.DATE_CSV_RE.match(csv.name):
                    dates.add(csv.stem)
    dates |= sd.scored_dates()
    cutoff = today.isoformat()
    return sorted(d for d in dates if d <= cutoff)


def dates_in_range(date_from: str, date_to: str) -> list[str]:
    """Inklusiver Tagesbereich (UTC), ältester zuerst."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        return []
    n = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(n + 1)]


def already_complete(path: Path) -> set[str]:
    """Tage, die in ``path`` bereits vollständig (24 Nicht-NaN-Stunden) vorliegen.

    Lässt einen erneuten Lauf bereits geladene Tage überspringen
    (vermeidet unnötige ENTSO-E-Abrufe); mit ``--force`` umgehbar.
    """
    if not path.exists():
        return set()
    df = pd.read_parquet(path)
    if df.empty or "timestamp_utc" not in df.columns:
        return set()
    df = df.dropna(subset=["load_mw"])
    day = df["timestamp_utc"].str.slice(0, 10)
    counts = day.value_counts()
    return set(counts[counts >= 24].index)


def fetch_one_day(target_date: str) -> pd.DataFrame | None:
    """Ein Tag Actual-Load als Tidy-Frame, oder None falls noch nicht verfügbar.

    Nutzt ``score_day.fetch_ground_truth`` (Retry/Backoff +
    Vollständigkeitsprüfung). ``GroundTruthNotReady`` → überspringen
    (None). Ein fehlender API-Key (``RuntimeError``, *nicht*
    ``GroundTruthNotReady``) propagiert und lässt den Lauf laut scheitern.
    """
    try:
        series = sd.fetch_ground_truth(target_date)
    except sd.GroundTruthNotReady as exc:
        print(f"[fetch_actuals] {target_date}: noch nicht verfügbar — "
              f"übersprungen ({exc})")
        return None
    stamps = pd.DatetimeIndex(series.index).strftime(TS_FORMAT)
    return pd.DataFrame({
        "timestamp_utc": list(stamps),
        "load_mw": series.to_numpy(dtype=float),
    })


def merge_actuals(frames: list[pd.DataFrame]) -> int:
    """Idempotent nach ACTUALS_PATH mergen (analog zu score_day.append_scores).

    Dedup-Schlüssel ist ``timestamp_utc`` (jüngster Abruf gewinnt),
    Ausgabe stündlich aufsteigend sortiert (diff-freundlich). Liefert die
    Gesamtzahl der Zeilen in der Datei. Keine Datei-Änderung, wenn
    ``frames`` leer ist (vermeidet leere Commits).
    """
    if not frames:
        return 0
    new_df = pd.concat(frames, ignore_index=True)
    if ACTUALS_PATH.exists():
        existing = pd.read_parquet(ACTUALS_PATH)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        ACTUALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        combined = new_df
    combined = combined.drop_duplicates(subset=["timestamp_utc"], keep="last")
    combined = combined.sort_values("timestamp_utc").reset_index(drop=True)
    combined.to_parquet(ACTUALS_PATH, index=False)
    return len(combined)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="date_from", default=None,
                        help="Startdatum YYYY-MM-DD (UTC). Default: aus "
                             "Submissions/Scores ermittelt.")
    parser.add_argument("--to", dest="date_to", default=None,
                        help="Enddatum YYYY-MM-DD (UTC, inklusive). Default: "
                             "heute (UTC).")
    parser.add_argument("--force", action="store_true",
                        help="Auch bereits vollständig geladene Tage neu abrufen.")
    args = parser.parse_args()

    if args.date_from:
        date_to = args.date_to or _today_utc().isoformat()
        due = dates_in_range(args.date_from, date_to)
    elif args.date_to:
        # Nur --to angegeben: alle ermittelten Tage bis (inkl.) date_to.
        due = [d for d in discover_dates() if d <= args.date_to]
    else:
        due = discover_dates()

    if not due:
        print("[fetch_actuals] Keine abzurufenden Tage ermittelt — nichts zu tun.")
        return 0

    skip = set() if args.force else already_complete(ACTUALS_PATH)
    todo = [d for d in due if d not in skip]
    if skip:
        print(f"[fetch_actuals] {len(skip)} Tag(e) bereits vollständig — "
              f"übersprungen (--force zum Neuladen).")

    frames: list[pd.DataFrame] = []
    deferred: list[str] = []
    for d in todo:
        print(f"[fetch_actuals] Lade Actual-Load für {d} …")
        frame = fetch_one_day(d)
        if frame is None:
            deferred.append(d)
        else:
            frames.append(frame)

    total = merge_actuals(frames)
    try:
        rel = ACTUALS_PATH.relative_to(REPO_ROOT)
    except ValueError:
        rel = ACTUALS_PATH
    if frames:
        print(f"[fetch_actuals] {len(frames)} Tag(e) geschrieben "
              f"({len(frames) * 24} Stunden); {total} Zeilen in {rel}.")
    else:
        print("[fetch_actuals] Keine neuen Actuals — nichts geschrieben.")
    if deferred:
        print(f"[fetch_actuals] Aufgeschoben (Actuals noch nicht verfügbar): "
              f"{', '.join(deferred)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
