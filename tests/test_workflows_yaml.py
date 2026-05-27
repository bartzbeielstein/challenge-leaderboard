"""Static lint of .github/workflows/*.yml.

Pins invariants the live pipeline depends on. The most important one:
`build-and-deploy.yml`'s `on.workflow_run.workflows` must contain the
exact `name:` of `score-daily.yml` — a typo there silently breaks the
auto-rebuild chain, which is the classic mode of failure for this
kind of two-stage pipeline.
"""
from __future__ import annotations

from pathlib import Path

import yaml


WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text())


def test_score_daily_has_07_utc_cron():
    wf = _load("score-daily.yml")
    on = wf[True] if True in wf else wf["on"]  # PyYAML quirk: `on` -> True
    crons = [s["cron"] for s in on["schedule"]]
    assert "0 7 * * *" in crons


def test_score_daily_workflow_dispatch_accepts_target_date():
    wf = _load("score-daily.yml")
    on = wf[True] if True in wf else wf["on"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert "target_date" in inputs


def test_build_and_deploy_listens_to_score_daily_name_exact():
    sd = _load("score-daily.yml")
    bd = _load("build-and-deploy.yml")
    bd_on = bd[True] if True in bd else bd["on"]
    listened = bd_on["workflow_run"]["workflows"]
    assert sd["name"] in listened, (
        f"build-and-deploy.yml listens to {listened} but score-daily.yml is "
        f"named {sd['name']!r}; the chain breaks silently if these diverge."
    )


def test_build_and_deploy_push_paths_include_scores_parquet():
    bd = _load("build-and-deploy.yml")
    bd_on = bd[True] if True in bd else bd["on"]
    paths = bd_on["push"]["paths"]
    assert "data/scores.parquet" in paths


def test_build_and_deploy_has_pages_permissions():
    bd = _load("build-and-deploy.yml")
    assert bd["permissions"]["pages"] == "write"
    assert bd["permissions"]["id-token"] == "write"


def test_validate_pr_triggers_on_pull_request():
    wf = _load("validate-pr.yml")
    on = wf[True] if True in wf else wf["on"]
    assert "pull_request" in on
