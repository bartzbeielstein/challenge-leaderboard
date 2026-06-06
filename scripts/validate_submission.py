"""
validate_submission.py

Prüft eine Submission-CSV auf Schema, Pfad-Konvention, Deadline und
Team-Berechtigung. Wird sowohl im PR-Workflow als auch lokal (vor dem
Push) aufgerufen.

Exit-Codes:
  0  --- alle Checks bestanden
  1  --- Schema- oder Pfad-Verstoß
  2  --- Deadline überschritten
  3  --- Team unbekannt oder PR-Autor nicht autorisiert

CR-3: jede Verletzung beendet das Programm mit nicht-null-Code und
einer eindeutigen Fehlerzeile auf stderr; keine stille Imputation.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml


PATH_RE = re.compile(r"^submissions/(?P<team>[a-z0-9_]+)/(?P<date>\d{4}-\d{2}-\d{2})\.csv$")
EXPECTED_COLUMNS = ["timestamp_utc", "forecast_mw"]


def die(code: int, message: str) -> "None":
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def load_teams(teams_yml: Path) -> dict[str, dict]:
    data = yaml.safe_load(teams_yml.read_text())
    return {t["id"]: t for t in data.get("teams") or []}


def parse_path(repo_relative: str) -> tuple[str, str]:
    m = PATH_RE.match(repo_relative)
    if not m:
        die(1, f"Pfad '{repo_relative}' entspricht nicht "
               "submissions/<team_id>/<YYYY-MM-DD>.csv")
    return m.group("team"), m.group("date")


def validate_schema(csv_path: Path, target_date: str) -> None:
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        die(1, f"CSV nicht lesbar: {exc}")

    if list(df.columns) != EXPECTED_COLUMNS:
        die(1, f"Spalten {list(df.columns)} != erwartete {EXPECTED_COLUMNS}")
    if len(df) != 24:
        die(1, f"24 Zeilen erwartet, aber {len(df)} gefunden")
    if df["forecast_mw"].isna().any():
        die(1, "forecast_mw enthält NaN-Werte (CR-3-Verstoß)")
    if (df["forecast_mw"] <= 0).any():
        die(1, "forecast_mw enthält nicht-positive Werte")

    expected_stamps = pd.date_range(
        f"{target_date}T00:00:00Z", periods=24, freq="h", tz="UTC"
    ).strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
    actual_stamps = df["timestamp_utc"].astype(str).tolist()
    if actual_stamps != expected_stamps:
        for i, (a, e) in enumerate(zip(actual_stamps, expected_stamps)):
            if a != e:
                die(1, f"timestamp_utc[{i}] = '{a}' != '{e}'")
        die(1, "timestamp_utc-Reihe weicht ab (Länge/Reihenfolge)")


def validate_deadline(target_date: str, now_utc: datetime | None = None) -> None:
    now = now_utc or datetime.now(tz=timezone.utc)
    # Deadline = D-1 23:59 UTC = Zieltag 00:00 UTC minus 1 Minute.
    # Alles in UTC — keine lokale Zeitzone (CR: UTC-only).
    target_midnight = datetime.fromisoformat(f"{target_date}T00:00:00") \
        .replace(tzinfo=timezone.utc)
    deadline = target_midnight - timedelta(minutes=1)
    if now >= deadline:
        die(2, f"Deadline {deadline.isoformat()} (UTC) überschritten "
               f"(jetzt {now.isoformat()})")


def validate_authorship(team_id: str, pr_author: str,
                         teams: dict[str, dict]) -> None:
    team = teams.get(team_id)
    if team is None:
        die(3, f"Team '{team_id}' nicht in teams.yml registriert")
    if team.get("pseudo", False):
        die(3, f"Team '{team_id}' ist ein Pseudo-Team (Scores werden direkt "
               f"aus den ENTSO-E-Daten abgeleitet); CSV-Submissions sind "
               f"nicht erlaubt")
    handles = [h.lower() for h in team.get("github_handles", [])]
    if pr_author.lower() not in handles:
        die(3, f"PR-Autor '{pr_author}' nicht in github_handles für "
               f"Team '{team_id}': {handles}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True,
                        help="Repo-relativer Pfad zur Submission-CSV")
    parser.add_argument("--teams", default="teams.yml")
    parser.add_argument("--pr-author", default=None,
                        help="GitHub-Handle des PR-Autors; übersprungen wenn leer")
    parser.add_argument("--skip-deadline", action="store_true",
                        help="Deadline-Check überspringen (für lokale Tests)")
    args = parser.parse_args()

    rel = args.path
    team_id, target_date = parse_path(rel)
    validate_schema(Path(rel), target_date)
    if not args.skip_deadline:
        validate_deadline(target_date)
    if args.pr_author:
        teams = load_teams(Path(args.teams))
        validate_authorship(team_id, args.pr_author, teams)

    print(f"OK: team={team_id} target_date={target_date} file={rel}")


if __name__ == "__main__":
    main()
