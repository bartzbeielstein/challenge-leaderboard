"""
validate_submission.py

Prüft eine Submission auf Schema, Pfad-Konvention, Deadline und
Team-Berechtigung. Unterstützt das zweistufige Commit-Reveal-
Verfahren:

  * `.commit`-Datei (vor Commit-Deadline `D-1 23:59 Europe/Berlin`):
    enthält die SHA-256-Hex-Summe der späteren CSV.
  * `.csv`-Datei (nach Commit-Deadline): die eigentliche Prognose.
    Ihre SHA-256-Summe muss mit der auf `main` liegenden
    `.commit`-Datei desselben (Team, Zieldatum)-Paares übereinstimmen.

Wird sowohl im PR-Workflow als auch lokal (vor dem Push) aufgerufen.

Exit-Codes:
  0  --- alle Checks bestanden
  1  --- Schema-, Pfad- oder Hash-Verstoß
  2  --- Deadline überschritten (Commit zu spät / Reveal zu früh)
  3  --- Team unbekannt oder PR-Autor nicht autorisiert

CR-3: jede Verletzung beendet das Programm mit nicht-null-Code und
einer eindeutigen Fehlerzeile auf stderr; keine stille Imputation.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml


PATH_RE = re.compile(
    r"^submissions/(?P<team>[a-z0-9_]+)/(?P<date>\d{4}-\d{2}-\d{2})"
    r"\.(?P<ext>csv|commit)$"
)
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{64}$")
EXPECTED_COLUMNS = ["timestamp_utc", "forecast_mw"]
DEADLINE_TZ = ZoneInfo("Europe/Berlin")


def die(code: int, message: str) -> "None":
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def load_teams(teams_yml: Path) -> dict[str, dict]:
    data = yaml.safe_load(teams_yml.read_text())
    return {t["id"]: t for t in data.get("teams") or []}


def parse_path(repo_relative: str) -> tuple[str, str, str]:
    m = PATH_RE.match(repo_relative)
    if not m:
        die(1, f"Pfad '{repo_relative}' entspricht nicht "
               "submissions/<team_id>/<YYYY-MM-DD>.{csv,commit}")
    return m.group("team"), m.group("date"), m.group("ext")


def validate_commit_schema(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        die(1, f"Commit-Datei nicht lesbar: {exc}")
    if not COMMIT_RE.match(text):
        die(1, f"Commit-Datei muss genau eine 64-stellige SHA-256-Hex-"
               f"Zeichenkette enthalten (gefunden: '{text[:80]}')")


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_reveal_hash(csv_path: Path) -> None:
    commit_path = csv_path.with_suffix(".commit")
    if not commit_path.exists():
        die(1, f"Keine zugehörige Commit-Datei {commit_path} auf main — "
               f"der SHA-256-Commit muss vor der Commit-Deadline "
               f"eingereicht worden sein")
    expected = commit_path.read_text(encoding="utf-8").strip().lower()
    actual = sha256_hex(csv_path).lower()
    if actual != expected:
        die(1, f"SHA-256 der CSV ({actual}) stimmt nicht mit Commit "
               f"({expected}) überein")


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


def validate_deadline(target_date: str, ext: str,
                       now_utc: datetime | None = None) -> None:
    """Commit muss vor, Reveal nach der Commit-Deadline eingereicht sein.

    Commit-Deadline = D-1 23:59 Europe/Berlin = D 00:00 minus 1 min.
    """
    now = now_utc or datetime.now(tz=timezone.utc)
    commit_deadline = datetime.fromisoformat(f"{target_date}T00:00:00") \
        .replace(tzinfo=DEADLINE_TZ) - pd.Timedelta(minutes=1)
    commit_deadline_utc = commit_deadline.astimezone(timezone.utc)
    if ext == "commit":
        if now >= commit_deadline_utc:
            die(2, f"Commit-Deadline {commit_deadline.isoformat()} "
                   f"überschritten (jetzt {now.isoformat()})")
    elif ext == "csv":
        if now < commit_deadline_utc:
            die(2, f"Reveal-CSV darf erst nach Commit-Deadline "
                   f"{commit_deadline.isoformat()} eingereicht werden "
                   f"(jetzt {now.isoformat()})")


def validate_authorship(team_id: str, pr_author: str,
                         teams: dict[str, dict]) -> None:
    team = teams.get(team_id)
    if team is None:
        die(3, f"Team '{team_id}' nicht in teams.yml registriert")
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
    team_id, target_date, ext = parse_path(rel)
    if ext == "commit":
        validate_commit_schema(Path(rel))
    else:
        validate_schema(Path(rel), target_date)
        validate_reveal_hash(Path(rel))
    if not args.skip_deadline:
        validate_deadline(target_date, ext)
    if args.pr_author:
        teams = load_teams(Path(args.teams))
        validate_authorship(team_id, args.pr_author, teams)

    print(f"OK: team={team_id} target_date={target_date} kind={ext} file={rel}")


if __name__ == "__main__":
    main()
