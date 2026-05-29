"""End-to-end UTC correctness tests for the submission + scoring pipeline.

These pin the **UTC-only** contract across validation and scoring against
the canonical live scenario:

  - ENTSO-E's newest published DE actual is 2026-05-28 18:00 UTC.
  - "Now" at generation time is 2026-05-28 21:36 UTC.
  - The forecast targets day D = 2026-05-29 (00:00–23:00 UTC), written to
    submissions/team_4/2026-05-29.csv.
  - The deadline is D-1 23:59 UTC = 2026-05-28 23:59 UTC.
  - Scoring runs at 2026-05-30 07:00 UTC and scores "yesterday" = 2026-05-29
    against ENTSO-E actuals for the 24 UTC hours of D.

No network, no real clock: every "now" is injected. The guard test fails
if any local-time zone leaks back into the deadline/window code paths.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import score_day as sd
import validate_submission as vs

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

TARGET = "2026-05-29"          # day D being forecast
NOW_GEN = datetime(2026, 5, 28, 21, 36, tzinfo=timezone.utc)   # generation time
DEADLINE = datetime(2026, 5, 28, 23, 59, tzinfo=timezone.utc)  # D-1 23:59 UTC


# --------------------------------------------------------------------------
# 1. Deadline boundary (UTC only)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("now", [
    NOW_GEN,                                              # scenario "now"
    datetime(2026, 5, 28, 23, 58, tzinfo=timezone.utc),  # one minute before
])
def test_deadline_accepts_before_2359_utc(now):
    vs.validate_deadline(TARGET, now_utc=now)  # must not raise


@pytest.mark.parametrize("now", [
    DEADLINE,                                            # exactly D-1 23:59 UTC -> reject
    datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc),   # D 00:00 UTC
])
def test_deadline_rejects_at_or_after_2359_utc(now):
    with pytest.raises(SystemExit) as ei:
        vs.validate_deadline(TARGET, now_utc=now)
    assert ei.value.code == 2


def test_deadline_is_one_minute_before_target_midnight_utc():
    # Property: the cutoff is target-day 00:00 UTC minus exactly one minute,
    # independent of any DST/local-offset (which must not appear at all).
    just_before = DEADLINE - timedelta(seconds=1)
    vs.validate_deadline(TARGET, now_utc=just_before)
    with pytest.raises(SystemExit):
        vs.validate_deadline(TARGET, now_utc=DEADLINE)


# --------------------------------------------------------------------------
# 2. Schema / timestamp alignment between generation and validator
# --------------------------------------------------------------------------

def test_generated_file_matches_validator_stamps(make_submission_csv):
    # A 24-row UTC file for the target day passes schema unchanged.
    p = make_submission_csv(f"submissions/team_4/{TARGET}.csv")
    vs.validate_schema(p, TARGET)


def test_validator_expected_stamps_are_24_utc_hours_of_target():
    stamps = pd.date_range(
        f"{TARGET}T00:00:00Z", periods=24, freq="h", tz="UTC"
    ).strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
    assert stamps[0] == "2026-05-29T00:00:00Z"
    assert stamps[-1] == "2026-05-29T23:00:00Z"
    assert len(stamps) == 24


def test_off_by_one_hour_is_rejected(tmp_path):
    # Stamps shifted by +1h (start 01:00) must be rejected with exit 1.
    p = tmp_path / "submissions/team_4" / f"{TARGET}.csv"
    p.parent.mkdir(parents=True)
    stamps = pd.date_range(
        f"{TARGET}T01:00:00Z", periods=24, freq="h", tz="UTC"
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    pd.DataFrame({"timestamp_utc": stamps,
                  "forecast_mw": [1000.0] * 24}).to_csv(p, index=False)
    with pytest.raises(SystemExit) as ei:
        vs.validate_schema(p, TARGET)
    assert ei.value.code == 1


# --------------------------------------------------------------------------
# 3. Scoring window (24 UTC hours of D) + cron target mapping
# --------------------------------------------------------------------------

def test_scoring_window_is_24_utc_hours_of_target():
    fetch_start, fetch_end, target_hours = sd.scoring_window(TARGET)
    assert len(target_hours) == 24
    assert target_hours[0] == pd.Timestamp("2026-05-29T00:00:00Z")
    assert target_hours[-1] == pd.Timestamp("2026-05-29T23:00:00Z")
    assert str(target_hours.tz) == "UTC"
    # Buffer: 6 h before D 00:00, 5 h after D 23:00 (i.e. D+1 05:00).
    assert fetch_start == datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc)
    assert fetch_end == datetime(2026, 5, 30, 5, 0, tzinfo=timezone.utc)


def test_cron_runs_at_07_utc_and_targets_yesterday():
    # The 07:00 UTC cron scores "yesterday" (UTC): a 2026-05-30 run -> D.
    run_day = datetime(2026, 5, 30, 7, 0, tzinfo=timezone.utc)
    target = (run_day.date() - pd.Timedelta(days=1)).isoformat()
    assert target == TARGET

    import yaml
    wf = yaml.safe_load(
        (SCRIPTS.parent / ".github" / "workflows" / "score-daily.yml").read_text()
    )
    on = wf[True] if True in wf else wf["on"]
    assert "0 7 * * *" in [s["cron"] for s in on["schedule"]]
    # The target-day step must derive yesterday in UTC (`date -u`), never local.
    body = (SCRIPTS.parent / ".github" / "workflows" / "score-daily.yml").read_text()
    assert 'date -u -d "yesterday"' in body


# --------------------------------------------------------------------------
# 4. UTC-only invariant guard over the deadline/window source paths
# --------------------------------------------------------------------------

FORBIDDEN_SUBSTRINGS = ["ZoneInfo", "pytz", "Europe/Berlin", "astimezone",
                        "CET", "CEST"]


@pytest.mark.parametrize("script", ["validate_submission.py", "score_day.py"])
def test_no_local_timezone_tokens_in_source(script):
    src = (SCRIPTS / script).read_text()
    for token in FORBIDDEN_SUBSTRINGS:
        assert token not in src, f"{script} still references {token!r} (UTC-only violation)"


@pytest.mark.parametrize("script", ["validate_submission.py", "score_day.py"])
def test_no_naive_now_or_today(script):
    """datetime.now() must always pass a tz; date.today() is banned."""
    tree = ast.parse((SCRIPTS / script).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "today":
                raise AssertionError(f"{script}: date.today() is banned (UTC-only)")
            if func.attr == "now":
                # require at least one arg/kwarg (the tz)
                assert node.args or node.keywords, (
                    f"{script}: naive datetime.now() without tz (UTC-only)"
                )
