"""Tests für die Joker-Regel (challenge_leaderboard.joker + apply_joker.py).

Hermetisch: alle Pfade unter tmp_path, kein ENTSO-E, kein echtes
data/scores.parquet. Abgedeckt:

  * Happy Path — CSV ersetzt, teams.yml markiert, genau EINE Score-Zeile
    neu bewertet, alle anderen Zeilen byte-identisch
  * LOCF-Tag ohne eigene Submission — Joker legt die Datei neu an
  * Kommentar-Erhalt in teams.yml (kein yaml.dump-Round-Trip)
  * ``joker: false`` zählt als verfügbar und wird in-place ersetzt
  * Ablehnungen: Joker bereits eingesetzt (4), Zieltag nicht bewertet
    (4), Schema-Verstoß (1), Pseudo-/Retired-Team (3), Ist-Werte
    unvollständig (RuntimeError) — jeweils OHNE Mutation (CR-3: kein
    halber Joker)
"""
from __future__ import annotations

import pandas as pd
import pytest

import apply_joker as aj
from challenge_leaderboard.joker import check_joker_available, mark_joker_used
from challenge_leaderboard.teams import load_teams
from challenge_leaderboard.validation import SubmissionInvalid

DAY = "2026-06-22"
OTHER_DAY = "2026-06-21"

TEAMS_TEXT = """\
# Team-Registry (Test) — dieser Kommentar muss erhalten bleiben.
teams:
  - id: team_4   # inline-Kommentar bleibt erhalten
    display_name: "Team 4"
    github_handles: [bartzbeielstein]
    certified: "No"
  - id: hot_rod
    display_name: "Hot Rod"
    github_handles: [someone-else]
    joker: false
    certified: "No"
  - id: used_up
    display_name: "Used Up"
    github_handles: [x]
    joker: "2026-06-01"
  - id: entsoe
    display_name: "ENTSO-E"
    pseudo: true
  - id: old_team
    display_name: "Old Team"
    github_handles: [y]
    retired: true
"""


def hourly_stamps(day: str) -> list[str]:
    return pd.date_range(
        f"{day}T00:00:00Z", periods=24, freq="h", tz="UTC"
    ).strftime("%Y-%m-%dT%H:%M:%SZ").tolist()


def write_submission(root, team: str, day: str, base: float) -> None:
    p = root / "submissions" / team / f"{day}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "timestamp_utc": hourly_stamps(day),
        "forecast_mw": [base + i for i in range(24)],
    }).to_csv(p, index=False)


def write_joker_csv(root, team: str, day: str, values) -> None:
    p = root / "joker" / team / f"{day}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "timestamp_utc": hourly_stamps(day), "forecast_mw": values,
    }).to_csv(p, index=False)


def score_row(team: str, day: str, mae: float) -> dict:
    return {
        "team_id": team, "target_date": day,
        "scored_at_utc": "2026-06-23T09:00:00+00:00", "source_date": day,
        "carried_forward": False, "mae": mae, "rmse": mae, "mape": 1.0,
        "bias": -mae, "upr": 100.0,
    }


@pytest.fixture
def joker_repo(tmp_path):
    """Mini-Repo: teams.yml, Submissions, committete Actuals, Scores."""
    (tmp_path / "teams.yml").write_text(TEAMS_TEXT)
    for team, base in [("team_4", 30000.0), ("hot_rod", 31000.0),
                       ("used_up", 32000.0)]:
        write_submission(tmp_path, team, DAY, base)
    write_submission(tmp_path, "team_4", OTHER_DAY, 30000.0)
    (tmp_path / "data").mkdir()
    # Actuals: Zieltag exakt load = 40000 + Stunde -> perfekter Joker = MAE 0.
    pd.DataFrame({
        "timestamp_utc": hourly_stamps(DAY),
        "load_mw": [40000.0 + i for i in range(24)],
        "entsoe_forecast_mw": [39000.0] * 24,
    }).to_parquet(tmp_path / "data" / "actual_load.parquet", index=False)
    pd.DataFrame([
        score_row("team_4", DAY, 10000.0),
        score_row("team_4", OTHER_DAY, 9000.0),
        score_row("hot_rod", DAY, 9000.0),
        score_row("used_up", DAY, 8000.0),
    ]).to_parquet(tmp_path / "data" / "scores.parquet", index=False)
    return tmp_path


def snapshot(root) -> dict:
    """Byte-Stände aller mutierbaren Artefakte (für No-Mutation-Asserts)."""
    return {
        "teams": (root / "teams.yml").read_bytes(),
        "sub": (root / "submissions" / "team_4" / f"{DAY}.csv").read_bytes(),
        "scores": (root / "data" / "scores.parquet").read_bytes(),
    }


