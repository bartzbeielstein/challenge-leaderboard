"""challenge_leaderboard.push — importable git/gh submission PR flow.

Parameterised, unified variants of the per-script push helpers in the
forecasting scripts (e.g. bart26k-lecture/scripts/team4_optuna_submit.py).
All functions are pure-Python and testable without side-effects (the
subprocess-heavy path is isolated in open_submission_pr).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def submission_push_steps(
    *,
    csv_rel: str,
    team_id: str,
    target_date: str,
    now_hhmmss: str,
    repo: str = "bartzbeielstein/challenge-leaderboard",
    pr_body: str,
) -> tuple[str, list[list[str]]]:
    """Return (branch_name, list-of-argv-steps) for a submission PR.

    The branch name follows the convention
    ``submission/<team_id>-<target_date>-<now_hhmmss>``.
    Steps (in order):

    1. ``git switch -c <branch> main``
    2. ``git add <csv_rel>``
    3. ``git commit -m "submission(<team_id>): forecast for <target_date>"``
    4. ``git push -u origin <branch>``
    5. ``gh pr create ...`` with the given *pr_body*

    Parameters
    ----------
    csv_rel:
        Repo-relative path to the submission CSV (e.g.
        ``submissions/team_4/2026-06-16.csv``).
    team_id:
        Registered team identifier.
    target_date:
        ISO date string of the forecast target day (``YYYY-MM-DD``).
    now_hhmmss:
        Current time as ``HHMMSS`` string used in the branch name (caller
        supplies this so the function stays pure and deterministic in tests).
    repo:
        ``owner/repo`` slug for the ``gh pr create --repo`` flag.
    pr_body:
        Markdown body for the pull-request description.

    Returns
    -------
    tuple[str, list[list[str]]]
        ``(branch_name, steps)`` where each step is a list of argv strings.
    """
    branch = f"submission/{team_id}-{target_date}-{now_hhmmss}"
    title = f"submission({team_id}): forecast for {target_date}"
    steps: list[list[str]] = [
        ["git", "switch", "-c", branch, "main"],
        ["git", "add", csv_rel],
        ["git", "commit", "-m", title],
        ["git", "push", "-u", "origin", branch],
        [
            "gh", "pr", "create",
            "--base", "main",
            "--repo", repo,
            "--head", branch,
            "--title", title,
            "--body", pr_body,
        ],
    ]
    return branch, steps


def open_submission_pr(
    *,
    lb_root: Path,
    csv_rel: str,
    team_id: str,
    target_date: str,
    now_hhmmss: str,
    repo: str = "bartzbeielstein/challenge-leaderboard",
    pr_body: str,
) -> int:
    """Run the submission push steps via subprocess (cwd=*lb_root*).

    Returns 0 on success or the non-zero returncode of the first failing step.
    Each command's stdout/stderr is forwarded to the parent process.

    Parameters
    ----------
    lb_root:
        Root of the challenge-leaderboard clone (working directory for git/gh).
    csv_rel, team_id, target_date, now_hhmmss, repo, pr_body:
        Forwarded verbatim to :func:`submission_push_steps`.
    """
    _branch, steps = submission_push_steps(
        csv_rel=csv_rel,
        team_id=team_id,
        target_date=target_date,
        now_hhmmss=now_hhmmss,
        repo=repo,
        pr_body=pr_body,
    )
    for cmd in steps:
        res = subprocess.run(cmd, cwd=lb_root, capture_output=True, text=True)
        if res.stdout.strip():
            print(res.stdout.strip())
        if res.stderr.strip():
            print(res.stderr.strip())
        if res.returncode != 0:
            return res.returncode
    return 0


def print_push_instructions(
    *,
    lb_root: Path,
    csv_rel: str,
    team_id: str,
    target_date: str,
    now_hhmmss: str,
    repo: str = "bartzbeielstein/challenge-leaderboard",
    pr_body: str,
) -> list[str]:
    """Return the human-readable push instruction lines.

    Each element is a shell command string the operator can copy-paste.
    The list starts with ``cd <lb_root>`` so it is self-contained.

    Parameters
    ----------
    lb_root:
        Root of the challenge-leaderboard clone.
    csv_rel, team_id, target_date, now_hhmmss, repo, pr_body:
        Forwarded verbatim to :func:`submission_push_steps`.

    Returns
    -------
    list[str]
        Human-readable command lines (one per step, plus the cd preamble).
    """
    _branch, steps = submission_push_steps(
        csv_rel=csv_rel,
        team_id=team_id,
        target_date=target_date,
        now_hhmmss=now_hhmmss,
        repo=repo,
        pr_body=pr_body,
    )
    lines = [f"cd {lb_root}"]
    for cmd in steps:
        lines.append(" ".join(cmd))
    return lines
