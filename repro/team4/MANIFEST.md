# MANIFEST — team_4 reproducibility package

Snapshot date: **2026-06-07** (UTC). This file records exactly where and how
the `team_4` forecasts on
https://bartzbeielstein.github.io/challenge-leaderboard/ were produced.

## Execution architecture (original runs)

| Item | Value |
|---|---|
| Hardware | Apple Silicon (arm64, Apple T6050 class) |
| OS | macOS 26.5.1 (build 25F80), Darwin kernel 25.5.0 |
| `uname -a` | `Darwin p040025.vpn-f10.FH-Koeln.DE 25.5.0 Darwin Kernel Version 25.5.0: Mon Apr 27 20:41:12 PDT 2026; root:xnu-12377.121.6~2/RELEASE_ARM64_T6050 arm64` |
| Python | 3.14.2 (pinned in `.python-version`) |
| uv | 0.9.18 |
| Multiprocessing | start method `fork` (set by the script) |

Reproduction on other architectures: the pipeline runs on Linux/Windows
(see `tool.uv.environments` in `pyproject.toml`; Intel macOS is excluded —
unresolvable pin conflict in shap 0.52.0), but LightGBM floating-point
results can differ across architectures. Bit-exact reproduction is only
claimed for arm64 macOS.

## Source provenance

| Item | Value |
|---|---|
| Pipeline script | `bart26k-lecture/scripts/team4_submit.py` @ commit `87e27e1` |
| Leaderboard repo | `bartzbeielstein/challenge-leaderboard` @ commit `f7171d4` |
| Packaged copy divergences | D1 `--as-of` flag (historical replay), D2 package-local data/cache/figures homes, D3 `--leaderboard-root` defaults to the package dir. Full list in the module docstring of `team4_submit.py`; everything else is byte-identical to upstream. |

## Dependency pins

All `==`-pinned in `pyproject.toml`, fully resolved in `uv.lock`
(204 packages); queried from the original environment via
`importlib.metadata` on 2026-06-07. Key packages:

| Package | Version |
|---|---|
| spotforecast2-safe | 18.1.0 |
| spotforecast2 | 5.1.1 |
| spotoptim | 0.12.8 |
| lightgbm | 4.6.0 |
| pandas | 3.0.3 |
| numpy | 2.4.6 |
| scipy | 1.18.0rc1 |
| scikit-learn | 1.9.0 |
| shap | 0.52.0 |
| entsoe-py | 0.8.0 |

## Data snapshot (`data/interim/`)

ENTSO-E Transparency Platform data (DE bidding zone), downloaded by the
operational pipeline; snapshot taken 2026-06-07 ~15:04 UTC:

| File | Content | Coverage |
|---|---|---|
| `energy_load.csv` | Actual Total Load + day-ahead Forecasted Load, 15-min | 2015-01-01 → 2026-06-08 21:45 UTC (last published actual: 2026-06-07 14:00 UTC) |
| `renewable_forecast.csv` | wind/solar generation forecast | same window |
| `day_ahead_price.csv` | DE_LU day-ahead spot price | same window |

Attribution: data © ENTSO-E Transparency Platform
(https://transparency.entsoe.eu/), reused under its free-of-charge data
reuse terms for the purpose of scientific reproducibility.

## Integrity

SHA-256 checksums of every bundled file: `expected/SHA256SUMS`
(verify with `shasum -a 256 -c expected/SHA256SUMS`).

## Determinism statement

- **Repro profile** (`--deterministic`, serial SpotOptim, `random_state=42`,
  frozen snapshot, `--as-of 2026-06-07T15:00:00Z`): bit-identical output on
  arm64 macOS; reference CSV in `expected/`.
- **Operational profile** (parallel SpotOptim, `n_jobs=-1`, 100 trials —
  what actually produced the leaderboard submission): NOT bit-reproducible
  (parallel scheduling variance); re-runs land close to, but not identical
  to, `expected/2026-06-08_submitted.csv`.
- Weather and COVID exogenous providers fetch **historical** data live at
  run time (stable archives, but not version-pinned); offline they degrade
  gracefully (`on_exog_provider_failure="skip"`) — the run still works but
  uses fewer features and then deviates from the reference. The renewable
  forecast, day-ahead price and load inputs are bundled and offline-safe.
