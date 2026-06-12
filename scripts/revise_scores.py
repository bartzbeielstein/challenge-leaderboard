"""revise_scores.py

ENTSO-E-Revisionen erkennen und bereits benotete Tage neu bewerten.

ENTSO-E korrigiert veröffentlichte Actual-Total-Load-Werte gelegentlich
nachträglich (fehlende TSO-Zonen, implausible Stunden, verspätete
Meldungen). Dieses Skript vergleicht für ein rückwärtiges Tagesfenster
den frisch abgerufenen ENTSO-E-Stand mit dem committeten Stand in
``data/actual_load.parquet``. Weicht ein bereits benoteter Tag um mehr
als ``--tolerance`` MW ab (oder war er unvollständig gespeichert), wird
er neu bewertet:

  * ``data/scores.parquet``      — Tages-Scores aller Teams neu berechnet
                                   (idempotent, letzte Zeile gewinnt;
                                   ``scored_at_utc`` dokumentiert die
                                   Neubewertung)
  * ``data/actual_load.parquet`` — Ist-Load + ENTSO-E-Forecast des Tages
                                   auf den korrigierten Stand gebracht

Nur vollständige frische Tage (24 Nicht-NaN-Stunden) lösen eine
Neubewertung aus — ein ENTSO-E-Schluckauf beim Abruf kann einen bereits
benoteten Tag also nie verschlechtern. Nicht benotete Tage sind Sache
des Catch-up in ``score_day.py``, nicht dieses Skripts.

Aufruf (Workflow, nach dem Tageslauf — best effort):
    uv run python scripts/revise_scores.py --end 2026-06-11 --window 7

Voraussetzung:
    ENTSOE_API_KEY in der Umgebung
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# score_day.py / fetch_actuals.py liegen im selben Verzeichnis; Download-,
# Scoring- und Merge-Logik werden wiederverwendet statt dupliziert.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_actuals as fa  # noqa: E402
import score_day as sd  # noqa: E402


# Ab dieser maximalen Stunden-Abweichung [MW] gilt ein Tag als revidiert.
# Identische Publikationen reproduzieren sich bit-genau; die Schwelle
# fängt lediglich Resampling-/Rundungsrauschen ab, ohne echte Korrekturen
# (typisch >> 100 MW) zu verschlucken.
DEFAULT_TOLERANCE_MW = 1.0
DEFAULT_WINDOW_DAYS = 7


def revision_window(end: str, window: int) -> list[str]:
    """Bereits benotete Tage im Fenster ``[end - window + 1, end]``, aufsteigend.

    Nur Tage mit Zeilen in ``scores.parquet`` — Revision heißt Neubewertung,
    nie Erstbewertung (die übernimmt der Catch-up in ``score_day.py``).
    """
    end_d = date.fromisoformat(end)
    days = {(end_d - timedelta(days=i)).isoformat()
            for i in range(max(window, 1))}
    return sorted(days & sd.scored_dates())


def stored_load(day: str) -> pd.Series:
    """Committeter Ist-Load des Tages aus ``actual_load.parquet``.

    Serie indiziert mit ``timestamp_utc``-Strings; leer, wenn die Datei
    oder der Tag fehlt (gilt dann als revisionsbedürftig).
    """
    if not fa.ACTUALS_PATH.exists():
        return pd.Series(dtype=float)
    df = pd.read_parquet(fa.ACTUALS_PATH)
    if df.empty or "timestamp_utc" not in df.columns:
        return pd.Series(dtype=float)
    day_rows = df[df["timestamp_utc"].str.startswith(day)]
    return day_rows.set_index("timestamp_utc")["load_mw"].astype(float)


def fresh_day_frame(day: str) -> tuple[pd.Series, pd.DataFrame] | None:
    """Ein ENTSO-E-Abruf → (Actual-Serie, Tidy-Frame für merge_actuals).

    None, wenn der Abruf scheitert oder der Tag (noch/wieder) unvollständig
    publiziert ist — dann wird der Tag übersprungen, nie herabgestuft.
    Ein Abruf liefert beide Spalten (Actual + Day-ahead-Forecast).
    """
    try:
        frame = sd._download_load_frame(day)
    except Exception as exc:
        print(f"[revise] {day}: Abruf fehlgeschlagen — übersprungen ({exc})")
        return None
    load_col = next(
        (c for c in frame.columns if "Actual" in c and "Load" in c), None)
    if load_col is None:
        print(f"[revise] {day}: keine Actual-Load-Spalte — übersprungen")
        return None
    actual = frame[load_col].astype(float).rename("load")
    if len(actual) != 24 or actual.isna().any():
        print(f"[revise] {day}: frischer Stand unvollständig "
              f"({int(actual.isna().sum())} fehlende Stunden) — übersprungen")
        return None
    fcol = next((c for c in frame.columns if "forecast" in c.lower()), None)
    forecast = (frame[fcol].astype(float) if fcol is not None
                else pd.Series(index=actual.index, dtype=float))
    stamps = pd.DatetimeIndex(actual.index).strftime(fa.TS_FORMAT)
    tidy = pd.DataFrame({
        "timestamp_utc": list(stamps),
        "load_mw": actual.to_numpy(dtype=float),
        "entsoe_forecast_mw": forecast.to_numpy(dtype=float),
    })
    return actual, tidy


def max_deviation(fresh: pd.Series, stored: pd.Series) -> float:
    """Maximale absolute Stunden-Abweichung frisch vs. committet [MW].

    ``inf``, wenn der committete Stand Stunden des frischen Tages nicht
    abdeckt (fehlender/unvollständiger Tag zählt als Revision).
    """
    stamps = pd.DatetimeIndex(fresh.index).strftime(fa.TS_FORMAT)
    aligned = stored.reindex(stamps)
    if aligned.isna().any():
        return float("inf")
    diff = fresh.to_numpy(dtype=float) - aligned.to_numpy(dtype=float)
    return float(np.abs(diff).max())


def revise(end: str, window: int, tolerance: float) -> list[str]:
    """Fenster prüfen, abweichende Tage neu bewerten. Liefert revidierte Tage."""
    team_ids = sd.load_team_ids()
    days = revision_window(end, window)
    if not days:
        print("[revise] Keine benoteten Tage im Fenster — nichts zu prüfen.")
        return []
    print(f"[revise] Prüfe {len(days)} benotete Tag(e): {', '.join(days)}")

    revised: list[str] = []
    score_rows: list[dict] = []
    actual_frames: list[pd.DataFrame] = []
    for day in days:
        result = fresh_day_frame(day)
        if result is None:
            continue
        fresh, tidy = result
        dev = max_deviation(fresh, stored_load(day))
        if dev <= tolerance:
            print(f"[revise] {day}: unverändert "
                  f"(max. Abweichung {dev:.2f} MW)")
            continue
        dev_label = "committeter Stand unvollständig" if dev == float("inf") \
            else f"max. Abweichung {dev:.1f} MW"
        print(f"[revise] {day}: ENTSO-E-Korrektur erkannt ({dev_label}) "
              f"— Tag wird neu bewertet")
        rows = sd.score_one_day(day, team_ids, actual=fresh)
        score_rows.extend(rows)
        actual_frames.append(tidy)
        revised.append(day)

    if revised:
        sd.append_scores(score_rows)
        fa.merge_actuals(actual_frames)
        print(f"[revise] {len(revised)} Tag(e) neu bewertet "
              f"({len(score_rows)} Score-Zeilen): {', '.join(revised)}")
    else:
        print("[revise] Keine ENTSO-E-Korrekturen im Fenster.")
    return revised


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--end", default=None,
        help="Letzter zu prüfender Tag YYYY-MM-DD (UTC). Default: gestern.")
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_DAYS, metavar="N",
        help=f"Fensterbreite in Tagen rückwärts ab --end "
             f"(Default {DEFAULT_WINDOW_DAYS}).")
    parser.add_argument(
        "--tolerance", type=float, default=DEFAULT_TOLERANCE_MW, metavar="MW",
        help=f"Maximal tolerierte Stunden-Abweichung in MW, bevor ein Tag "
             f"als revidiert gilt (Default {DEFAULT_TOLERANCE_MW}).")
    args = parser.parse_args()

    end = args.end or (
        datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    revise(end, args.window, args.tolerance)
    # Best effort: ein fehlgeschlagener Einzeltag ist kein Lauf-Fehler —
    # der nächste Tageslauf prüft das Fenster erneut.
    return 0


if __name__ == "__main__":
    sys.exit(main())
