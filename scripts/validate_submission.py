"""
validate_submission.py

Thin CLI wrapper around challenge_leaderboard.validation. Prüft eine
Submission-CSV auf Schema, Pfad-Konvention, Deadline und Team-Berechtigung.
Wird sowohl im PR-Workflow als auch lokal (vor dem Push) aufgerufen.

Exit-Codes:
  0  --- alle Checks bestanden
  1  --- Schema- oder Pfad-Verstoß
  2  --- Deadline überschritten
  3  --- Team unbekannt oder PR-Autor nicht autorisiert

CR-3: jede Verletzung beendet das Programm mit nicht-null-Code und
einer eindeutigen Fehlerzeile auf stderr; keine stille Imputation.

Module-level names (PATH_RE, EXPECTED_COLUMNS, parse_path,
validate_schema, validate_deadline, validate_authorship, load_teams) are
preserved as shims so that existing test code importing this module
with ``import validate_submission as vs`` continues to work unchanged.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from challenge_leaderboard import validation as _v
from challenge_leaderboard.teams import load_teams  # re-export for tests

# ---------------------------------------------------------------------------
# Constants — re-exported from the library so ``vs.PATH_RE`` keeps working.
# ---------------------------------------------------------------------------

PATH_RE = _v.PATH_RE
EXPECTED_COLUMNS = _v.EXPECTED_COLUMNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(code: int, message: str) -> "None":
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Shims — same signatures as before; convert SubmissionInvalid → sys.exit.
# ---------------------------------------------------------------------------

def parse_path(repo_relative: str) -> tuple[str, str]:
    try:
        return _v.parse_path(repo_relative)
    except _v.SubmissionInvalid as e:
        die(e.code, str(e))


def validate_schema(csv_path: Path, target_date: str) -> None:
    try:
        _v.validate_schema(csv_path, target_date)
    except _v.SubmissionInvalid as e:
        die(e.code, str(e))


def validate_deadline(target_date: str, now_utc=None) -> None:
    try:
        _v.validate_deadline(target_date, now_utc)
    except _v.SubmissionInvalid as e:
        die(e.code, str(e))


def validate_authorship(team_id: str, pr_author: str,
                         teams: dict[str, dict]) -> None:
    try:
        _v.validate_authorship(team_id, pr_author, teams)
    except _v.SubmissionInvalid as e:
        die(e.code, str(e))


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

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
