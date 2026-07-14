"""apply_joker.py

Joker-Regel anwenden: jedes Team darf während der gesamten Challenge
genau EINEN Joker einsetzen und damit EINEN bereits bewerteten Zieltag
durch eine aktualisierte Prognose korrigieren. Das schließt per LOCF
bewertete Tage ohne eigene Submission ein — die Datei
``submissions/<team>/<datum>.csv`` wird dann neu angelegt. Der Tag wird
anschließend gegen die committeten Ist-Werte
(``data/actual_load.parquet``) neu bewertet — kein ENTSO-E-Abruf,
keine Änderung an anderen Tagen oder Teams (chirurgische
Einzel-Neubewertung, analog zur Idempotenz von
``score_day.append_scores``: letzte Zeile gewinnt).

Ablauf (alle Checks VOR der ersten Mutation — CR-3: kein halber Joker):
  1. Team registriert, kein Pseudo-/Retired-Team (Exit 3)
  2. Joker noch verfügbar (Exit 4)
  3. Zieltag bereits bewertet (Exit 4) — auch ein LOCF-bewerteter Tag
     qualifiziert; nicht bewertete Tage nicht
  4. Ersatz-CSV besteht dieselben Schema-Checks wie eine reguläre
     Submission (Exit 1)
  5. Committete Ist-Werte des Tages vollständig (Exit 5)
Danach: CSV ersetzen, ``joker: "<datum>"`` in teams.yml setzen,
Tag neu bewerten, Vorher/Nachher ausgeben.

Aufruf:
    uv run python scripts/apply_joker.py --team team_fabinalii --date 2026-06-22
    # Ersatzdatei per Konvention unter joker/<team>/<datum>.csv,
    # abweichend via --file

Danach ``uv run python scripts/build_leaderboard.py`` ausführen und
Submission + teams.yml + data/ + public/ committen.

Exit-Codes:
  0 — Joker angewendet
  1 — Schema-Verstoß der Ersatz-CSV
  3 — Team unbekannt / Pseudo / Retired
  4 — Joker-Regel verletzt (bereits eingesetzt, Zieltag nicht bewertet)
  5 — committete Ist-Werte des Zieltages unvollständig
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# score_day.py liegt im selben Verzeichnis; die Scoring-Logik wird
# wiederverwendet statt dupliziert (Muster aus revise_scores.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import score_day as sd  # noqa: E402

from challenge_leaderboard.joker import (  # noqa: E402
    check_joker_available,
    mark_joker_used,
)
from challenge_leaderboard.teams import check_team_registry, load_teams  # noqa: E402
from challenge_leaderboard.validation import (  # noqa: E402
    SubmissionInvalid,
    validate_schema,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_stored_actual(target_date: str, actuals_path: Path) -> pd.Series:
    """24 committete Ist-Stunden des Zieltages, chronologisch sortiert.

    Bewertet wird ausschließlich gegen den committeten Stand in
    ``actual_load.parquet`` (Single Source of Truth der bereits
    benoteten Tage) — bewusst KEIN frischer ENTSO-E-Abruf, damit der
    Joker exakt gegen dieselben Ist-Werte benotet wird wie die ersetzte
    Submission. Raises RuntimeError, wenn der Tag fehlt oder
    unvollständig ist.
    """
    if not actuals_path.exists():
        raise RuntimeError(f"{actuals_path} existiert nicht")
    df = pd.read_parquet(actuals_path)
    day = df[df["timestamp_utc"].str.startswith(target_date)].sort_values(
        "timestamp_utc"
    )
    if len(day) != 24 or day["load_mw"].isna().any():
        raise RuntimeError(
            f"Committete Ist-Werte für {target_date} unvollständig "
            f"({len(day)} Zeilen, {int(day['load_mw'].isna().sum())} NaN) — "
            f"Joker-Neubewertung nicht möglich"
        )
    return pd.Series(
        day["load_mw"].to_numpy(dtype=float),
        index=pd.to_datetime(day["timestamp_utc"], utc=True),
        name="load",
    )


def existing_score_row(
    scores_path: Path, team_id: str, target_date: str
) -> dict:
    """Bestehende Score-Zeile (team, tag) — SubmissionInvalid(4) wenn unbewertet."""
    rows = pd.DataFrame()
    if scores_path.exists():
        df = pd.read_parquet(scores_path)
        rows = df[
            (df["team_id"] == team_id) & (df["target_date"] == target_date)
        ]
    if rows.empty:
        raise SubmissionInvalid(
            4,
            f"Zieltag {target_date} ist für '{team_id}' (noch) nicht "
            f"bewertet — der Joker korrigiert nur bereits bewertete Tage",
        )
    return rows.iloc[-1].to_dict()


def upsert_score_row(scores_path: Path, row: dict) -> None:
    """Zeile idempotent ersetzen (wie ``append_scores``: letzte gewinnt)."""
    combined = pd.concat(
        [pd.read_parquet(scores_path), pd.DataFrame([row])],
        ignore_index=True,
    ).drop_duplicates(subset=["team_id", "target_date"], keep="last")
    combined.to_parquet(scores_path, index=False)


def apply_joker(
    team_id: str,
    target_date: str,
    joker_csv: Path | None = None,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict:
    """Joker für *team_id* auf *target_date* anwenden.

    Liefert ``{"before": <alte Score-Zeile>, "after": <neue Score-Zeile>}``.
    Raises SubmissionInvalid (Codes 1/3/4) bzw. RuntimeError (Ist-Werte),
    ohne dass irgendetwas mutiert wurde.
    """
    teams_yml = repo_root / "teams.yml"
    sub_path = repo_root / "submissions" / team_id / f"{target_date}.csv"
    scores_path = repo_root / "data" / "scores.parquet"
    actuals_path = repo_root / "data" / "actual_load.parquet"
    if joker_csv is None:
        joker_csv = repo_root / "joker" / team_id / f"{target_date}.csv"

    # --- alle Checks vor der ersten Mutation -------------------------------
    team = check_team_registry(team_id, load_teams(teams_yml))
    check_joker_available(team)
    before = existing_score_row(scores_path, team_id, target_date)
    validate_schema(joker_csv, target_date)
    actual = load_stored_actual(target_date, actuals_path)

    # --- Mutationen ---------------------------------------------------------
    # Bei einem LOCF-bewerteten Tag existiert noch keine Datei — der
    # Joker legt sie an (einzige Ausnahme vom Deadline-Regime).
    sub_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(joker_csv, sub_path)
    mark_joker_used(teams_yml, team_id, target_date)
    forecast = pd.read_csv(sub_path)["forecast_mw"].to_numpy(dtype=float)
    after = {
        "team_id": team_id,
        "target_date": target_date,
        "scored_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "source_date": target_date,
        "carried_forward": False,
        **sd.score_submission(forecast, actual),
    }
    upsert_score_row(scores_path, after)
    return {"before": before, "after": after}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--team", required=True, help="Team-Id aus teams.yml")
    parser.add_argument("--date", required=True,
                        help="Zu ersetzender Zieltag YYYY-MM-DD (UTC)")
    parser.add_argument(
        "--file", default=None,
        help="Pfad zur Ersatz-CSV (Default: joker/<team>/<datum>.csv)")
    args = parser.parse_args()

    try:
        result = apply_joker(
            args.team, args.date,
            Path(args.file) if args.file else None,
        )
    except SubmissionInvalid as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 5

    b, a = result["before"], result["after"]
    print(f"[joker] {args.team} {args.date}: Submission ersetzt, "
          f"Tag neu bewertet (joker: \"{args.date}\" in teams.yml gesetzt)")
    for metric in ("mae", "rmse", "mape", "bias", "upr"):
        print(f"[joker]   {metric.upper():>4}: {b[metric]:>12.4f}  ->  "
              f"{a[metric]:>12.4f}")
    print("[joker] Nächste Schritte: uv run python scripts/build_leaderboard.py "
          "&& git add/commit (Submission, teams.yml, data/, public/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
