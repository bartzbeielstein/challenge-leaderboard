"""challenge_leaderboard.validation — submission validation library.

All public functions RAISE SubmissionInvalid instead of calling sys.exit.
Exit-code contract (same as the CLI):
  1 — schema or path violation
  2 — deadline exceeded
  3 — team unknown / pseudo / retired / author not authorised

CLI scripts wrap these in try/except → sys.exit(e.code) to preserve the
original behaviour for callers that rely on process exit codes.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Public constants (re-exported from __init__)
# ---------------------------------------------------------------------------

PATH_RE = re.compile(
    r"^submissions/(?P<team>[a-z0-9_]+)/(?P<date>\d{4}-\d{2}-\d{2})\.csv$"
)
EXPECTED_COLUMNS = ["timestamp_utc", "forecast_mw"]


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class SubmissionInvalid(Exception):
    """Raised by any validation step on a rule violation.

    Attributes
    ----------
    code : int
        The intended process exit code (1, 2, or 3).
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def parse_path(repo_relative: str) -> tuple[str, str]:
    """Parse *repo_relative* and return (team_id, target_date).

    Raises SubmissionInvalid(1, ...) if the path does not match the
    ``submissions/<team_id>/<YYYY-MM-DD>.csv`` convention.
    """
    m = PATH_RE.match(repo_relative)
    if not m:
        raise SubmissionInvalid(
            1,
            f"Pfad '{repo_relative}' entspricht nicht "
            "submissions/<team_id>/<YYYY-MM-DD>.csv",
        )
    return m.group("team"), m.group("date")


def validate_schema(csv_path: Path, target_date: str) -> None:
    """Validate the CSV at *csv_path* for *target_date*.

    Checks performed:
    - File is readable.
    - Columns match EXPECTED_COLUMNS exactly.
    - Exactly 24 rows.
    - No NaN in forecast_mw.
    - All forecast_mw values > 0.
    - timestamp_utc column matches the expected hourly UTC sequence for *target_date*.

    Raises SubmissionInvalid(1, ...) on any violation.
    """
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        raise SubmissionInvalid(1, f"CSV nicht lesbar: {exc}") from exc

    if list(df.columns) != EXPECTED_COLUMNS:
        raise SubmissionInvalid(
            1, f"Spalten {list(df.columns)} != erwartete {EXPECTED_COLUMNS}"
        )
    if len(df) != 24:
        raise SubmissionInvalid(
            1, f"24 Zeilen erwartet, aber {len(df)} gefunden"
        )
    if df["forecast_mw"].isna().any():
        raise SubmissionInvalid(1, "forecast_mw enthält NaN-Werte (CR-3-Verstoß)")
    if (df["forecast_mw"] <= 0).any():
        raise SubmissionInvalid(1, "forecast_mw enthält nicht-positive Werte")

    expected_stamps = (
        pd.date_range(
            f"{target_date}T00:00:00Z", periods=24, freq="h", tz="UTC"
        )
        .strftime("%Y-%m-%dT%H:%M:%SZ")
        .tolist()
    )
    actual_stamps = df["timestamp_utc"].astype(str).tolist()
    if actual_stamps != expected_stamps:
        for i, (a, e) in enumerate(zip(actual_stamps, expected_stamps)):
            if a != e:
                raise SubmissionInvalid(
                    1, f"timestamp_utc[{i}] = '{a}' != '{e}'"
                )
        raise SubmissionInvalid(
            1, "timestamp_utc-Reihe weicht ab (Länge/Reihenfolge)"
        )


def validate_deadline(
    target_date: str, now_utc: datetime | None = None
) -> None:
    """Raise SubmissionInvalid(2, ...) if the deadline for *target_date* has passed.

    Deadline = D-1 23:59 UTC (= midnight of the target day minus 1 minute).
    All comparisons are UTC-only (no local timezone).
    """
    now = now_utc or datetime.now(tz=timezone.utc)
    target_midnight = datetime.fromisoformat(f"{target_date}T00:00:00").replace(
        tzinfo=timezone.utc
    )
    deadline = target_midnight - timedelta(minutes=1)
    if now >= deadline:
        raise SubmissionInvalid(
            2,
            f"Deadline {deadline.isoformat()} (UTC) überschritten "
            f"(jetzt {now.isoformat()})",
        )


def validate_authorship(
    team_id: str, pr_author: str, teams: dict[str, dict]
) -> None:
    """Raise SubmissionInvalid(3, ...) if *pr_author* is not authorised for *team_id*.

    Also raises for unknown teams, pseudo teams, and retired teams.
    *teams* is a dict as returned by ``load_teams``.
    """
    from .teams import check_team_registry  # noqa: PLC0415 — avoid circular import at module level

    team = check_team_registry(team_id, teams)
    handles = [h.lower() for h in team.get("github_handles", [])]
    if pr_author.lower() not in handles:
        raise SubmissionInvalid(
            3,
            f"PR-Autor '{pr_author}' nicht in github_handles für "
            f"Team '{team_id}': {handles}",
        )


def validate_submission_file(
    repo_relative: str,
    *,
    csv_path: Path | None = None,
    teams_yml: Path | None = None,
    pr_author: str | None = None,
    skip_deadline: bool = False,
) -> tuple[str, str]:
    """Full validation pipeline for a single submission file.

    Runs in order: parse_path → validate_schema → validate_deadline
    (unless *skip_deadline*) → validate_authorship (if *pr_author* given).

    Parameters
    ----------
    repo_relative:
        The repo-relative path string, e.g. ``submissions/team_4/2026-06-16.csv``.
    csv_path:
        Filesystem path to the CSV. Defaults to ``Path(repo_relative)``.
    teams_yml:
        Path to ``teams.yml``. Required when *pr_author* is provided.
    pr_author:
        GitHub handle of the PR author. Skipped when None or empty.
    skip_deadline:
        Skip the deadline check (useful for local testing).

    Returns
    -------
    tuple[str, str]
        ``(team_id, target_date)`` on success.

    Raises
    ------
    SubmissionInvalid
        On any rule violation, with ``.code`` set to 1, 2, or 3.
    """
    from .teams import load_teams  # noqa: PLC0415

    team_id, target_date = parse_path(repo_relative)
    validate_schema(csv_path or Path(repo_relative), target_date)
    if not skip_deadline:
        validate_deadline(target_date)
    if pr_author:
        if teams_yml is None:
            raise ValueError("teams_yml must be provided when pr_author is given")
        teams = load_teams(teams_yml)
        validate_authorship(team_id, pr_author, teams)
    return team_id, target_date
