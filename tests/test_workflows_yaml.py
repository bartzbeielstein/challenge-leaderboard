"""Static lint of .github/workflows/*.yml.

Pins invariants the live pipeline depends on. The most important one:
`build-and-deploy.yml` must rebuild the Pages site from the auto-merge
`push` that lands `data/scores.parquet` on `main` — and must NOT also
listen on `workflow_run` of Daily Scoring. Daily Scoring opens a bot PR
that auto-merge.yml merges, so a `workflow_run` build fires *before* that
merge (stale, pre-merge data) and races the correct push build on the
shared "pages" concurrency group; the stale build can win and publish a
leaderboard missing the day just scored (observed 2026-07-05).
"""
from __future__ import annotations

from pathlib import Path

import yaml


WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text())


def test_score_daily_has_09_utc_cron():
    # The 09 UTC hour gives margin over ENTSO-E's H+1 publication floor for
    # the last UTC hour; see the comment in score-daily.yml and README. The
    # minute is deliberately off the full hour (GitHub delays/drops schedules
    # at :00), so assert the hour, not the exact minute.
    wf = _load("score-daily.yml")
    on = wf[True] if True in wf else wf["on"]  # PyYAML quirk: `on` -> True
    crons = [s["cron"] for s in on["schedule"]]
    hours = {c.split()[1] for c in crons}
    assert "9" in hours, f"expected a 09 UTC cron, got {crons}"
    assert "0 9 * * *" not in crons, "cron minute must be off the full hour (:00)"
    assert "0 7 * * *" not in crons, "07:00 UTC cron must be retired (too early)"


def test_score_daily_has_concurrency_guard():
    # Overlapping cron + manual runs must not race on data/scores.parquet.
    wf = _load("score-daily.yml")
    conc = wf["concurrency"]
    assert conc["group"]
    assert conc["cancel-in-progress"] is False


