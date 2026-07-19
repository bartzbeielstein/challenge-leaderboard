# Model/Method Card: Team Neura Live Load Forecasting Pipeline

This card describes the forecasting method used by Team Neura for the DDMO 2026 electricity load forecasting challenge. It uses the provided `MODEL_CARD.md` as a structural template and documents the live notebook pipeline in `live_preprocessing_v2_original_restored.ipynb`.

The method forecasts German hourly electricity load for a 24-hour target day and writes a leaderboard-ready CSV file to `submissions/neura/YYYY-MM-DD.csv`.

**Card version:** 1.0.0  ·  **Card date:** 2026-07-14  ·  **Applies to notebook:** `live_preprocessing_v2_original_restored.ipynb` (base LightGBM version)

## 1. Model Details

| Field | Value |
| --- | --- |
| Name | Team Neura Live Load Forecasting Pipeline |
| Version | 1.0.0 |
| Type | Recursive multi-step time-series forecasting pipeline for German electricity load |
| Challenge team | `neura` |
| Team members / GitHub handles | `Shervinfmz`, `Mayank07-Git`, `ravishankarbhojani123-svg` |
| Country / bidding zone | Germany, `DE` |
| Target variable | ENTSO-E `Actual Load` in MW |
| Forecast horizon | 24 hourly values for the target day, `00:00` to `23:00` UTC |
| Output format | CSV with columns `timestamp_utc` and `forecast_mw` |
| Main estimator | `ForecasterRecursiveLGBM` from `spotforecast2_safe` |
| Base regressor | `lightgbm.LGBMRegressor` |
| Main feature groups | historical load lags, calendar features, multi-city weather features, derived weather features |
| Normal grading account | `neura` |
| ENTSO-E forecasted-load account | `neura_entsoe`, only required if ENTSO-E Forecasted Load or netload is used |

The pipeline is a live forecasting workflow rather than a general-purpose forecasting package. It downloads recent ENTSO-E load data, preprocesses the load series, builds calendar and weather features, trains a recursive LightGBM model, generates a live forecast, validates the submission format, compares the result against weekly persistence, and stores provenance.

A key rule for the challenge is that the normal `neura` submission must not use ENTSO-E Forecasted Load as an input feature. The pipeline selects `Actual Load` as the training target and records `entsoe_forecast_used_for_submission: false` in provenance. Forecasted Load may exist in the raw ENTSO-E data file, but the normal grading forecast does not use it as a predictor.

## 2. Intended Use and Scope

The intended use is day-ahead German electricity load forecasting for the DDMO challenge leaderboard. The method produces one 24-hour CSV submission for the next target day. It is designed for repeated daily use, where the notebook is run shortly before the submission deadline.

The pipeline is suitable for:

- producing a normal graded Team Neura submission under `submissions/neura/`;
- evaluating recent forecast quality with a masked seven-day backtest;
- comparing the trained model against a weekly persistence baseline;
- documenting forecast provenance for later audit.

The method is not intended for safety-critical grid operation, automated trading without human review, or long-horizon planning. It produces point forecasts only and does not provide calibrated uncertainty intervals.

## 3. How to Get Started

The live notebook is run from the project environment and writes directly into the local clone of the challenge leaderboard repository.

Required setup:

```text
<leaderboard_repo>
<project_venv>
```

The notebook expects an ENTSO-E API key through the environment variable:

```text
ENTSOE_API_KEY
```

The normal submission is written to:

```text
<leaderboard_repo>\submissions\neura\YYYY-MM-DD.csv
```

The notebook automatically sets the live target date to tomorrow in UTC:

```python
NOW_UTC = pd.Timestamp.now(tz="UTC")
TODAY_UTC = NOW_UTC.normalize()
TOMORROW_UTC = TODAY_UTC + pd.Timedelta(days=1)
TARGET_DATE = TOMORROW_UTC.date().isoformat()
```

After running the notebook, the user creates a clean Git branch from `upstream/main`, adds exactly one submission CSV, commits it, pushes it to the fork, and opens a pull request.

