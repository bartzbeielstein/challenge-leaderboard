# Model/Method Card: Voltrion CR-2 Backtest Submitter

This card describes what the Voltrion forecast submitter (`make_submission.py`) is, how to use it safely, the conditions under which its results are valid, and the responsibilities it places on anyone who runs it. It follows the [Hugging Face Model Card Guidebook](https://huggingface.co/docs/hub/model-card-guidebook) taxonomy, mirroring the structure of `MODEL_CARD.md` in this repository.

## 1. Model Details

| Field | Value |
| --- | --- |
| Name | Voltrion CR-2 Backtest Submitter (`voltrion_cr2_backtest_submit`) |
| Version | Unversioned internal script; not packaged or released. Identified per run by the SHA-256 of `make_submission.py` and the `uv.lock`/`pyproject.toml` hashes recorded in each run's `runinfo_<team>_<date>.json` (see Section 4). |
| Type | Deterministic Python command-line pipeline for day-ahead (24 h, hourly) German grid-load forecasting. Trains its own small model ensemble on every invocation; performs no persisted pretraining. |
| Developed by | Team Voltrion — GitHub handles `Malakamz`, `esmahxhj`,`Malekuti`, `DarkReaperGT` (`teams.yml`) |
| Distributed by | Not distributed as a package. Lives at the root of the team's fork of the course repository, `DarkReaperGT/challenge-leaderboard` (fork of `bartzbeielstein/challenge-leaderboard`). |
| Language | Python 3.13 or newer (`pyproject.toml`: `requires-python = ">=3.13"`) |
| License | None declared. No `LICENSE` file exists in this repository; the script is a course submission artifact, not a licensed release. |
| Repository | `https://github.com/DarkReaperGT/challenge-leaderboard` (team fork); upstream course repo `https://github.com/bartzbeielstein/challenge-leaderboard` |
| Technical report | None. The course rules ("Challenge Rules" CR-1..CR-4) are documented in `README.md` and `DEPLOYMENT.md` of this repository and in `lecture/12_challenge.qmd` of the course materials, not shipped with this script. |

The script depends on `spotforecast2-safe` (course-provided data access layer), `pandas`, `numpy`, `scikit-learn`, and optionally `lightgbm` (falls back to `HistGradientBoostingRegressor` if LightGBM is unavailable). It has no CPE identifiers and is not tracked in any SBOM or vulnerability-tracking system, since it is an unpublished, single-team course script rather than a distributed package.

The script itself is a low-risk, fully inspectable component: it is deterministic under CR-2, fails loudly (`Abort` exceptions) on invalid or stale data, and never reads the ENTSO-E day-ahead forecast that the challenge explicitly forbids as an input. It is not a certified or regulated system; it is coursework for the *SoSe 2026 Numerische Mathematik / DDMO* Live-Lastprognose-Challenge at TH Köln.

Responsibilities are divided as follows.

| Responsibility | Party | Contact |
| --- | --- | --- |
| Script development and maintenance | Team Voltrion | GitHub `DarkReaperGT` (gongtamik@gmail.com), `Malakamz`, `esmahxhj`,`Malekuti` |
| Distribution | None (private/team fork, not published) | — |
| Deployment, daily submission, and audit | Team Voltrion (each `--target-date` run) | team fork issue tracker / course instructor |
| Scoring and leaderboard operation | Course maintainer (`bartzbeielstein/challenge-leaderboard`) | upstream repo issue tracker |

There is no formal release history. Each day's submission and its accompanying `runinfo_<team>_<date>.json`, snapshot directory, and audit log under `snapshots/voltrion/<date>/` and `_cache/voltrion_cr2_backtest/<date>/` constitute the versioned record of that day's run, together with the untracked `make_submission.py` used to produce it (see `git status` — the script is intentionally kept out of the tracked history of the shared PR branch).

## 2. Intended Use and Scope

`make_submission.py` produces one 24-hour, hourly forecast of German (`DE`) grid load (MW) per day, for submission to the course's daily leaderboard challenge via `submissions/voltrion/<YYYY-MM-DD>.csv`. It reads only ENTSO-E *Actual Total Load* as its historical target; it deliberately never reads, anchors on, or calibrates against the ENTSO-E day-ahead forecast, since that is disallowed by the challenge rules. It optionally augments its features with Open-Meteo weather for five German population/industry centers.

The script feeds its own bundled model set — a median-optimized LightGBM (or HistGradientBoosting fallback), a mean-optimized HistGradientBoosting and ExtraTrees "control" pair, a Ridge-regression anchor, and three non-parametric baseline profiles (weekly persistence, weekday-hour profile, recent-level profile) — through a backtest-validated weighted ensemble, a learned bias correction, and symmetric ramp/energy guards.

The package has clear limits. It is hardcoded to `COUNTRY_CODE = "DE"` and a fixed 24-hour horizon; it is not a general multi-region or multi-horizon forecasting tool. It does not persist or reuse trained models between runs — every invocation retrains from scratch on the available history. It does not tune hyperparameters; the model configurations in `make_model_set()` are fixed constants chosen ahead of time, not searched per run. It does not silently repair bad input: out-of-range load values, stale data, or leaked target-day data raise an `Abort` rather than being used.

## 3. How to Get Started

```powershell
cd "C:\Users\gongt\Documents\challenge-leaderboard\challenge-leaderboard"
$env:ENTSOE_API_KEY = "DEIN_TOKEN"
$env:PYTHONHASHSEED = "0"
$env:OMP_NUM_THREADS = "1"
$env:MKL_NUM_THREADS = "1"
$env:OPENBLAS_NUM_THREADS = "1"
$env:NUMEXPR_NUM_THREADS = "1"

uv run python make_submission.py --team voltrion --target-date 2026-07-03
```

Without weather features:

```powershell
uv run python make_submission.py --team voltrion --target-date 2026-07-03 --weather-mode off
```

Using only the already-cached ENTSO-E data (no live download):

```powershell
uv run python make_submission.py --team voltrion --target-date 2026-07-03 --skip-download
```

`PYTHONHASHSEED` only takes effect if set before the Python process starts; if it is missing or wrong, the script re-executes itself once automatically with `PYTHONHASHSEED=0` and the thread-count environment variables fixed to `1` (see `_bootstrap_cr2_environment()`, `make_submission.py:98-138`).

## 4. Technical Specification

### Task and model family

The script performs recursive, one-day-ahead (24-step, hourly) univariate load forecasting from the series' own lags, rolling-window statistics, calendar features, and optional weather. It is not a single model but a small heterogeneous ensemble whose component weights and bias correction are re-estimated from a rolling historical backtest on every run.

### Architecture

The file is organized into eleven numbered sections (as commented in the source):

1. **Konfiguration** — constants: country, thresholds, guard bands, weather locations (`make_submission.py:164-233`).
2. **Abort, Logging, Dateien, CR-2** — controlled-abort exception, dual console/file logging, hashing, JSON/CSV writers, CR-2 environment assertions (`:240-461`).
3. **ENTSO-E Actual Load laden** — downloads or reuses cached *Actual Total Load*; explicitly ignores any day-ahead/forecast column (`:463-540`).
4. **Coverage, Lücken, Cleaning** — gap detection, sanity-range filtering (20,000–95,000 MW), interpolation, and history-based synthetic fill for remaining gaps; hard failure if NaNs or leakage remain (`:543-702`).
5. **Kalender + Wetterfeatures** — cyclic calendar encodings, a fixed German holiday/Easter calculation, and weighted Open-Meteo pulls for Köln, München, Stuttgart, Hannover, and Frankfurt (`:705-953`).
6. **Features für Modelle** — lag list (`1..24`, plus `48,72,...,672` hours) and rolling-window features, both vectorized (training) and single-timestep (recursive inference) variants (`:956-1035`).
7. **Modelle** — `make_model_set()` builds `lgbm_main` (or `histgb_main` fallback), `histgb_control`, `extra_trees_control`, `ridge_linear` (`:1038-1118`).
8. **Backtest-Kalibrierung** — retrains the model set on truncated history, backtests over the last N days (default 21, minimum 7), derives per-component MAE-based ensemble weights and an hourly or Ridge-regression residual bias correction (`:1252-1459`).
9. **Finalisierung, Guards, Diagnose** — applies bias correction, a symmetric ramp limit (6,000 MW/h), a workday/weekend daily-energy guard, and a shape-diagnosis pass that flags low correlation or amplitude versus historical profiles (`:1461-1632`).
10. **Submission, Validierung, Git** — writes the 24-row `timestamp_utc,forecast_mw` CSV, validates its contract, and optionally stages/commits/pushes it plus snapshots (`:1634-1736`).
11. **Orchestrierung + CLI** — `make_submission()` ties the stages together and writes `runinfo_<team>_<date>.json`; `parse_args()`/`main()` provide the CLI entry point (`:1738-2033`).

### Mathematical description

For the cleaned actual-load series $y = \{y_1, \ldots, y_T\}$, each of the four fitted components predicts $\hat y_t$ recursively: at each of the 24 target hours, a feature row of lags $\{y_{t-\ell} : \ell \in \{1,\ldots,24,48,72,\ldots,672\}\}$, rolling statistics over the trailing 24/168/672 hours, and calendar/weather features is built from already-known or already-predicted values, and the model output is appended to the history before the next hour is predicted. The three profile baselines are non-parametric medians over comparable historical hours (same weekday+hour, or same hour) rather than fitted models.

The final forecast is

$$\hat y_{\text{final}} = \mathrm{guard}\big(\mathrm{ramp}\big(\mathrm{bias}\big(\textstyle\sum_i w_i \hat y_i\big)\big)\big),$$

where the weights $w_i$ come from an inverse-MAE power law over the backtest window ($w_i \propto \mathrm{MAE}_i^{-6}$, floored at 3% per profile component; `WEIGHT_POWER = 6.0`, `MIN_COMPONENT_WEIGHT = 0.03`), `bias` is either a Ridge regression on backtest residuals (context-dependent) or, with too little backtest data, an hourly median residual table, and `ramp`/`guard` are the symmetric bounding steps described in Section 9 above.

### Training

The script trains its full model set from scratch on every invocation — there are no persisted weights, and nothing is cached between days beyond the ENTSO-E/weather data caches. Two training passes happen per run: one on history truncated at the start of the backtest window (`compute_backtest_calibration`, for weight/bias estimation) and one, with identical hyperparameters, on the full allowed history up to `target − data_delay_hours` (`train_models(label="final")`). Default history depth is 3 years (`TRAIN_YEARS = 3`) plus a 45-day download margin.

The `lgbm_main` configuration (used when LightGBM is importable) is:

| Hyperparameter | Value |
| --- | --- |
| n_estimators | 420 |
| learning_rate | 0.032 |
| num_leaves | 31 |
| subsample | 0.90 |
| colsample_bytree | 0.90 |
| objective | regression_l1 (median) |
| random_state | 2026 |
| deterministic | True |
| force_col_wise | True |
| num_threads | 1 |

`histgb_control` (`HistGradientBoostingRegressor`, mean-objective) uses `max_iter=300`, `learning_rate=0.05`, `max_leaf_nodes=31`, `l2_regularization=0.08`. `extra_trees_control` uses `n_estimators=260`, `min_samples_leaf=2`, `max_features=0.75`, `n_jobs=1`. `ridge_linear` is a `StandardScaler` + `Ridge(alpha=5.0)` pipeline. All four share `random_state = RANDOM_STATE = 2026`. These are the script's fixed operating values, not tuned per run — hyperparameter search is explicitly out of scope (Section 2).

### Design objectives

Four properties are enforced by construction. The pipeline is **CR-2 deterministic**: it re-execs itself to force `PYTHONHASHSEED=0`, pins BLAS/OpenMP/NumExpr threads to 1, and trains LightGBM/ExtraTrees single-threaded with fixed seeds. It is **leakage-free**: `assert_no_target_leakage` aborts if any actual-load value at or after the target day is present, and the day-ahead forecast column is never read at all (`ignored_note` in `entsoe_actual_meta`). It is **fail-safe**: cleaning, coverage, and submission-contract checks raise `Abort` with a distinct exit code rather than emitting a silently-wrong forecast. It is **bias-symmetric by design**: guard bands (ramp limit, energy-scale bounds) are chosen symmetric around 1.0 specifically to avoid a systematic over- or under-prediction skew (see the inline rationale at `make_submission.py:196-205`).

## 5. Interfaces and Runtime

**Inputs**: ENTSO-E *Actual Total Load* for `DE`, fetched via `spotforecast2_safe.downloader.entsoe.download_new_data` (requires `ENTSOE_API_KEY`) or reused from cache with `--skip-download`; optionally, Open-Meteo archive/forecast hourly weather for five weighted locations (`--weather-mode auto`, the default, with automatic fallback to calendar-only features if the API call fails).

**Outputs**, all under the repository root:

| Artifact | Path |
| --- | --- |
| Submission CSV (24 rows, columns `timestamp_utc`, `forecast_mw`) | `submissions/<team>/<target-date>.csv` |
| Per-hour diagnostics (components, weights, bias) | `_cache/voltrion_cr2_backtest/<date>/tables/own_model_diagnostics.csv` |
| Backtest diagnostics | `_cache/voltrion_cr2_backtest/<date>/tables/backtest_calibration_diagnostics.csv` |
| Run metadata (env, hashes, guard decisions, safety notes) | `_cache/voltrion_cr2_backtest/<date>/runinfo_<team>_<date>.json` |
| Audit log | `_cache/voltrion_cr2_backtest/<date>/audit/audit_<team>_<date>.log` |
| Snapshots (copies of the above plus raw ENTSO-E/weather pulls) | `snapshots/<team>/<date>/` |

**CLI arguments** (`parse_args`, `make_submission.py:1994-2013`):

| Flag | Default | Purpose |
| --- | --- | --- |
| `--team` | `voltrion` | Team ID; must match `^[a-z0-9_]+$` |
| `--target-date` | tomorrow (UTC) | `YYYY-MM-DD` or `auto` |
| `--repo-root` | current directory | Must contain a `submissions/` folder |
| `--train-years` | 3 | History depth for training/download |
| `--start-margin-days` | 45 | Extra download buffer before train start |
| `--data-delay-hours` | 3 | Safety margin before the target day |
| `--backtest-days` | 21 | Days used for weight/bias calibration (min. 7) |
| `--weather-mode` | `auto` | `auto` or `off` |
| `--skip-download` | off | Reuse existing ENTSO-E cache |
| `--allow-non-cr2` | off | Disable the hard `PYTHONHASHSEED`/thread guards |
| `--no-validate` | off | Skip the standalone CSV validator |
| `--push` | off | `git add`/`commit`/`push` the CSV and snapshots automatically |
| `-v`/`-vv` | off | More verbose / debug logging |

**Runtime**: CPU-only, Python 3.13+, single-threaded by design for reproducibility (`OMP_NUM_THREADS=MKL_NUM_THREADS=OPENBLAS_NUM_THREADS=NUMEXPR_NUM_THREADS=VECLIB_MAXIMUM_THREADS=1`). No GPU code. A full run (download, cleaning, backtest retraining, final training, prediction) typically involves two full model-training passes over up to ~3 years of hourly data (≈26,000 rows) plus up to 21 recursive 24-hour backtest predictions.

Runtime dependencies (from `pyproject.toml` plus the script's direct imports):

| Dependency | Role |
| --- | --- |
| `spotforecast2-safe` (>=3.0.0,<4) | ENTSO-E download/cache access (course-provided) |
| `pandas` (>=2.2) | Time series handling |
| `numpy` | Numerics |
| `scikit-learn` | `HistGradientBoostingRegressor`, `ExtraTreesRegressor`, `Ridge`, `StandardScaler`, `Pipeline` |
| `lightgbm` | `LGBMRegressor` (optional; falls back to `HistGradientBoostingRegressor` if missing) |

No license inventory is maintained for this script, since it is an internal course tool rather than a distributed artifact; licenses of the dependencies above follow their respective upstream projects.

## 6. Data and Operational Design Domain

Production data comes exclusively from live ENTSO-E and Open-Meteo API calls at run time; the script ships no bundled fixtures or pretrained weights. Every run's raw and cleaned inputs are snapshotted (with SHA-256 hashes) under `snapshots/<team>/<date>/` for later inspection, and the audit log records the pipeline's decisions step by step.

The Operational Design Domain (ODD) is the set of conditions under which the script's output is valid. Outside these conditions it raises an `Abort` (with a distinct process exit code) instead of returning an unreliable forecast.

| Condition | Valid range | Outside the range |
| --- | --- | --- |
| Region | `DE` (hardcoded `COUNTRY_CODE`) | not supported |
| Forecast horizon | fixed 24 hours, hourly, from UTC midnight of `--target-date` | not supported |
| ENTSO-E actual-load freshness | last valid hour within `MAX_ACTUAL_LAG_HOURS = 36` h of the target start | `Abort(1)` |
| Data gaps after cleaning | none permitted in the 28-day scan window | `Abort(1)` |
| Reasonable load range | 20,000–95,000 MW | value marked NaN, then cleaned/imputed or aborted |
| Target-day leakage | no actual-load value at or after the target day | `Abort(1)` ("DATA LEAKAGE") |
| Minimum training samples | ≥ 1,000 rows after lag/rolling feature construction | `Abort(1)` |
| Backtest depth for learned bias model | ≥ `24 × MIN_BACKTEST_DAYS = 168` residual points | falls back to hourly-median bias table |
| Submission contract | exactly 24 rows, columns `timestamp_utc`, `forecast_mw`, no NaN/Inf/negative values | `Abort(4)` |
| ENTSO-E day-ahead forecast | never read | not applicable by construction |

Forecast quality is bounded by how well the ensemble's components and the backtest-derived weights generalize; sudden regime changes (e.g., unseasonal weather, grid events) outside the training distribution degrade accuracy even though the pipeline's own guards continue to run correctly. The symmetric ramp (6,000 MW/h) and daily-energy guards (workday 0.94–1.06×, weekend/holiday 0.92–1.08× of a reference-profile mean) exist specifically to bound how far a single bad component or backtest window can pull the final forecast, but they are heuristics tuned on this team's own backtest history, not a formal accuracy guarantee.

## 7. Evaluation

The script performs its own internal backtest on every run rather than shipping a fixed offline evaluation. `compute_backtest_calibration` reruns the full model set over the trailing `--backtest-days` (default 21, minimum 7) days and records, per run, in `runinfo_<team>_<date>.json`:

- `mae_by_component`: MAE in MW of each of the seven ensemble components (four fitted models, three profile baselines) over the backtest window.
- `weighted_backtest_mae_mw` / `weighted_backtest_bias_mw`: MAE and signed bias of the weighted ensemble before the final bias correction.
- `bias_by_hour_mw`: the learned per-hour correction actually applied (or a note that the Ridge residual model was used instead).

These numbers are recomputed from live data on every invocation and therefore vary run to run; no fixed number is reported in this card. The authoritative, externally verifiable evaluation is the course's own daily scoring pipeline (`score_day.py`, described in this repository's `README.md` under "Score-Logik"): primary metric is MAE over the 24 target hours, aggregated as the mean daily MAE across all scored days, with RMSE, MAPE, signed Mean Bias, and Under-Prediction Rate (UPR) reported as secondary/display metrics. Team Voltrion's historical daily submissions are in `submissions/voltrion/*.csv`; the public leaderboard result is at `https://bartzbeielstein.github.io/challenge-leaderboard/`.

Before every write, `assert_submission_contract` and `validate_submission` independently re-check the output CSV (exact columns, 24 rows, no NaN/Inf/negative values, strictly hourly consecutive timestamps) and abort with exit code 4 if either check fails.

## 8. Model Transparency

The script produces point forecasts only (a single `forecast_mw` per hour); it does not natively produce prediction intervals or calibrated uncertainty. Any interval estimate would have to be added on top, outside this script.

The code is white-box: there is no compiled inference kernel and no opaque persisted model — every run retrains from source, and the full transformation from raw ENTSO-E data to final forecast is readable in `make_submission.py`. Per-component contributions are directly inspectable: `own_model_diagnostics.csv` records each component's raw prediction and its final ensemble weight for every target hour, and `runinfo_<team>_<date>.json` records the backtest MAE that produced those weights. Feature-level attribution (e.g., LightGBM split/gain importance) is available through the underlying `scikit-learn`/`lightgbm` model objects but is not extracted or persisted by the script itself.

## 9. Operation: Monitoring and Response

Each run writes a timestamped audit log (console + `_cache/voltrion_cr2_backtest/<date>/audit/audit_<team>_<date>.log`) covering data cleaning results, coverage-guard checks, backtest calibration, and any triggered guard. Built-in monitoring signals include:

- **Data freshness**: `Abort(1)` if the last valid ENTSO-E actual-load hour is more than 36 h before the target day.
- **Data quality**: sanity-range filtering (20,000–95,000 MW), gap-length checks over a rolling 28-day window, and a warning (not an abort) if the largest cleaned hourly step exceeds 8,000 MW.
- **Forecast shape**: `diagnose_forecast_shape` warns (does not abort) if the forecast correlates weakly with the historical weekday-hour profile, if its daily amplitude is much flatter than that profile, if the daily range exceeds 18,000 MW, or if any hourly ramp exceeds the 6,000 MW/h limit.
- **Guard activation**: the daily-energy guard's activation, reason, and applied scale factor are logged and recorded in `finalization_info.energy_guard` whenever it fires.

There is no automated retraining trigger or alerting integration; the script is run manually (or via a scheduled task the team controls) once per target day, and a human reviews the printed summary, the audit log, or `runinfo_<team>_<date>.json` before deciding whether to `--push` the submission. On any `Abort`, the process exits with a non-zero, condition-specific code and writes no submission file for that day.

## 10. Compliance Support

This script is coursework, not a certified or regulated product, and this card makes no EU AI Act, ISO 26262, or IEC 61508 compliance claims on its behalf — those frameworks are not applicable to a single-team academic submission tool.

What it does implement is the course's own reproducibility convention, "CR-2" (bitwise-reproducible runs), by construction: forced `PYTHONHASHSEED=0`, single-threaded BLAS/OpenMP/NumExpr execution, fixed random seeds, and deterministic LightGBM flags (`deterministic=True`, `force_col_wise=True`). Every run's environment (Python version, interpreter path, platform, thread-env variables, `uv.lock`/`pyproject.toml` hashes) and every intermediate artifact's SHA-256 hash are captured in `runinfo_<team>_<date>.json`, which is the closest analogue this script has to an audit trail. The course's other rules referenced in this repository's `README.md` and `DEPLOYMENT.md` — e.g. CR-3 ("clean deferral" on incomplete ground truth) — are properties of the scoring pipeline, not of this submission script.

## 11. Glossary

| Term | Meaning |
| --- | --- |
| CR-2 | This course's "Challenge Rule" requiring bitwise-reproducible submission runs (fixed seeds, single-threaded numeric libraries, `PYTHONHASHSEED=0`); see `README.md`. |
| ENTSO-E | European Network of Transmission System Operators for Electricity; publishes *Actual Total Load* and day-ahead forecasts via its Transparency Platform. |
| MAE | Mean Absolute Error; the leaderboard's primary scoring metric. |
| LOCF | Last Observation Carried Forward; how the leaderboard scores a team that misses a submission day. |
| UPR | Under-Prediction Rate; share of hours where the forecast was below actual load. |
| Open-Meteo | Free weather API used here (archive endpoint for training, forecast endpoint for the target day). |

## 12. How to Audit

An auditor (or teammate) can validate a given day's Voltrion submission as follows.

1. Open `snapshots/voltrion/<date>/runinfo_voltrion_<date>.json` and confirm `cr2_info.strict_cr2` is `true` and `safety_notes.entsoe_day_ahead_forecast_used` is `false`.
2. Re-hash `submissions/voltrion/<date>.csv` and compare it against `output_sha256` in the same `runinfo` file.
3. Inspect `snapshots/voltrion/<date>/audit_voltrion_<date>.log` (or the `_cache/.../audit/` copy) for cleaning, coverage, and guard warnings raised during that run.
4. Check `own_model_diagnostics.csv` and `backtest_calibration_diagnostics.csv` in the same snapshot folder to see each component's raw prediction, its ensemble weight, and the backtest MAE that produced it.
5. Confirm the committing GitHub handle is authorized for `voltrion` in `teams.yml` (`Malakamz`, `esmahxhj`, `Malekuti`, `DarkReaperGT`).
6. Re-run locally with `--skip-download --no-validate` disabled (i.e. the default validator on) against the same cached data to confirm the CSV contract checks in `validate_submission` still pass.

## 13. Citation, Authors, and Contact

Maintained by Team Voltrion for the SoSe 2026 *Numerische Mathematik / DDMO* Live-Lastprognose-Challenge (TH Köln, Bartz-Beielstein). GitHub handles: `Malakamz`, `esmahxhj`, `Malekuti`, `DarkReaperGT`. Repository contact: `DarkReaperGT` (gongtamik@gmail.com).

There is no formal citation target; this is unpublished coursework. If a reference is needed, cite the repository and commit:

```bibtex
@misc{voltrion2026
  author       = {{Team Voltrion (Malakamz, esmahxhj, Malekuti, DarkReaperGT)}},
  title        = {{Voltrion CR-2 Backtest Submitter for the SoSe26 Lastprognose-Challenge}},
  year         = {2026},
  howpublished = {\url{https://github.com/DarkReaperGT/challenge-leaderboard}},
  note         = {Course submission script, TH K\"oln, unlicensed}
}
```

The course's scoring methodology and rules are documented in `lecture/12_challenge.qmd` (course materials, not part of this repository) and summarized in this repository's `README.md`.

## 14. Disclaimer and Liability

**Limitation of liability.** This script is a course submission tool built for one team's participation in the SoSe26 Live-Lastprognose-Challenge. It is provided as-is, with no warranty of forecast accuracy or fitness for any purpose beyond that challenge. Its authors accept no liability for decisions made based on its forecasts outside the course context.

It is not a certified, production, or safety-critical system, and none of the compliance language in `MODEL_CARD.md` (which documents the unrelated `spotforecast2-safe` library) applies to this script or to Team Voltrion. Responsibility for each submitted forecast's correctness under the course's own rules rests with Team Voltrion, per `teams.yml`; responsibility for scoring and leaderboard operation rests with the course maintainer.
