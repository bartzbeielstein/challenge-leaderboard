"""challenge_leaderboard — scoring and leaderboard package for the SoSe26 load-forecast challenge.

Provides the single source of truth for submission validation and leaderboard
glue. The library raises typed exceptions (no sys.exit); CLI scripts layer the
sys.exit behaviour on top.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .validation import (  # noqa: E402
    SubmissionInvalid,
    validate_submission_file,
    EXPECTED_COLUMNS,
    PATH_RE,
)
from .teams import load_teams, check_team_registry  # noqa: E402

__all__ = [
    "__version__",
    "SubmissionInvalid",
    "validate_submission_file",
    "EXPECTED_COLUMNS",
    "PATH_RE",
    "load_teams",
    "check_team_registry",
]