## 4. Technical Specification

### Task

The task is recursive multi-step forecasting of German hourly load. The model receives historical actual-load values and aligned exogenous features, then predicts enough steps from the first unknown hour to the end of the target day. Only the 24 target-day rows are written to the leaderboard CSV.

### Data source

The pipeline downloads ENTSO-E German load data with `spotforecast2_safe.downloader.entsoe.download_new_data` and reads it with `spotforecast2_safe.data.fetch_data`. The raw data can contain both `Forecasted Load` and `Actual Load`, but the pipeline explicitly selects the `Actual Load` column for the training series:

```python
load_col = next(c for c in df_raw.columns if "Actual" in c and "Load" in c)
y_raw = df_raw[load_col].astype(float).rename("load")
```

If the downloaded series is at 15-minute resolution, it is resampled to hourly mean values.

### Live training window

The notebook starts the training download at:

```text
2024-01-01 00:00 UTC
```

The live end is the current UTC time at notebook execution. The actual training target is cut at the latest available full actual-load hour.

An example executed run for target date `2026-06-20` used:

| Quantity | Example value |
| --- | --- |
| Training start | `2024-01-01 00:00:00+00:00` |
| Last full actual hour | `2026-06-19 20:00:00+00:00` |
| First prediction hour | `2026-06-19 21:00:00+00:00` |
| Last target hour | `2026-06-20 23:00:00+00:00` |
| Live recursive steps | `27` |
| Final training rows | `21,621` |

### Preprocessing

The pipeline uses a professor-style annotation and weighting approach instead of blindly filling the original series.

The main preprocessing steps are:

1. Keep the raw hourly load series.
2. Mark real outliers with `mark_outliers(contamination=0.005, random_state=2026)`.
3. Fill missing and marked values with `get_missing_weights(window_size=168)`.
4. Create a `WeightFunction` so unreliable rows and their affected windows are not trusted during training.
5. Apply a manually defined quality mask for the professor-reported faulty ENTSO-E / TenneT period.

The masked faulty period is:

```text
2026-06-03 00:00 UTC to 2026-06-05 23:00 UTC
```

In the executed example run, the raw hourly series had six missing hourly values on `2026-05-31`. Outlier marking produced 115 marked rows. After missing-value processing and the quality mask, the final training target had no NaN values, and 4,382 rows had zero weight.

### Feature engineering

The model uses three main feature groups.

First, it uses recursive load lags. The main model uses 168 lags, which represents one week of hourly lag history.

Second, it builds calendar features with `ExogBuilder` and three `Period` definitions:

| Period | Number of encoded periods | Input range |
| --- | ---: | --- |
| hour | 12 | 0 to 23 |
| dayofweek | 4 | 0 to 6 |
| month | 12 | 1 to 12 |

For Germany, the calendar builder also supports country-specific calendar effects. In the example run, the calendar matrix contained 30 columns.

Third, it fetches multi-city weather features from Open-Meteo for four German locations:

| City | Latitude | Longitude |
| --- | ---: | ---: |
| Frankfurt | 50.110924 | 8.682127 |
| Berlin | 52.520008 | 13.404954 |
| Hamburg | 53.551086 | 9.993682 |
| Munich | 48.135125 | 11.581981 |

The base hourly weather variables are:

- `temperature_2m`
- `relative_humidity_2m`
- `precipitation`
- `cloud_cover`
- `wind_speed_10m`
- `wind_direction_10m`

The pipeline adds derived German weather aggregates such as mean/min/max temperature, temperature spread, heating degree, cooling degree, humidity mean, cloud cover mean, wind speed mean, and precipitation mean. It also adds simple rolling weather-window features over the previous 24 hours.

In the example run, the final exogenous feature matrix contained 64 columns: 30 calendar features and 34 weather-related features.

### Model

The estimator is `ForecasterRecursiveLGBM` from `spotforecast2_safe`, using a LightGBM regressor:

```python
ForecasterRecursiveLGBM(
    iteration=0,
    lags=168,
    periods=periods,
    country_code="DE",
    random_state=2026,
)
```

