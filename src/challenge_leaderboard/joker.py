"""challenge_leaderboard.joker — one-joker-per-team rule helpers.

Jedes Team darf während der gesamten Challenge genau EINEN Joker
einsetzen: ein bereits bewerteter Zieltag — auch ein per LOCF
bewerteter Tag ohne eigene Submission — wird durch eine aktualisierte
Prognose korrigiert (Anwendung: ``scripts/apply_joker.py``). Buchführung in ``teams.yml``
über den Schlüssel ``joker``: fehlt oder ``false`` = Joker verfügbar;
nach Einsatz das ISO-Datum des ersetzten Zieltages, z. B.
``joker: "2026-06-22"``.

Exit-code contract (extends the 1–3 contract in ``validation.py``):
  4 — Joker-Regel verletzt (bereits eingesetzt, Zieltag nicht bewertet)

All public functions RAISE SubmissionInvalid instead of calling
sys.exit; CLI scripts convert to process exit codes.
"""

from __future__ import annotations

import re
from pathlib import Path

from .validation import SubmissionInvalid

# Trifft die Zeile ``  - id: <team_id>`` (optional mit Inline-Kommentar).
_ID_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s*id:\s*(?P<id>[A-Za-z0-9_]+)\s*(#.*)?$"
)
_JOKER_LINE_RE = re.compile(r"^(?P<indent>\s*)joker:")


def check_joker_available(team: dict) -> None:
    """Raise SubmissionInvalid(4, ...) if *team* has already used its joker.

    ``joker`` fehlend, ``None``, ``false`` oder leer = verfügbar; ein
    truthy Wert (das ISO-Datum des ersetzten Zieltages) = eingesetzt.
    """
    used = team.get("joker") or False
    if used:
        raise SubmissionInvalid(
            4,
            f"Team '{team.get('id')}' hat seinen Joker bereits eingesetzt "
            f"(joker: \"{used}\" in teams.yml) — nur ein Joker pro Team",
        )


def _team_block(lines: list[str], team_id: str) -> tuple[int, int, str]:
    """Return (start, end, indent) of the ``- id: team_id`` block in *lines*.

    *start* is the index of the id line, *end* the index of the next
    ``- id:`` line (or ``len(lines)``). Raises SubmissionInvalid(4, ...)
    if the team is not found.
    """
    start = indent = None
    for i, line in enumerate(lines):
        m = _ID_LINE_RE.match(line)
        if m and m.group("id") == team_id:
            start, indent = i, m.group("indent")
            break
    if start is None:
        raise SubmissionInvalid(
            4, f"Team '{team_id}' nicht in teams.yml gefunden"
        )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _ID_LINE_RE.match(lines[j]):
            end = j
            break
    return start, end, indent


def mark_joker_used(teams_yml: Path, team_id: str, target_date: str) -> None:
    """Set ``joker: "<target_date>"`` on *team_id* in *teams_yml*.

    Kommentar-erhaltend: teams.yml wird NICHT via yaml.dump neu
    geschrieben (das würde den Schema-Kommentarblock zerstören), sondern
    per gezielter Text-Einfügung direkt nach der ``- id:``-Zeile des
    Teams; eine vorhandene ``joker: false``-Zeile wird ersetzt.

    Defensiv wird die Verfügbarkeit erneut geprüft (nie doppelt
    markieren) und das Ergebnis per YAML-Round-Trip verifiziert.
    """
    from .teams import load_teams  # noqa: PLC0415 — circular import at module level

    team = load_teams(teams_yml).get(team_id)
    if team is None:
        raise SubmissionInvalid(
            4, f"Team '{team_id}' nicht in {teams_yml} registriert"
        )
    check_joker_available(team)

    lines = teams_yml.read_text().splitlines(keepends=True)
    start, end, indent = _team_block(lines, team_id)
    new_line = f'{indent}  joker: "{target_date}"\n'
    for k in range(start + 1, end):
        m = _JOKER_LINE_RE.match(lines[k])
        if m:  # vorhandenes ``joker: false`` ersetzen
            lines[k] = f'{m.group("indent")}joker: "{target_date}"\n'
            break
    else:
        lines.insert(start + 1, new_line)
    teams_yml.write_text("".join(lines))

    if load_teams(teams_yml).get(team_id, {}).get("joker") != target_date:
        raise RuntimeError(
            f"teams.yml-Update fehlgeschlagen: joker-Schlüssel für "
            f"'{team_id}' nach dem Schreiben nicht '{target_date}'"
        )
