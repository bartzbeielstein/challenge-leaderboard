"""Tests for scripts/validate_submission.py.

Each scenario invokes `main()` indirectly by calling the helpers it
composes. `validate_deadline` is exercised separately so we can drive
the wall-clock without monkey-patching `datetime.now`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import validate_submission as vs


def test_parse_path_happy():
    team, date = vs.parse_path("submissions/team_4/2026-05-28.csv")
    assert team == "team_4"
    assert date == "2026-05-28"


def test_parse_path_bad_pattern_exits_1():
    with pytest.raises(SystemExit) as ei:
        vs.parse_path("submissions/team_4/bad.csv")
    assert ei.value.code == 1


def test_validate_schema_happy(make_submission_csv):
    p = make_submission_csv("submissions/team_4/2026-05-28.csv")
    vs.validate_schema(p, "2026-05-28")


def test_validate_schema_wrong_row_count(make_submission_csv):
    p = make_submission_csv("submissions/team_4/2026-05-28.csv", rows=23)
    with pytest.raises(SystemExit) as ei:
        vs.validate_schema(p, "2026-05-28")
    assert ei.value.code == 1


def test_validate_schema_negative_mw(make_submission_csv):
    p = make_submission_csv(
        "submissions/team_4/2026-05-28.csv",
        forecast=[-1.0] + [1000.0] * 23,
    )
    with pytest.raises(SystemExit) as ei:
        vs.validate_schema(p, "2026-05-28")
    assert ei.value.code == 1


def test_validate_schema_wrong_columns(make_submission_csv):
    p = make_submission_csv(
        "submissions/team_4/2026-05-28.csv",
        columns=["ts", "mw"],
    )
    with pytest.raises(SystemExit) as ei:
        vs.validate_schema(p, "2026-05-28")
    assert ei.value.code == 1


def test_validate_schema_nan_mw(tmp_path):
    p = tmp_path / "submissions/team_4/2026-05-28.csv"
    p.parent.mkdir(parents=True)
    stamps = pd.date_range("2026-05-28T00:00:00Z", periods=24, freq="h",
                            tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    df = pd.DataFrame({"timestamp_utc": stamps,
                       "forecast_mw": [float("nan")] + [1000.0] * 23})
    df.to_csv(p, index=False)
    with pytest.raises(SystemExit) as ei:
        vs.validate_schema(p, "2026-05-28")
    assert ei.value.code == 1


def test_validate_deadline_passes_before_cutoff():
    # 2026-05-28T00:00 Europe/Berlin -> 2026-05-27T22:00 UTC (CEST is +02:00)
    # Deadline = D-1 23:59 Berlin = 2026-05-27T21:59 UTC
    now = datetime(2026, 5, 27, 21, 0, tzinfo=timezone.utc)  # one hour before
    vs.validate_deadline("2026-05-28", now_utc=now)


def test_validate_deadline_blocks_after_cutoff():
    now = datetime(2026, 5, 27, 22, 0, tzinfo=timezone.utc)  # one minute past
    with pytest.raises(SystemExit) as ei:
        vs.validate_deadline("2026-05-28", now_utc=now)
    assert ei.value.code == 2


def test_authorship_unknown_team(teams_yml):
    teams = vs.load_teams(teams_yml)
    with pytest.raises(SystemExit) as ei:
        vs.validate_authorship("does_not_exist", "bartzbeielstein", teams)
    assert ei.value.code == 3


def test_authorship_wrong_user(teams_yml):
    teams = vs.load_teams(teams_yml)
    with pytest.raises(SystemExit) as ei:
        vs.validate_authorship("team_4", "some-randomer", teams)
    assert ei.value.code == 3


def test_authorship_correct_user_case_insensitive(teams_yml):
    teams = vs.load_teams(teams_yml)
    vs.validate_authorship("team_4", "BartzBeielstein", teams)