The LightGBM parameters are:

| Parameter | Value |
| --- | ---: |
| `n_estimators` | 400 |
| `learning_rate` | 0.05 |
| `num_leaves` | 63 |
| `min_child_samples` | 20 |
| `n_jobs` | -1 |
| `deterministic` | `True` |
| `force_col_wise` | `True` |
| `random_state` | 2026 |

The model is trained with the aligned exogenous feature frame and the `WeightFunction` produced during preprocessing.

### Submission generation

The model predicts from the first unknown actual-load hour until `23:00 UTC` of the target day. The notebook then slices exactly the 24 target-day timestamps and writes:

```text
submissions/neura/YYYY-MM-DD.csv
```

The CSV validation checks:

- exactly two columns: `timestamp_utc`, `forecast_mw`;
- 24 rows;
- no NaN values;
- no non-positive forecasts;
- timestamp format compatible with the challenge requirement.

## 5. Interfaces and Runtime

The target is a numeric univariate time series with a regular hourly date-time index. In this project, the target is German actual electrical load, measured in megawatts (MW). The model also uses numeric exogenous features stored as pandas DataFrame columns and aligned to the same hourly index. These features include calendar-based variables and weather-based variables. Before prediction, the input data is prepared and checked so that missing or incomplete feature data is not intentionally passed to the model.

Inside the pipeline, the data is transformed into lag-based input features, combined with encoded calendar information and aligned weather features, and then passed to a LightGBM-based recursive forecaster. Optional outlier handling is applied only where it is explicitly enabled. The model produces a 24-step hourly forecast for the challenge submission, and all predicted values are returned in the same unit as the target, megawatts (MW).

The fitted forecaster is saved in the project cache using Python pickle with a `.pkl` file extension. In the notebook, the saved model path follows the pattern `forecaster_live_<TEAM_ID>_<TARGET_DATE>.pkl`. This saved model can be loaded again from the same cache path for later inspection, prediction, or audit.

The pipeline runs in the project Python environment on a central processing unit (CPU). It does not require a graphics processing unit (GPU) and does not use GPU-specific code. The main runtime cost comes from feature engineering, lag construction, and fitting the LightGBM model. Reproducibility depends on fixed random seeds, stable dependency versions, deterministic model settings where available, and a stable data window.

Runtime dependencies are ordinary Python packages used for data handling, feature engineering, model fitting, persistence, calendar construction, weather data access, and API access. Most dependencies carry permissive licenses. The surrounding forecasting toolkit or related project libraries may carry their own license terms, so the final project documentation should keep the license information visible.

| Dependency         | License             |
| ------------------ | ------------------- |
| numpy              | BSD-3-Clause        |
| pandas             | BSD-3-Clause        |
| scikit-learn       | BSD-3-Clause        |
| feature-engine     | BSD-3-Clause        |
| numba              | BSD                 |
| lightgbm           | MIT                 |
| holidays           | MIT                 |
| pyarrow            | Apache-2.0          |
| requests           | Apache-2.0          |
| astral             | Apache-2.0          |
| tqdm               | MPL-2.0 and MIT     |
| spotforecast2-safe | AGPL-3.0-or-later      |

Because the pipeline performs CPU-only training and prediction and uses no GPU, its direct energy cost is moderate. Runtime cost is dominated by vector operations during feature engineering, lag construction, and the LightGBM fit. For an hourly ENTSO-E load series with a multi-year history, a typical LightGBM fit completes in seconds to minutes on a commodity CPU, depending on the number of features, lags, and validation loops. No pretrained weights are shipped with the project, so there are no embedded training emissions to report.

## 6. Data and Operational Design Domain

The model is designed for short-term forecasting of German electrical load using historical actual load, calendar information, and weather-related exogenous features. The production input data is supplied by the project pipeline from ENTSO-E load data and external weather sources. Validation is performed on historical target days so that model forecasts can be compared against later available actual load values. The evaluation is time-aware: only information available before the forecast horizon should be used, so future target values cannot influence past predictions.