# ---------------------------------------------------------------------------
# Happy Path
# ---------------------------------------------------------------------------

def test_apply_joker_happy_path(joker_repo):
    write_joker_csv(joker_repo, "team_4", DAY,
                    [40000.0 + i for i in range(24)])
    result = aj.apply_joker("team_4", DAY, repo_root=joker_repo)

    # Submission durch Joker-CSV ersetzt
    sub = joker_repo / "submissions" / "team_4" / f"{DAY}.csv"
    assert sub.read_bytes() == (
        joker_repo / "joker" / "team_4" / f"{DAY}.csv").read_bytes()
    # Buchführung
    assert load_teams(joker_repo / "teams.yml")["team_4"]["joker"] == DAY
    # Vorher/Nachher
    assert result["before"]["mae"] == pytest.approx(10000.0)
    assert result["after"]["mae"] == pytest.approx(0.0)
    assert result["after"]["carried_forward"] is False
    assert result["after"]["source_date"] == DAY


def test_apply_joker_touches_exactly_one_score_row(joker_repo):
    write_joker_csv(joker_repo, "team_4", DAY,
                    [40000.0 + i for i in range(24)])
    before = pd.read_parquet(joker_repo / "data" / "scores.parquet")
    aj.apply_joker("team_4", DAY, repo_root=joker_repo)
    after = pd.read_parquet(joker_repo / "data" / "scores.parquet")

    assert len(after) == len(before)  # ersetzt, nicht dupliziert
    changed = after[(after.team_id == "team_4") & (after.target_date == DAY)]
    assert changed["mae"].item() == pytest.approx(0.0)
    # Alle anderen Zeilen unverändert (hot_rod, used_up, anderer Tag)
    key = ["team_id", "target_date"]
    others_before = before[~((before.team_id == "team_4")
                             & (before.target_date == DAY))]
    others_after = after[~((after.team_id == "team_4")
                           & (after.target_date == DAY))]
    pd.testing.assert_frame_equal(
        others_before.sort_values(key).reset_index(drop=True),
        others_after.sort_values(key).reset_index(drop=True),
    )


def test_apply_joker_preserves_comments(joker_repo):
    write_joker_csv(joker_repo, "team_4", DAY,
                    [40000.0 + i for i in range(24)])
    aj.apply_joker("team_4", DAY, repo_root=joker_repo)
    text = (joker_repo / "teams.yml").read_text()
    assert "# Team-Registry (Test) — dieser Kommentar muss erhalten bleiben." in text
    assert "# inline-Kommentar bleibt erhalten" in text


def test_joker_false_counts_as_available_and_is_replaced(joker_repo):
    write_joker_csv(joker_repo, "hot_rod", DAY,
                    [40000.0 + i for i in range(24)])
    aj.apply_joker("hot_rod", DAY, repo_root=joker_repo)
    text = (joker_repo / "teams.yml").read_text()
    assert 'joker: "2026-06-22"' in text
    assert "joker: false" not in text  # in-place ersetzt, nicht doppelt
    assert load_teams(joker_repo / "teams.yml")["hot_rod"]["joker"] == DAY


# ---------------------------------------------------------------------------
# Ablehnungen — jeweils ohne Mutation
# ---------------------------------------------------------------------------

def test_joker_already_used_rejected(joker_repo):
    write_joker_csv(joker_repo, "used_up", DAY,
                    [40000.0 + i for i in range(24)])
    before = snapshot(joker_repo)
    with pytest.raises(SubmissionInvalid) as exc:
        aj.apply_joker("used_up", DAY, repo_root=joker_repo)
    assert exc.value.code == 4
    assert snapshot(joker_repo) == before


def test_joker_fills_locf_scored_day(joker_repo):
    """LOCF-bewerteter Tag ohne eigene Submission: Joker legt die Datei an."""
    locf_day = "2026-06-24"
    scores_path = joker_repo / "data" / "scores.parquet"
    locf = score_row("team_4", locf_day, 12000.0)
    locf.update(carried_forward=True, source_date=DAY)
    pd.concat([pd.read_parquet(scores_path), pd.DataFrame([locf])],
              ignore_index=True).to_parquet(scores_path, index=False)
    actuals_path = joker_repo / "data" / "actual_load.parquet"
    day_actuals = pd.DataFrame({
        "timestamp_utc": hourly_stamps(locf_day),
        "load_mw": [41000.0 + i for i in range(24)],
        "entsoe_forecast_mw": [39000.0] * 24,
    })
    pd.concat([pd.read_parquet(actuals_path), day_actuals],
              ignore_index=True).to_parquet(actuals_path, index=False)
    write_joker_csv(joker_repo, "team_4", locf_day,
                    [41000.0 + i for i in range(24)])

    result = aj.apply_joker("team_4", locf_day, repo_root=joker_repo)

    sub = joker_repo / "submissions" / "team_4" / f"{locf_day}.csv"
    assert sub.exists()  # Datei wurde neu angelegt
    assert result["before"]["carried_forward"] is True
    assert result["after"]["carried_forward"] is False
    assert result["after"]["source_date"] == locf_day
    assert result["after"]["mae"] == pytest.approx(0.0)
    assert load_teams(joker_repo / "teams.yml")["team_4"]["joker"] == locf_day


