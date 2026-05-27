# Investigation report — challenge-leaderboard CI failures & stale Pages site

**Date:** 2026-05-27
**Investigator:** Claude (Opus 4.7, 1M context) on behalf of @bartzbeielstein
**Branch:** `chore/pipeline-investigation-2026-05-27`
**Live site at start:** only `team_4` with 2 scored days (2026-05-12, 2026-05-13)
**Live site at end:** `neura` (rank 1), `hot_rod` (rank 2), `team_4` (rank 3) — 5 total scored days

---

## TL;DR — Root cause

There was no broken workflow and no broken secret. Two unrelated facts collided:

1. **The pre-LOCF version of `scripts/score_day.py` silently skipped any team
   without an exact submission for the target date.** Most teams submit late,
   so most scheduled runs scored nobody and produced a parquet that was
   byte-identical to `main` → `peter-evans/create-pull-request` reported
   "Branch is not ahead of base", emitted `pull-request-operation=""`, and the
   step `Kein Score, kein PR` ran. The workflow was green on the Actions page
   while no data ever landed. The LOCF fix shipped on **2026-05-26 17:21 UTC**
   (workflow run `26463924432`, "score: LOCF für fehlende Submissions").
2. **The GitHub-Actions scheduled cron is unreliable.** The
   `0 7 * * *` trigger fired **75 minutes late today** (`08:15:35 UTC` instead
   of `07:00 UTC`, run `26499428631`). This is well-documented behaviour for
   `schedule:` workflows during high load and is the reason the site looked
   "stale" when the investigation started — yesterday's data was simply not
   scored yet.

When today's cron finally fired, with the **post-LOCF** code, it scored
`team_4`, `hot_rod`, `neura` for 2026-05-26 in one shot and the build/deploy
chain published the new leaderboard within a minute. No fix needed.

| Run | Time (UTC) | Conclusion | What landed |
|---|---|---|---|
| `26499428631` (scheduled) | 2026-05-27T08:15:35Z | success | 3 rows for 2026-05-26 |
| PR `#40` (`bot/scores-2026-05-26`) | 2026-05-27T08:16:11Z | merged to main | auto-merge via SCORE_BOT_TOKEN |
| `26499464593` (build-and-deploy) | 2026-05-27T08:16:21Z | success | live site refreshed |

---

## Evidence — verbatim quotes

### Pre-LOCF failure mode (the one that stalled the leaderboard)

Run `26440402613`, scheduled 2026-05-26T08:09:15Z, target date 2026-05-25:

```
[score_day] Lade Ground-Truth für 2026-05-25 …
[score_day] Keine Submissions für 2026-05-25 — fertig.
```

then in the `Score-Stand-PR öffnen` step:

```
Branch 'bot/scores-2026-05-25' is not ahead of base 'main' and will not be created
```

…followed by the misleadingly-green step `Kein Score, kein PR`:

```
[score-daily] Keine neuen Scores für 2026-05-25 (oder identisch zu HEAD).
```

The phrase `Keine Submissions für 2026-05-25 — fertig` is NOT in the current
`scripts/score_day.py`. The current message is
`Keine bewertbaren Prognosen für {target_date} — fertig.` (line 182). That
divergence is what dated the bug: the run executed code from before commit
`26463924432` (2026-05-26 17:21 UTC).

### Post-LOCF success (today)

Run `26499428631`, scheduled 2026-05-27T08:15:35Z, target date 2026-05-26:

```
[score_day] Lade Ground-Truth für 2026-05-26 …
[score_day] hot_rod: MAE=1961.51 MW RMSE=2236.25 MAPE=3.77%
[score_day] neura:   MAE=1707.30 MW RMSE=2217.33 MAPE=3.70%
[score_day] team_4:  MAE=3466.19 MW RMSE=4090.82 MAPE=6.74%
[score_day] 3 Zeilen in data/scores.parquet geschrieben.
```

### Other clean signals

- `gh api repos/bartzbeielstein/challenge-leaderboard/actions/workflows` →
  all three workflows have `"state": "active"`. None auto-disabled.
- `gh secret list -R bartzbeielstein/challenge-leaderboard` →
  `ENTSOE_API_KEY` and `SCORE_BOT_TOKEN` are both present (latest rotation
  2026-05-25T18:48Z for the bot token).
- `gh api repos/bartzbeielstein/challenge-leaderboard/pages` →
  `"status": "built"`, `"build_type": "workflow"`, public, HTTPS enforced.
