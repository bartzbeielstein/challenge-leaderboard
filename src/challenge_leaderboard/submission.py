"""challenge_leaderboard.submission — submission write contract and helpers.

Provides the importable write/contract glue that forecasting scripts use to
produce leaderboard-compatible CSVs. Mirrors the operational helpers in
bart26k-lecture/scripts/team4_optuna_submit.py (write_submission, assert_contract).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def assert_contract(y0: "pd.Series", *, first_timestamp: "pd.Timestamp") -> None:
    """Assert that *y0* meets the submission contract.

    Raises AssertionError (not SubmissionInvalid) so the calling script can
    treat this as a programming error rather than a user-facing validation
    failure.

    Checks:
    - Exactly 24 hourly steps.
    - First index entry equals *first_timestamp*.
    - All values > 0.
    - No NaN values.
    """
    assert len(y0) == 24, f"expected 24 hourly steps for y_0, got {len(y0)}"
    assert y0.index[0] == first_timestamp, (
        f"first step {y0.index[0]} != expected first_timestamp {first_timestamp}"
    )
    assert (y0 > 0).all(), "non-positive forecast value -- spec requires > 0"
    assert y0.notna().all(), "NaN in forecast -- spec forbids"


def write_submission_csv(
    y0: "pd.Series",
    *,
    target_date: str,
    team_id: str,
    lb_root: Path,
) -> Path:
    """Write *y0* as a leaderboard-compatible submission CSV.

    The file is written to ``lb_root/submissions/<team_id>/<target_date>.csv``
    with columns ``timestamp_utc`` (ISO-8601 UTC) and ``forecast_mw``
    (rounded to 2 decimal places). Parent directories are created if needed.

    Parameters
    ----------
    y0:
        A pandas Series with a UTC DatetimeIndex of 24 hourly entries.
    target_date:
        ISO date string (``YYYY-MM-DD``) of the forecast target day.
    team_id:
        Registered team identifier (used as the submissions sub-directory name).
    lb_root:
        Root of the challenge-leaderboard clone.

    Returns
    -------
    Path
        Absolute path of the written CSV file.
    """
    sub_dir = lb_root / "submissions" / team_id
    sub_dir.mkdir(parents=True, exist_ok=True)
    path = sub_dir / f"{target_date}.csv"
    df = pd.DataFrame(
        {
            "timestamp_utc": y0.index.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forecast_mw": y0.round(2).values,
        }
    )
    df.to_csv(path, index=False)
    return path
