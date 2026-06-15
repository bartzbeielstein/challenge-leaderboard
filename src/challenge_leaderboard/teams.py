"""challenge_leaderboard.teams — team registry helpers.

Functions operate on the parsed teams.yml dict and raise SubmissionInvalid
(code 3) on registry violations so callers get a typed exception rather than
a sys.exit.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def load_teams(teams_yml: Path) -> dict[str, dict]:
    """Parse teams.yml and return a dict keyed by team id."""
    data = yaml.safe_load(teams_yml.read_text())
    return {t["id"]: t for t in data.get("teams") or []}


def check_team_registry(team_id: str, teams: dict[str, dict]) -> dict:
    """Return the team dict for *team_id*, raising SubmissionInvalid(3, ...) on any violation.

    Checks performed (in order):
    1. Team must exist in the registry.
    2. Team must not be a pseudo team (entsoe-derived; CSV submissions forbidden).
    3. Team must not be retired (left the live competition).
    """
    # Import here to avoid a circular-import cycle (teams <- validation <- teams).
    from .validation import SubmissionInvalid  # noqa: PLC0415

    team = teams.get(team_id)
    if team is None:
        raise SubmissionInvalid(3, f"Team '{team_id}' nicht in teams.yml registriert")
    if team.get("pseudo", False):
        raise SubmissionInvalid(
            3,
            f"Team '{team_id}' ist ein Pseudo-Team (Scores werden direkt "
            f"aus den ENTSO-E-Daten abgeleitet); CSV-Submissions sind "
            f"nicht erlaubt",
        )
    if team.get("retired", False):
        raise SubmissionInvalid(
            3,
            f"Team '{team_id}' nimmt nicht mehr am Live-Wettbewerb teil "
            f"(retired in teams.yml); neue Submissions sind nicht erlaubt",
        )
    return team