The Operational Design Domain (ODD) describes the conditions under which the method is expected to produce valid results. Outside these conditions, the forecast may become unreliable or the pipeline may fail during preprocessing, feature construction, or prediction.

| Condition               | Valid range / requirement                                                                             | Outside the range                                                           |
| ----------------------- | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Target series           | German hourly actual electrical load, numeric, univariate, and ordered by time                        | unreliable or invalid forecast                                              |
| Time index              | regular hourly date-time index after preprocessing or resampling                                      | lag structure and forecast horizon may become incorrect                     |
| Sampling interval       | hourly observations                                                                                   | recursive 24-hour prediction may not match the expected challenge format    |
| Training history        | at least 168 hours plus enough additional historical variation for weekly load patterns               | weekly lag structure cannot be formed reliably                              |
| Missing target values   | handled only through the project’s explicit preprocessing, annotation, imputation, or weighting logic | raw unhandled NaN values are not valid input                                |
| Exogenous features      | numeric, complete, and aligned to the training and prediction time index                              | feature matrix cannot be built safely or prediction should fail             |
| Calendar features       | generated consistently for the training and target horizon                                            | weekday, holiday, or cyclic time effects may be wrong                       |
| Weather features        | available and aligned for both historical training data and the forecast horizon                      | weather-based feature matrix may be incomplete or unreliable                |
| ENTSO-E Forecasted Load | not used for normal `neura` grading submissions                                                       | if used directly or through netload, the submission must use `neura_entsoe` |
| Forecast horizon        | exactly 24 hourly target-day predictions                                                              | invalid leaderboard submission                                              |
| Output unit             | megawatts (MW), matching the target load unit                                                         | forecast values become inconsistent with the evaluation target              |

The method assumes that recent German load behavior, weekly seasonality, calendar structure, and weather conditions are informative for the next-day forecast. It is expected to work best when the target day is similar to patterns represented in the historical training data. Forecasts may be less reliable during public holidays, bridge days, unusual grid events, missing-data periods, extreme weather conditions, or sudden changes in electricity consumption behavior.

The model also depends on correct feature construction. Lag features must be built without leaking future load values into the prediction horizon. Calendar and weather features must be generated or aligned for the exact timestamps being predicted. If features are created manually outside the project pipeline, there is a risk of leakage, misalignment, or accidental use of information that would not have been available at forecast time.

To stay inside the valid domain, each forecast should be generated from a clean hourly load series, aligned calendar and weather features, and a sufficient historical window. New changes to the model or feature set should be validated against historical ground truth before being used for live challenge submissions. For the normal graded `neura` submission, ENTSO-E Forecasted Load and netload derived from Forecasted Load must not be used; those belong to the separate `neura_entsoe` track.

## 7. Evaluation

The notebook evaluates the method with a masked seven-day historical backtest. It searches backward over up to 90 candidate days and skips days whose target period or 168-hour input window touches the known faulty data period. This avoids evaluating the model on periods where the input data could be affected by known ENTSO-E or TenneT data-quality issues.

For each valid backtest day, the notebook trains a fresh recursive LightGBM model using the same feature design as the live submission model. The forecast is then compared against the later available actual load values and against a weekly persistence baseline. Weekly persistence uses the load from exactly one week earlier as a simple reference forecast. This comparison is important because the challenge ranking is mainly based on MAE, and the model should perform better than a simple seasonal baseline.

In the example executed run, the valid seven-day backtest summary was:

| Metric             |      Model | Weekly persistence |
| ------------------ | ---------: | -----------------: |
| Mean MAE           |   970.2 MW |         1,727.9 MW |
| Mean RMSE          | 1,174.4 MW |         2,179.6 MW |
| Mean MAPE          |      1.93% |              3.42% |
| Mean bias          |   -89.8 MW |          -547.4 MW |
| Days better by MAE |      6 / 7 |                  — |

A cleaner subset excluding affected days produced:

