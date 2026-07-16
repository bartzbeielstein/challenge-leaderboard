# Model/Method Card: Team Weather Report Day-Ahead Load Forecast

This card documents the forecasting system used by **Team Weather Report** in
the DDMO SoSe 2026 ENTSO-E day-ahead load forecasting challenge. It describes
the production method, the conditions under which its results are meaningful,
its measured performance across the full season, its known reproducibility
limitations, and the responsibilities of the team operating it. The structure
follows the [Hugging Face Model Card Guidebook](https://huggingface.co/docs/hub/model-card-guidebook)
taxonomy used by the course template (`spotforecast2-safe`'s own model card).

## 1. Model Details

| Field | Value |
| --- | --- |
| Name | Team Weather Report Day-Ahead Load Forecast |
| Version | Live production configuration as of source commit `fd9a1e3` (2026-07-10) |
| Type | Recursive multi-step time-series forecast using a LightGBM regressor and SpotOptim hyperparameter search |
| Developed by | Team Weather Report (`LaloMohamad`, `yanickfotsing`) |
| Distributed by | Team Weather Report through the DDMO challenge repository and reproducibility package |
| Language | Python 3.14 |
| License | No separate license is asserted by Team Weather Report; course-derived source and dependencies retain their original licenses |
| Repository | <https://github.com/bartzbeielstein/challenge-leaderboard>, `submissions/team_weather_report/` |
| Technical report | This model card |

Deployment-specific details are separated from the template metadata above:

| Deployment field | Value |
| --- | --- |
| Target | German (DE) ENTSO-E bidding-zone total electricity load |
| Resolution and horizon | 24 hourly point forecasts in MW, from 00:00 to 23:00 UTC of the target day |
| Main software | `spotforecast2-safe==22.10.1`, `spotforecast2==10.0.0`, `spotoptim==1.0.2`, `lightgbm==4.6.0` |
| Source pipeline | `scripts/team_weather_report_submit.py` (live) and `scripts/team_weather_report_backtest.py` (experimentation), source commit `fd9a1e3af2c7d24dab531ba106e1d35adac3b3a8` |
| Automated scheduling | `scripts/nightly_submit.sh` via macOS `launchd`, two scheduled attempts nightly (21:00 and 23:30 CEST), PID-locked against overlapping runs |
| Source repository | `bart26l-vorlesung` (RWTH Aachen course repository) |

The production system is the aggregate-Germany **`rich`** variant: full 15-city
population-weighted weather features, degree-hours, apparent temperature, and
PACF-informed lag selection. It does **not** use ENTSO-E `Forecasted Load` or
a derived net-load value as a model input — enforced by a post-training
leakage guard that aborts the run if that column ever reaches the model,
exogenous set, or fitted feature names. ENTSO-E's own day-ahead forecast is
used only as a post-training shape-plausibility reference.

The forecasting script builds on course-derived software (`spotforecast2-safe`,
`spotforecast2`). This card does not grant a new license for that source code;
reuse remains subject to the licenses and terms of the original repositories
and packaged dependencies (both AGPL-3.0-or-later).

| Responsibility | Party | Contact |
| --- | --- | --- |
| Daily configuration, execution, validation, and submission | Team Weather Report | GitHub: `LaloMohamad`, `yanickfotsing` |
| Course framework and reference pipeline | DDMO teaching team | Course communication channels |
| Load, renewable, and price data | ENTSO-E Transparency Platform | Provider documentation |
| Weather data | Open-Meteo | Provider documentation |
| Approval of use outside the challenge | Prospective system integrator | Not delegated to the teaching team or data providers |

## 2. Intended Use and Scope

The intended use is the DDMO SoSe 2026 challenge: generate one forecast on day
`D-1` for each of the 24 UTC hours of target day `D`, submitted as a pull
request against the shared leaderboard repository. The method also supports
pseudo-live historical backtests and controlled comparisons of features,
hyperparameter bounds, and lag pools — every production change this season
was evaluated this way before adoption (Section 7).

The output is suitable for educational model comparison under the challenge
scoring rules. It is **not** validated for operational grid control,
electricity trading, reserve scheduling, dispatch, or any safety-critical or
financial decision. It produces point forecasts only and does not, in its
live configuration, quantify the probability or cost of extreme errors (a
quantile-band mode exists in the codebase but is not part of the production
submission — Section 8).

Several feature and hyperparameter variants were built, backtested, and
explicitly **not** adopted into production this season (Section 7): day-ahead
temperature injection, demand- and cooling-degree-hour sample weighting,
quantile-loss training objectives, widened `num_leaves`/`max_depth` search
bounds, and a 72-hour lag candidate. Each remains in the codebase as an
opt-in, backtest-gated option and is documented with its actual measured
result, not discarded silently.

## 3. How to Get Started

### Reproduce a specific documented submission

Every submission since 2026-06-28 automatically saves a self-contained
reproducibility package — the submitted forecast, the exact weather snapshot
the model saw at prediction time, the SpotOptim tuning results, the trained
model, and the raw ENTSO-E training data — to `_cache/reproducibility/<target_date>/`,
with a manifest recording the exact git commit, whether the working tree was
clean at run time, package versions, and the full variant configuration used.

```bash
uv sync
uv run python scripts/apply_package_patches.py   # required after every uv sync, see Section 12
cat _cache/reproducibility/<target_date>/manifest.json   # exact commit + config used
```

**Submissions before 2026-06-28 predate this mechanism and cannot be exactly
reproduced** — Open-Meteo overwrites its own forecast data with reanalysis
values after roughly five days, so the weather signal the model actually saw
at prediction time is gone on any later replay for those dates. This is the
same limitation the original 2026-06-21 reproducibility package documented
independently (measured replay drift: mean absolute 139.47 MW, maximum
absolute 356.18 MW, mean signed -4.82 MW, against a best-effort frozen-weather
replay). The package therefore distinguishes **submission provenance**
(what was actually submitted, always preserved) from **bit-identical replay
reproducibility** (only available from 2026-06-28 onward) rather than
claiming they are the same thing.

### Run the live production pipeline

```bash
cd bart26l-vorlesung
export ENTSOE_API_KEY="<personal ENTSO-E token>"

uv run python scripts/team_weather_report_submit.py \
  --team-id team_weather_report \
  --leaderboard-root <path to a clone of challenge-leaderboard> \
  --n-trials 10 --n-initial 5 --no-figures --push
```

The generated CSV is validated and pushed as a pull request before the
challenge deadline (target day `D` minus one day, `~02:00` local). The
automated scheduler (`scripts/nightly_submit.sh`) runs this exact command at
21:00 and 23:30 CEST nightly, idempotent against double-submission and locked
against overlapping runs (Section 9).

### Run a backtest

```bash
uv run python scripts/team_weather_report_backtest.py \
  --target-date 2026-06-22 --variant rich \
  --n-trials 10 --n-initial 5 --train-years 3 \
  --output-dir _cache/team4_backtests/season_01/<experiment-name>
```

## 4. Technical Specification

### Task and recursive forecast

ENTSO-E supplies quarter-hourly values. The pipeline aggregates `Actual Load`
to hourly means and learns a one-step mapping from historical load, rolling
statistics, and exogenous information to the next load value. A 24-hour
forecast is produced recursively:

$$
\hat{y}_{t+h} = f(\hat{y}_{t+h-1}, \ldots, y_{t+h-k}, x_{t+h}),
\qquad h=1,\ldots,24.
$$

For later horizons, earlier predictions enter the lag window. Consequently,
forecast errors can propagate through the day — a genuine, unaddressed
limitation, not a hypothetical one (see the 06-17 evening-surge case in
Section 7).

### Training and validation

| Setting | Production value |
| --- | --- |
| Training history | Three years ending at the latest complete, quality-controlled hour (`train_years=3`), matching the download-window fairness rule applied across every team in the challenge |
| Regressor | `LGBMRegressor` inside `ForecasterRecursive` |
| Random state | `42` |
| Validation | SpotOptim's own chronological cross-validation over the training window |
| Hyperparameter optimizer | SpotOptim, sequential surrogate search |
| Production budget | `n_trials=10`, `n_initial=5` |
| Outlier bounds | Automatic outlier detection (`spotforecast2_safe` default) plus explicit value-sanity QC (Section 6) |
| Imputation | Explicit weighted time-series imputation; zero residual NaNs required before training |
| Polynomial interactions | Degree two, capped at 40 selected interactions |

PACF is calculated from quality-controlled historical `Actual Load`; the
eight strongest significant partial-autocorrelation lags warm-start the
search. SpotOptim then chooses from six candidate lag sets, every one of
which retains the weekly anchor (167/168h). A representative recent live
selection (the run of 2026-07-07 21:00 CEST, targeting the 2026-07-08
forecast; verified directly against `tuning_results/bart26k-live-team4_Actual
Load_spotoptim_20260707_205947.json`, not approximated): lags
`[1, 2, 3, 15, 24, 25, 26, 48, 168, 169]` (PACF-informed candidate, extended
with the 48h and 168/169h anchors), with hyperparameters:

| Hyperparameter | Value |
| --- | ---: |
| `num_leaves` | 98 |
| `max_depth` | 5 |
| `learning_rate` | 0.04672 |
| `n_estimators` | 1754 |
| `bagging_fraction` | 0.65273 |
| `feature_fraction` | 0.67677 |
| `reg_alpha` | 2.44489 |
| `reg_lambda` | 0.000564 |

SpotOptim reliably selects different configurations across dates within the
current search space (confirmed this season at full budget, t100/i40) — the
table above is one representative instance, not a fixed default the model
falls back to.

### Active feature families

The production (`rich`) pipeline uses the following information when
available at forecast time:

- autoregressive load lags selected from six anchored candidate sets,
- rolling load means over 72 hours, 168 hours (7 days), and 720 hours (30 days),
- daily, weekly, monthly, quarterly, and yearly cyclic calendar features,
- 15-city, population-weighted Open-Meteo weather data: temperature,
  apparent temperature, heating/cooling degree hours, weather-window features,
- day/night indicators and continuous solar ephemeris features,
- German holiday, day-before/day-after-holiday, bridge-day, workday, and
  day-type features,
- ENTSO-E day-ahead wind and solar generation forecasts,
- ENTSO-E day-ahead electricity price,
- COVID infection-rate history supplied by the configured provider,
- at most 40 degree-two polynomial interaction features.

A logged `07_bounds_num_leaves_weekday` backtest run (2026-07-07 target,
train_years=3) selected 198 total feature names in the fitted LightGBM
model — the exact count for any specific live submission is not currently
recorded in the per-submission reproducibility manifest (Section 3), so this
figure should be read as a representative order of magnitude, not a
verified live-run number; a real gap worth closing by adding a feature-count
field to that manifest. Exact columns may differ run to run if an optional
provider is unavailable, because weather and side-provider failures use an
explicit `skip` policy (logged, not fatal to the run).

**Feature variants evaluated but not in production** (backtested and
rejected, Section 7): day-ahead temperature/temperature-anomaly injection,
demand-proportional and cooling-degree-hour sample weighting, quantile-loss
training objective, widened `num_leaves`/`max_depth` search bounds, and a
72-hour lag candidate.

### Leakage prevention

Target-day `Actual Load` is unavailable at training time and is not imputed
into the future. A post-training leakage guard inspects the target frame,
selected exogenous columns, and fitted model feature names, and aborts the
run if ENTSO-E `Forecasted Load` or an `Actual`-derived proxy reaches the
model. Every logged production run this season reports
`"leakage guard passed"` with the exact feature count; absence of this line
means a run's output should not be trusted.

## 5. Interfaces and Runtime

### Inputs

| Input | Role | Availability requirement |
| --- | --- | --- |
| ENTSO-E Actual Load | target history, lags, rolling features | only published values up to the cutoff |
| Open-Meteo weather | weather and weather-window features | forecast values for the prediction horizon, 15 cities |
| ENTSO-E wind and solar forecasts | exogenous day-ahead generation information | available before the target day |
| ENTSO-E day-ahead price | exogenous market information | available before the target day |
| Calendar and holiday rules | deterministic time features | computable in advance |
| ENTSO-E Forecasted Load | shape sanity reference only | never a model feature (leakage-guarded) |

All timestamps are normalized to UTC. Input frames must be monotonic and
regular after preprocessing. The training target is hourly MW; quarter-hourly
ENTSO-E values are aggregated by mean.

### Output

```text
timestamp_utc,forecast_mw
2026-07-09T00:00:00Z,...
...
2026-07-09T23:00:00Z,...
```

The validator requires exactly 24 unique hourly UTC timestamps and finite,
positive forecast values (`y0.clip(lower=1.0)` is applied before validation
as an additional safety floor).

### Runtime and persistence

A production `10/5` run takes roughly 15-45 minutes end to end on a
commodity laptop CPU, dominated by the SpotOptim search. No GPU is required
or used. Fitted models are serialized as compressed joblib files and are
included, per submission, in the `_cache/reproducibility/<date>/` package
described in Section 3.

## 6. Data and Operational Design Domain

The Operational Design Domain (ODD) is the German bidding-zone load
forecasting task at hourly resolution under the challenge's data
availability rules. These thresholds have been in place since early June and
remain unchanged as of this update.

| Condition | Valid operation | Response outside the condition |
| --- | --- | --- |
| Actual-load freshness | latest complete hour no more than 36 hours old (`MAX_ACTUAL_LAG_HOURS`) | abort unless `--allow-stale` is explicitly set |
| Interior target gaps | no gap longer than 12 hours in recent history (`MAX_ACTUAL_GAP_HOURS`) | abort |
| Quarter-hour integrity | intra-hour range below 8,000 MW (`MAX_INTRAHOUR_RANGE_MW`) | truncate corrupt tail (policy: `truncate`, chapter default since 2026-06-05) |
| Adjacent target movement | at most 6,000 MW step (`MAX_ADJ_STEP_MW`) | truncate corrupt tail |
| Actual-vs-forecast deviation | no single-slot deviation exceeding 11,000 MW (`MAX_DEVIATION_MW`) | truncate corrupt tail |
| Weather cache coverage | all 15 cities' per-city caches complete for the requested range | auto-repaired (forward/back-fill of isolated NaN gaps) and retried once; a fully missing city blocks the run |
| Forecast profile | correlation >= 0.6 vs. ENTSO-E day-ahead reference (`SHAPE_MIN_CORR`) | warning only, human review — not blocking |
| Forecast amplitude | range ratio >= 0.5 vs. reference (`SHAPE_MIN_RANGE`) | warning only, human review — not blocking |
| Submission schema | exactly 24 valid, positive UTC rows | validator failure; do not submit |

The model is measurably less reliable on rapid demand regime shifts not
represented in its three-year training window — documented concretely this
season, not hypothetically: systematic underprediction on multiple June
heat-wave dates (bias -899 to -969 MW at full trial budget), and a
market-wide (not model-specific) overprediction stretch 2026-07-03 through
2026-07-05 that coincided with a demand drop no team's model anticipated in
advance. Recursive uncertainty generally increases toward the end of the
24-hour horizon; the point forecast does not communicate this increase
numerically.

LOCF (last-observation-carried-forward) results are outside normal model
operation: they measure a missed or invalid submission and reuse an earlier
forecast. They are reported separately from fresh model forecasts throughout
this project (`leaderboard_adjusted_analysis.md`), never blended into a
single "model quality" number.

## 7. Evaluation

Unlike a library that ships no trained model, this project has a full
season of real, scored results — both live (leaderboard) and experimental
(backtest) — tracked continuously rather than as a single snapshot.

### Live challenge results (current as of 2026-07-08, 29 scored days since `RESTART_DATE`)

Four separate metrics are tracked, specifically so operational failures
(missed/late submissions) and model quality are never conflated:

| Metric | Value | Basis |
| --- | ---: | --- |
| Official leaderboard MAE | 2304.14 MW | all 29 scored days, including LOCF |
| True-submission MAE | 1447.65 MW | fresh submissions only, n=20 |
| LOCF-adjusted MAE | 1555.72 MW | LOCF days replaced with that day's cross-team fresh median, n=29 (9 replaced) |
| Final-model MAE (current config, since 06-30) | 1446.93 MW | fresh submissions on the current production config only, n=8 |
| Official rank | 18 of 19 live-qualified, registered teams | — |

The gap between the official and LOCF-adjusted/true-submission figures is
large and is this project's central, recurring, honestly-documented finding:
9 of 29 days are missed-submission days with real, non-strategic, documented
root causes (Section 9) — not model failures. `leaderboard_adjusted_analysis.md`
is the authoritative, continuously updated source for these figures and their
full derivation.

### Backtest experiment grid (`_cache/team4_backtests/season_01/`)

Every feature or hyperparameter change is backtested against a fixed
reference-date set before any production decision — adopted only when it
measurably wins, not assumed to help:

| Experiment | Finding |
| --- | --- |
| Weather features vs. none | Rich features win, -29% avg MAE — adopted |
| SpotOptim bound widening (`bagging_fraction`, `reg_lambda`, `reg_alpha`) | Adopted — production default |
| Full trial budget (t100/i40) | Worse than t10/i5 — the CV metric doesn't penalize systematic bias; more trials optimized harder for the wrong thing |
| Quantile loss (standalone, both budgets) | Ruled out — catastrophic at full budget (bias equaled MAE: every hour underpredicted) |
| Day-ahead temperature features | Ruled out over 11 hot-summer reference dates (`rich` won 8/11) |
| Sample weighting x CDH-weighting x quantile loss (12-cell grid) | Nothing beat plain `rich`; worst combination exceeded 2x the baseline MAE |
| Post-hoc additive bias correction (recent-submission-history-based) | Real effect (-34% to -60% MAE) on combinations with high, consistent Curvature Accuracy (`CA = bias/MAE`, a diagnostic introduced this season); confirmed **not** to help the low-CA production model as a blanket correction — predicted in advance, then confirmed by test |
| `num_leaves`/`max_depth` lower-bound widening | Worse on every date tested (+15% to +73% MAE) despite a diagnostic suggesting the optimizer was constrained |
| 72-hour lag candidate addition | Worse on every date tested (+27% to +49% MAE) |

The last two rows are included deliberately even though negative: a
diagnostic suggesting room for improvement does not guarantee that widening
a bound or adding a feature actually helps, and every change in this
project is tested against that possibility rather than assumed.

## 8. Model Transparency

The end-to-end pipeline is inspectable Python code. LightGBM is a tree
ensemble rather than an interpretable physical load model, but split-count
feature importance is available through the fitted estimator, and SHAP-based
global feature attribution (`shap.TreeExplainer`) *is* implemented
(`_plot_shap`, part of `save_diagnostics`). It does not run on the actual
production path, however: the live nightly submission always passes
`--no-figures`, which skips `save_diagnostics` (and therefore SHAP)
entirely. SHAP output is only produced when a run is invoked without that
flag — useful for offline inspection, not part of what generates a live
forecast. SpotOptim-selected lags and hyperparameters are persisted for
every run regardless (Section 5).

The system produces point forecasts and does not provide calibrated
prediction intervals in the live submission. An optional quantile-band mode
(`save_prediction_band`, q10/q50/q90) exists in the codebase and can be
enabled via `--prediction-band`, but is not part of the documented
production submission. Feature importance explains model usage, not
causality — a high importance for weather or price must not be read as
proof that the feature caused electricity demand.

Recursive forecasting means an error in an early predicted hour can enter
the lag window of later hours. Reviewers should inspect the whole daily
curve, especially ramps and late-horizon behavior, rather than relying only
on the daily mean — a real, observed failure mode this season (the 06-17
evening-surge miss was a shape error, not a level error, and would not have
been caught by a mean-only check).

## 9. Operation: Monitoring and Response

This section is unusually substantive because a meaningful fraction of this
season's actual work has been operational, not modeling — documented
honestly rather than glossed over, since it is the largest driver of the
gap between official and true-submission performance (Section 7).

Each live run performs and logs:

1. ENTSO-E download retries with bounded timeout and exponential backoff,
2. freshness, coverage, gap, range, step, and deviation checks (Section 6),
3. weather-cache gap auto-repair, with a retry-once wrapper that survives a
   *different* city independently tripping the same latent defect on a later
   run (see incidents below),
4. explicit outlier detection and weighted imputation, with a zero-NaN
   assertion after imputation,
5. leakage inspection against the fitted model's feature names,
6. a shape comparison against ENTSO-E's day-ahead profile (warning-only),
7. CSV schema validation before any push,
8. automatic reproducibility packaging (Section 3).

**Incidents found and fixed this season**, each with a real, investigated
root cause, not a workaround:

- A weather-cache bug (`fetch_data.py`: shared filename across all 15
  cities, so city 2 silently read city 1's data) caused an unrecoverable
  submission failure; the affected day (2026-07-02) remains a permanent,
  documented gap — no fabricated replacement was created.
- A git branch-collision bug during a manual recovery submission, resolved
  via a clean-checkout recovery procedure; PR merged successfully.
- `launchctl unload` was found to kill an *in-progress* scheduled run, not
  just prevent future fires — cost one submission before being understood.
  Fixed permanently with a PID-file lock inside `nightly_submit.sh` itself,
  removing any need to ever unload the scheduler for concurrency reasons.
- A legacy `launchctl load` silently left the scheduled job registered but
  never actually armed (`runs = 0` despite appearing loaded in `launchctl
  list`) — fixed by switching to the modern `bootstrap`/`bootout` commands.
- An idempotency-check bug: the "already submitted today?" check matched
  *any* team's commit mentioning the target date, not only this team's,
  causing two consecutive scheduled fires to silently skip a real
  submission. Fixed by anchoring the check to this team's exact
  commit-message prefix, verified against real git history before trusting it.

**Standing safeguards, current as of this update**: per-submission
reproducibility packaging, a PID-file lock against concurrent/overlapping
runs, an idempotent installed-package-patch script (Section 12), and the
weather-cache auto-repair-and-retry mechanism described above.

The operational priority is a valid submission before experimentation. The
team records, per submission, the target day, code commit, working-tree
cleanliness, package versions, selected lags and hyperparameters, and the
model artifact (Section 3).

## 10. Compliance Support

This is an educational forecasting system, not a certified high-risk AI
system. The following controls nevertheless map, loosely, to the
documentation concepts used in the course's EU AI Act reference mapping
(inherited from `spotforecast2-safe`'s own compliance framing):

| Obligation | Support in this deployment |
| --- | --- |
| Risk management | chronological backtests, baseline comparison before every adopted change, QC thresholds (Section 6), human shape review |
| Data governance | source identification, freshness/gap/coverage checks, leakage guard |
| Technical documentation | this card, `leaderboard_adjusted_analysis.md`, the full `season_01` experiment log with negative results included |
| Record-keeping | per-submission reproducibility packages (Section 3), console/file logs, tuning JSON, model artifacts |
| Transparency | recursive equation (Section 4), documented active feature families, documented rejected variants |
| Accuracy and robustness | four-way MAE reporting (Section 7) that separates operational failure from model quality by design |

No CPE identifiers, SBOM entries, or formal certification are maintained for
this submission specifically; where this project's own dependencies
(`spotforecast2-safe`, `spotforecast2`) carry that documentation, it applies
to those libraries, not to this team's configuration or pipeline code.

## 11. Glossary

| Term | Meaning |
| --- | --- |
| Actual Load | ENTSO-E measurement used as the historical forecasting target |
| Bias | Mean of forecast minus actual; negative values indicate average underprediction |
| Curvature Accuracy (CA) | `bias / MAE`, introduced this season. `\|CA\|` near 1 means a day's error is a near-uniform level offset (theoretically recoverable by a post-hoc shift); `\|CA\|` near 0 means errors partially cancel — a genuine shape problem, not fixable by a shift |
| ENTSO-E | European Network of Transmission System Operators for Electricity |
| Forecasted Load | ENTSO-E's own day-ahead load forecast; reference only, never a model feature |
| Lag | A past value of the target series (e.g., "lag 24" = demand 24 hours ago) used as an input feature |
| LightGBM | Gradient-boosted decision-tree regressor |
| LOCF | Last observation carried forward after a missing fresh submission |
| MAE | Mean absolute error in MW |
| MAPE | Mean absolute percentage error |
| PACF | Partial autocorrelation function used to propose informative lags |
| RMSE | Root mean squared error in MW |
| SpotOptim | Surrogate-model-based hyperparameter optimizer (Sequential Parameter Optimization) |
| UPR | Underprediction rate, percentage of hours with forecast below actual |

## 12. How to Audit

1. Confirm the two required installed-package patches are applied:
   `uv run python scripts/apply_package_patches.py` — idempotent, fails
   loudly (not silently) if the installed `spotforecast2`/`spotforecast2-safe`
   don't match the expected original or patched text.
2. For any specific past submission, inspect
   `_cache/reproducibility/<date>/manifest.json`: records the exact git
   commit, whether the tree was dirty at run time, package versions, and the
   full variant configuration used.
3. Read `leaderboard_adjusted_analysis.md` for the complete, dated,
   append-only record of official vs. adjusted performance and every
   operational incident's root cause.
4. Read `_cache/team4_backtests/season_01/roadmap.md` and the
   `experiment.md`/`plan.md` files in each numbered subfolder for the full
   backtest history, including every negative result.
5. Confirm the leakage guard: every training run logs `"leakage guard
   passed: ENTSO-E forecast absent from target, exog, and model"` with the
   exact feature count; absence of this line means the run should not be
   trusted.
6. For the 2026-06-21 historical snapshot specifically, the original
   `team-weather-report-repro-2026-06-21.zip` remains available with its own
   SHA256SUMS and `verify_replay.py`, documenting the exact provenance vs.
   replay distinction described in Section 3.

## 13. Citation, Authors, and Contact

Team Weather Report. (2026). *Team Weather Report Day-Ahead Load Forecast:
Model/Method Card and Reproducibility Package*. DDMO SoSe 2026.

| Role | Name / contact |
| --- | --- |
| Team | Team Weather Report |
| Registered contributors | GitHub `LaloMohamad`, `yanickfotsing` |
| Course | Data-Driven Modeling and Optimization, SoSe 2026 |
| Technical foundation | `spotforecast2-safe`, `spotforecast2`, SpotOptim |

Suggested BibTeX entry:

```bibtex
@misc{team_weather_report_2026,
  author       = {{Team Weather Report}},
  title        = {Team Weather Report Day-Ahead Load Forecast},
  year         = {2026},
  howpublished = {DDMO SoSe 2026 reproducibility package},
  note         = {Updated 2026-07-09; season-to-date evaluation}
}
```

## 14. Disclaimer and Liability

This system and its documentation were created for an educational forecasting
challenge. They are provided as is, without warranty of accuracy,
availability, fitness for a particular purpose, or uninterrupted access to
external providers.

The forecast is not approved for operational grid control, electricity
trading, reserve planning, dispatch, or safety-critical decisions. Known
limitations include point-forecast uncertainty, recursive error propagation,
concept drift and rapid demand-regime shifts (documented concretely in
Section 6-7), unusual holidays and weather regimes, provider outages,
publication delays, and the missing original weather snapshot for
submissions before 2026-06-28. Any organization considering use outside the
course must independently validate the full system, define monitoring and
fallback procedures, and assume all deployment responsibility.