def test_score_daily_workflow_dispatch_accepts_target_date():
    wf = _load("score-daily.yml")
    on = wf[True] if True in wf else wf["on"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert "target_date" in inputs


def test_score_daily_uses_catch_up():
    # The scoring step must pass --catch-up so a skipped cron self-heals.
    body = (WF / "score-daily.yml").read_text()
    assert "--catch-up" in body, (
        "score-daily.yml must run score_day.py with --catch-up so a missed "
        "scheduled run is recovered on the next run."
    )


def test_score_daily_checks_entsoe_revisions():
    # ENTSO-E korrigiert Ist-Lastwerte nachträglich; der Tageslauf muss das
    # rückwärtige Fenster prüfen und abweichende Tage neu bewerten — sonst
    # bleiben gegen implausible Daten benotete Tage für immer falsch.
    body = (WF / "score-daily.yml").read_text()
    assert "revise_scores.py" in body, (
        "score-daily.yml must run scripts/revise_scores.py so that "
        "ENTSO-E load corrections trigger a re-evaluation."
    )


def test_build_and_deploy_has_no_workflow_run_trigger():
    # Race guard (see module docstring). build-and-deploy must NOT rebuild on
    # `workflow_run` of Daily Scoring: that build fires before the bot score-PR
    # is auto-merged (stale, pre-merge data) and races the correct push build on
    # the shared "pages" concurrency group, where one deploy loses with
    # "Deployment failed, try again later" and the stale build can win (observed
    # 2026-07-05). The auto-merge push to data/scores.parquet is the single,
    # race-free trigger — covered by test_build_and_deploy_push_paths_*.
    bd = _load("build-and-deploy.yml")
    bd_on = bd[True] if True in bd else bd["on"]
    assert "workflow_run" not in bd_on, (
        "build-and-deploy.yml must not use a workflow_run trigger; the Pages "
        "rebuild is driven by the auto-merge push to data/scores.parquet. "
        "Re-adding workflow_run reintroduces the stale-deploy race."
    )


def test_build_and_deploy_push_paths_include_scores_parquet():
    bd = _load("build-and-deploy.yml")
    bd_on = bd[True] if True in bd else bd["on"]
    paths = bd_on["push"]["paths"]
    assert "data/scores.parquet" in paths


def test_build_and_deploy_push_paths_include_actuals_and_charts():
    # A merged actuals/chart/dep update must trigger a Pages rebuild, else the
    # forecast-vs-actual chart never refreshes on the published site.
    bd = _load("build-and-deploy.yml")
    bd_on = bd[True] if True in bd else bd["on"]
    paths = bd_on["push"]["paths"]
    for p in ("data/actual_load.parquet", "scripts/charts.py", "uv.lock"):
        assert p in paths, f"{p} missing from build-and-deploy.yml push paths"


def test_build_and_deploy_has_pages_permissions():
    bd = _load("build-and-deploy.yml")
    assert bd["permissions"]["pages"] == "write"
    assert bd["permissions"]["id-token"] == "write"


def test_validate_pr_triggers_on_pull_request():
    wf = _load("validate-pr.yml")
    on = wf[True] if True in wf else wf["on"]
    assert "pull_request" in on


def test_validate_pr_is_not_pull_request_target():
    # Security invariant: untrusted fork code is validated under `pull_request`
    # (no repo secrets exposed). `pull_request_target` would run that code with
    # secrets + a write token in the base-repo context — a known exfil vector.
    wf = _load("validate-pr.yml")
    on = wf[True] if True in wf else wf["on"]
    assert "pull_request_target" not in on


def test_validate_pr_counts_only_new_submission_files():
    body = (WF / "validate-pr.yml").read_text()
    assert "--diff-filter=A" in body
    assert "genau eine neue Submission hinzufügen" in body


def test_ci_workflow_runs_pytest_and_actionlint():
    # The test suite + workflow lint must actually run in CI on every PR.
    body = (WF / "ci.yml").read_text()
    assert "pytest" in body
    assert "actionlint" in body
    ci = _load("ci.yml")
    on = ci[True] if True in ci else ci["on"]
    assert "pull_request" in on


# --------------------------------------------------------------------------
# GitHub App auto-merge migration: SCORE_BOT_TOKEN (a human PAT) is retired
# in favour of a scoped App with short-lived per-run installation tokens.
# --------------------------------------------------------------------------

def test_no_workflow_uses_score_bot_token():
    # The PAT is fully retired — its expiry was the #1 silent-failure mode.
    for wf in WF.glob("*.yml"):
        assert "SCORE_BOT_TOKEN" not in wf.read_text(), (
            f"{wf.name} still references the retired SCORE_BOT_TOKEN PAT."
        )


def test_score_daily_uses_github_app_token():
    body = (WF / "score-daily.yml").read_text()
    assert "actions/create-github-app-token" in body
    # The bot PRs are created + auto-merged with the minted App token.
    assert "steps.app-token.outputs.token" in body


def test_validate_pr_has_no_auto_merge_and_no_secrets():
    # Auto-merge moved to auto-merge.yml; the untrusted pull_request workflow
    # must not enable merges or reference any secret.
    body = (WF / "validate-pr.yml").read_text()
    assert "enable-pull-request-automerge" not in body
    assert "secrets." not in body


def test_auto_merge_workflow_is_secure_workflow_run():
    body = (WF / "auto-merge.yml").read_text()
    am = _load("auto-merge.yml")
    on = am[True] if True in am else am["on"]
    # Triggered by the validation workflow completing (trusted base context),
    # not by pull_request (which would run untrusted fork code with secrets).
    assert "workflow_run" in on
    assert "Validate Submission PR" in on["workflow_run"]["workflows"]
    assert "pull_request" not in on
    # Mints an App token and only merges green PR validations.
    assert "actions/create-github-app-token" in body
    assert "conclusion == 'success'" in body or "conclusion=='success'" in body
    # Hardening: only single-file submission PRs are auto-merged.
    assert "submissions/" in body