| Metric    |      Model | Weekly persistence |
| --------- | ---------: | -----------------: |
| Mean MAE  |   936.2 MW |         1,581.1 MW |
| Mean RMSE | 1,133.9 MW |         2,031.1 MW |
| Mean MAPE |      1.89% |              3.16% |
| Mean bias |    32.4 MW |          -215.3 MW |

For the example target date `2026-06-20`, the generated submission had 24 rows, no NaN values, no non-positive forecasts, a minimum forecast of 38,144.27 MW, and a maximum forecast of 50,431.77 MW.

### Honest performance caveat (backtest vs. live leaderboard)

The backtest figures above are **optimistic** and must not be read as expected live accuracy, for two reasons:

- The backtest uses **archive (actual) weather** for past days, while the live forecast must use **forecast weather**, which is less accurate.
- The backtest **excludes known faulty-data days**, removing some of the hardest periods.

On the live challenge leaderboard, over the rated days to date, the model's mean MAE has been materially higher — on the order of **2,100-2,500 MW** — placing it mid-field and roughly at the level of ENTSO-E's own day-ahead forecast (~2,046 MW mean MAE over 2024-2026 in this dataset). Days with unusual load (for example early June 2026) produce large errors for every participant, including ENTSO-E itself. The ~950 MW backtest figure reflects favourable conditions, not live performance. **Both numbers should be reported together** so the card does not overstate accuracy.

## 8. Model Transparency

The model is based on a transparent feature pipeline and a tree-based LightGBM regressor. The notebook prints and plots feature importances from the fitted estimator.

In the example run, the most important features included:

- `lag_1`
- calendar hour features such as `hour_0`, `hour_2`, `hour_5`, `hour_3`
- wind-speed features for Frankfurt, Hamburg, Munich, and Berlin
- weekly lag features such as `lag_167` and `lag_168`
- humidity and cloud-cover aggregate features

These importances are useful for sanity checking but should not be interpreted as causal explanations. They indicate how the fitted tree model used features in that run.

The provenance file records the model configuration, lag count, feature counts, training shape, prediction shape, weather locations, forecast bounds, backtest summary, and flags showing whether blending, weekly persistence, or ENTSO-E Forecasted Load were used for submission.

## 9. Operation: Monitoring and Response

Before each submission, the following checks should be reviewed:

1. ENTSO-E Actual Load freshness is below the maximum allowed lag.
2. The loaded frame covers the required recent period.
3. The final training target has no NaN values.
4. The exogenous feature frame has no NaN values.
5. The submission CSV has exactly 24 rows and the correct columns.
6. Forecast values are positive and plausible.
7. The forecast curve is compared visually against weekly persistence.
8. The PR contains exactly one file: `submissions/neura/YYYY-MM-DD.csv`.

If one of these checks fails, the response should be to stop the submission, inspect the source data and feature alignment, rerun the notebook after fixing the issue, and only then create a clean Git branch for the CSV.

## 10. Compliance and Challenge Rule Support

The professor's rule separates normal submissions from submissions using ENTSO-E Forecasted Load. This pipeline is designed for the normal graded team account:

```text
neura
```

The pipeline does not use ENTSO-E Forecasted Load or netload for the normal submission. The provenance explicitly records:

```json
"entsoe_forecast_used_for_submission": false,
"weekly_persistence_used_for_submission": false,
"blend_applied": false,
"forecast_source": "pure_model_prediction"
```

If a future version uses ENTSO-E Forecasted Load directly or indirectly, for example through:

```text
netload = forecasted_load - wind - solar
```

then that forecast should be submitted under:

```text
neura_entsoe
```

not under the normal `neura` grading team.

### Code rules (CR-1 to CR-4)

| Rule | Requirement | How this pipeline meets it |
| --- | --- | --- |
| CR-1 | No dead code | Cells run end-to-end each execution; disabled experiments are gated by explicit switches, not left as unreachable code. |
| CR-2 | Determinism | Fixed seed (2026); LightGBM `deterministic=True` and `force_col_wise=True`; no GPU or neural components. Same inputs give the same forecast. |
| CR-3 | Fail-safe on bad data | Freshness, coverage, and NaN checks raise explicit errors and halt rather than emitting a silent guess. |
| CR-4 | Minimal dependencies | Built on spotforecast2-safe, scikit-learn, LightGBM, pandas, and the ENTSO-E/Open-Meteo clients; no heavy ML frameworks (no torch or tensorflow). |