def test_joker_unscored_missing_day_rejected(joker_repo):
    """Weder Submission noch Score-Zeile: kein Joker (Exit 4), keine Datei."""
    missing_day = "2026-06-25"
    write_joker_csv(joker_repo, "team_4", missing_day,
                    [40000.0 + i for i in range(24)])
    with pytest.raises(SubmissionInvalid) as exc:
        aj.apply_joker("team_4", missing_day, repo_root=joker_repo)
    assert exc.value.code == 4
    assert not (joker_repo / "submissions" / "team_4"
                / f"{missing_day}.csv").exists()


def test_joker_requires_scored_day(joker_repo):
    unscored = "2026-06-23"
    write_submission(joker_repo, "team_4", unscored, 30000.0)
    write_joker_csv(joker_repo, "team_4", unscored,
                    [40000.0 + i for i in range(24)])
    with pytest.raises(SubmissionInvalid) as exc:
        aj.apply_joker("team_4", unscored, repo_root=joker_repo)
    assert exc.value.code == 4


def test_joker_schema_violation_rejected_without_mutation(joker_repo):
    p = joker_repo / "joker" / "team_4" / f"{DAY}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({  # nur 23 Zeilen -> Schema-Verstoß
        "timestamp_utc": hourly_stamps(DAY)[:23],
        "forecast_mw": [40000.0] * 23,
    }).to_csv(p, index=False)
    before = snapshot(joker_repo)
    with pytest.raises(SubmissionInvalid) as exc:
        aj.apply_joker("team_4", DAY, repo_root=joker_repo)
    assert exc.value.code == 1
    assert snapshot(joker_repo) == before


@pytest.mark.parametrize("team_id", ["entsoe", "old_team", "ghost"])
def test_pseudo_retired_unknown_rejected(joker_repo, team_id):
    with pytest.raises(SubmissionInvalid) as exc:
        aj.apply_joker(team_id, DAY, repo_root=joker_repo)
    assert exc.value.code == 3


def test_incomplete_actuals_rejected_without_mutation(joker_repo):
    df = pd.read_parquet(joker_repo / "data" / "actual_load.parquet")
    df.loc[5, "load_mw"] = float("nan")
    df.to_parquet(joker_repo / "data" / "actual_load.parquet", index=False)
    write_joker_csv(joker_repo, "team_4", DAY,
                    [40000.0 + i for i in range(24)])
    before = snapshot(joker_repo)
    with pytest.raises(RuntimeError, match="unvollständig"):
        aj.apply_joker("team_4", DAY, repo_root=joker_repo)
    assert snapshot(joker_repo) == before


# ---------------------------------------------------------------------------
# Einheiten: check_joker_available / mark_joker_used
# ---------------------------------------------------------------------------

def test_check_joker_available_semantics():
    check_joker_available({"id": "t"})                  # fehlt -> verfügbar
    check_joker_available({"id": "t", "joker": False})  # false -> verfügbar
    check_joker_available({"id": "t", "joker": None})   # null -> verfügbar
    with pytest.raises(SubmissionInvalid) as exc:
        check_joker_available({"id": "t", "joker": "2026-06-01"})
    assert exc.value.code == 4


def test_mark_joker_used_inserts_after_id_line(joker_repo):
    teams_yml = joker_repo / "teams.yml"
    mark_joker_used(teams_yml, "team_4", DAY)
    assert load_teams(teams_yml)["team_4"]["joker"] == DAY
    # zweiter Einsatz scheitert (defensiv, Code 4)
    with pytest.raises(SubmissionInvalid) as exc:
        mark_joker_used(teams_yml, "team_4", "2026-06-30")
    assert exc.value.code == 4


def test_mark_joker_used_unknown_team(joker_repo):
    with pytest.raises(SubmissionInvalid) as exc:
        mark_joker_used(joker_repo / "teams.yml", "ghost", DAY)
    assert exc.value.code == 4