- `build-and-deploy.yml`'s `workflow_run.workflows: ["Daily Scoring"]`
  exactly matches `score-daily.yml`'s `name: Daily Scoring`. No silent
  desync of the auto-rebuild chain (tested in `tests/test_workflows_yaml.py`).

---

## Probes — live PR experiments

All six probes targeted a throwaway base branch `test/pipeline-probe-2026-05-27`
forked from `main`. The base branch and every `probe/*` head were deleted at
the end of the investigation.

| # | Probe | Run ID | Conclusion | Validator output |
|---|---|---|---|---|
| 1 | 23-row CSV | `26499094903` | failure (exit 1) | `ERROR: 24 Zeilen erwartet, aber 23 gefunden` |
| 2 | CSV for today, past D-1 23:59 Berlin | `26499158678` | failure (exit 2) | `ERROR: Deadline 2026-05-26T23:59:00+02:00 überschritten (jetzt 2026-05-27T08:10:01...+00:00)` |
| 3 | bartzbeielstein submitting to `a_team` | `26499208542` | failure (exit 3) | `ERROR: PR-Autor 'bartzbeielstein' nicht in github_handles für Team 'a_team': ['obecher', 'math1s0', 'jannhth', 'markdt551', 'kradid655']` |
| 4 | valid CSV for 2026-05-29 | `26499255487` | success (exit 0) | `OK: team=team_4 target_date=2026-05-29 file=submissions/team_4/2026-05-29.csv` — PR auto-merged into test branch |
| 5 | `score-daily.yml` dispatch on test branch for 2026-05-13 | `26499343288` | success | `1 Zeilen in data/scores.parquet geschrieben`, PR `#39` auto-merged into test branch |
| 6 | `build_leaderboard.py` run locally against probe-state parquet | n/a (local) | success | byte-identical to live `https://bartzbeielstein.github.io/challenge-leaderboard/data/scores.json` |

Probe 4's PR auto-merged into the test branch because `validate` succeeded and
the test branch had no branch-protection rules. That was acceptable — the test
branch and all probe heads were force-deleted from `origin` at the end of the
investigation. Closing PRs: `#35`, `#36`, `#37` closed; `#38`, `#39` merged
into the deleted test branch.

---

## Pytest suite — `tests/`

Hermetic, network-free, runs in ~80 ms on a clean checkout:

```
$ uv run pytest -q
............................                                             [100%]
28 passed in 0.08s
```

| File | What it pins |
|---|---|
| `tests/test_validate_submission.py` | path parsing, schema (24 rows, columns, NaN, negative MW), deadline (before/after D-1 23:59 Berlin), authorship (unknown team, wrong user, case-insensitive match) |
| `tests/test_score_day.py` | LOCF: exact match preferred; LOCF chooses most recent prior; future-only submissions skipped; full `main()` writes parquet rows with expected metrics; idempotence over re-runs; empty-submissions case |
| `tests/test_build_leaderboard.py` | rank order (mean MAE asc, n_submissions desc, tie-breaks), HTML + JSON outputs, empty-scores case |
| `tests/test_workflows_yaml.py` | cron `0 7 * * *`, workflow_dispatch input `target_date`, `build-and-deploy.yml` `workflow_run.workflows` exactly matches `score-daily.yml`'s `name:`, `paths: data/scores.parquet`, `pages: write` + `id-token: write` permissions, `validate-pr.yml` triggers on `pull_request` |

The `test_score_day.py` cases pin the **post-LOCF** semantics specifically, so
a regression to the pre-2026-05-26-17:21 behaviour (silent skip of any team
without an exact submission) cannot recur without a test failure.

The branch `chore/pipeline-investigation-2026-05-27` carries the suite as a
single commit `0dea0ea`. Open a PR against `main` whenever convenient.

---

## What this investigation did NOT fix

- **GitHub-Actions cron unreliability.** Today's run was 75 minutes late.
  This is platform behaviour, not a bug in this repo. If reliability of
  the daily scoring matters (e.g. for academic-deadline credibility), one
  option is a small external scheduler that calls
  `gh workflow run score-daily.yml` at exactly 07:00 UTC.
- **Late submissions still get LOCF-skipped if no prior submission exists.**
  `eigen_squad` has only `2026-05-27.csv`; the 2026-05-26 scoring run
  legitimately did not include them (no prior date to carry forward).
  This is by design.
- **`bot/scores-2026-05-12` is a leftover branch from a past scoring run
  that was force-deleted before its PR could merge.** Not actionable —
  next successful scoring of a 2026-05-12-adjacent date will replace it
  via `delete-branch: true`. Leaving it alone.