### EU AI Act alignment

This project is an educational artifact, not a certified high-risk system, but it is built to support the relevant duties, mirroring the underlying `spotforecast2-safe` library:

- **Article 10 (data governance):** missing or infinite data is rejected, not silently imputed.
- **Article 11 (technical documentation):** this card, the notebook, and the provenance file form the documentation baseline.
- **Article 12 (record-keeping):** run logs and the provenance JSON provide an audit trail.
- **Article 13 (transparency):** the pipeline is white-box; feature importances are inspectable.
- **Article 15 (accuracy and robustness):** deterministic, reproducible transforms, validated against historical ground truth before live use.

### Reproducibility

- Fixed random seed **2026**; deterministic LightGBM flags enabled; single-threaded execution removes floating-point reordering.
- Pinned environment: Python 3.13.x with pinned package versions (record exact versions in `requirements.txt` or a lockfile).
- Fixed training window (`DATA_START = 2024-01-01`) and cached inputs under `_cache/`.
- The underlying `spotforecast2-safe` library is deterministic and leakage-free by construction.
- **OpenSSF Scorecard:** run it against the software repository and record the score and link here -> `https://securityscorecards.dev/viewer/?uri=github.com%2FShervinfmz%2Fddmo-sose-26-Team-Neura`.

## 11. Limitations and Risks

The method has several limitations:

- It produces point forecasts only, without uncertainty intervals.
- Forecast quality depends on timely publication of ENTSO-E Actual Load.
- Weather forecast errors can affect future-hour exogenous features.
- The model may underperform during unusual holidays, faulty data periods, extreme weather, or sudden demand changes.
- Feature importance is descriptive, not causal.
- The model is retrained in the notebook workflow and is not a formally certified production system.
- Git submission errors can occur if the branch contains more than one changed file; the workflow must always check the PR diff before submission.

## 12. How to Audit

To audit a run, check the following artifacts:

1. The notebook execution output.
2. The generated submission CSV.
3. The provenance JSON file.
4. The saved model pickle.
5. The feature-importance output.
6. The weekly persistence comparison.
7. The recent seven-day backtest summary.
8. The GitHub PR diff.

A valid normal submission should satisfy:

```text
CSV path: submissions/neura/YYYY-MM-DD.csv
Rows: 24
Columns: timestamp_utc, forecast_mw
NaN: 0
Non-positive forecasts: 0
ENTSO-E Forecasted Load used as feature: false
PR changed files: exactly 1
```

## 13. Citation, Authors, and Contact

Prepared for Team Neura in the DDMO 2026 challenge.

Team GitHub handles:

- `Shervinfmz`
- `Mayank07-Git`
- `ravishankarbhojani123-svg`

Main software components:

- `spotforecast2_safe`
- `LightGBM`
- `pandas`
- `Open-Meteo`
- `ENTSO-E Transparency Platform`

Underlying forecasting library (please cite):

> Bartz-Beielstein, T. (2026). *spotforecast2-safe: Safety-critical subset of spotforecast2* (Version 25.0.0) [Computer software]. AGPL-3.0-or-later. https://github.com/sequential-parameter-optimization/spotforecast2-safe

Suggested citation:

```bibtex
@misc{neura_live_load_forecast_2026,
  author       = {Team Neura},
  title        = {Team Neura Live Load Forecasting Pipeline},
  year         = {2026},
  note         = {DDMO 2026 electricity load forecasting challenge model card}
}
```

## 14. Disclaimer and Liability

This method is provided for academic forecasting challenge use only. It is not intended for safety-critical deployment, operational grid control, electricity trading without human review, or any application where forecast errors could directly cause financial, operational, or safety harm.

The forecast quality depends on external data availability, correct notebook execution, and correct submission workflow. Team Neura and users of this method are responsible for validating each submission before creating a pull request.
