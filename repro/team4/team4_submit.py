#!/usr/bin/env python3
"""Standalone reproduction of the operational pipeline in
``lecture/14_team_4_submission.qmd`` (chapter 14, team_4 advanced submission),
runnable independently of Quarto.

It mirrors the chapter's *non-teaching* path --- download -> coverage guard ->
PACF lag selection -> ConfigEntsoe -> MultiTask(spotoptim) -> y_0 -> submission
CSV -> validate --- and optionally opens the leaderboard PR. The ~7 plotly
*teaching* visuals (endogenous / outliers / imputation / weather / rbf /
holidays / poly) and the prose availability tables are intentionally omitted;
five run-diagnostic figures (ACF, feature importance, SHAP, the forecast plot,
and team_4-vs-ENTSO-E) are saved as PNGs.

The reason this script exists: a render of chapter 14 aborted when ENTSO-E
returned transient HTTP 504/503 and ``download_new_data`` exhausted its shallow
5x/5s retry budget. This script wraps the download in an outer exponential-
backoff loop and an optional cached-data fallback so a transient outage no
longer kills the whole job.

PACKAGED COPY (reproducibility package) -- this file is a copy of
``bart26k-lecture/scripts/team4_submit.py`` at commit 87e27e1 with exactly
three documented divergences, all required to make the package self-contained
and historically replayable (see MANIFEST.md):

  D1  new ``--as-of <ISO-UTC>`` flag: freezes "now" so a frozen data snapshot
      reproduces a historical target day instead of failing freshness guards.
  D2  data/cache homes resolve to ``<package>/data`` and ``<package>/.cache``
      instead of ``~/spotforecast2_data/...`` (self-containment); the default
      diagnostics directory follows the data home.
  D3  ``--leaderboard-root`` defaults to the package directory (which bundles
      ``teams.yml`` + ``scripts/validate_submission.py``) instead of
      ``~/workspace/challenge-leaderboard``.

Run it from the unzipped package directory:

    uv sync --frozen
    uv run python team4_submit.py --skip-download \
        --as-of 2026-06-07T15:00:00Z --deterministic     # offline replay
    uv run python team4_submit.py --help                 # all flags

SYNC NOTE --- this file duplicates chapter-14 logic. When ch14 changes any of:
the env homes (`team4-imports`), the date constants (`team4-constants`), the
coverage guards (`team4-interim`/`team4-cutoff`, incl. the frontier
completeness guard and the value-sanity QC), the PACF rule (`fig-team4-acf`), the forecaster factory
(`team4-factory`), the ConfigEntsoe arguments (`team4-config`), the SpotOptim
search space (`team4-search-space`), the pipeline method order
(`team4-prepare`...`team4-train`), the shape plausibility check
(`team4-shape-check`), or the submission schema
(`team4-submission-write`), re-sync this script. The .qmd is the source of truth.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Headless backend MUST be selected before anything (incl. shap) imports pyplot.
import matplotlib

matplotlib.use("Agg")

import pandas as pd

# --- defaults pinned to the chapter-14 values (see SYNC NOTE) -----------------
COUNTRY = "DE"
# Team identity follows the feature set (qmd team4-submission-write): with
# ENTSO-E's day-ahead Forecasted Load as a model input, submit as
# team_4_entsoe; without it, as team_4. --team-id still overrides.
# Since the chapter split (2026-06-06) 14_team_4_submission.qmd is the PLAIN
# team_4 variant (both flags False); the ENTSO-E-prior variant lives in
# 14_team_4_entsoe_submission.qmd, mirrored by scripts/team4_entsoe_submit.py.
INCLUDE_ENTSOE_FORECAST_LOAD = False
INCLUDE_ENTSOE_NET_LOAD = False          # derived from Forecasted Load -> off together with it
DEFAULT_TEAM_ID = "team_4_entsoe" if INCLUDE_ENTSOE_FORECAST_LOAD else "team_4"
START_DOWNLOAD = "202201010000"
DATA_SUBDIR = "ddmo_ch14_team4"             # under ~/spotforecast2_data/ (= chapter `team4-imports`)
CACHE_SUBDIR = "ddmo_ch14_team4"            # under ~/.spotforecast2_cache/
OUTLIER_IQR_K = 5
MAX_ACTUAL_LAG_HOURS = 36                # team4-interim guard (qmd line 252)
GAP_SCAN_DAYS = 28                       # team4-interim interior-gap guard
MAX_ACTUAL_GAP_HOURS = 12                #   (third check; added 2026-06-04)
MAX_INTRAHOUR_RANGE_MW = 8_000           # team4-cutoff value-sanity QC: clean-data
MAX_ADJ_STEP_MW = 6_000                  #   q0.9999 is 7.3/4.1 GW (added 2026-06-05)
QC_WINDOW_DAYS = 3                       #   scan scope for the value-sanity QC
MAX_DEVIATION_MW = 11_000                # deviation rule (sf2-safe >= 18.1.0): dropout vs
DEVIATION_REF = "Forecasted Load"        #   day-ahead forecast; calibration in diagnosis/
DEVIATION_SLOTS = 1                      #   experiments/e9: 1 clean slot < -11 GW in 2.4 y,
                                         #   06-07 frontier dropout is single-slot -> slots=1
TARGET_CORRUPTION_POLICY = "truncate"    # chapter default since 2026-06-05; "abort" = conservative
LAG_FALLBACK = [1, 2, 24, 168]
TRAIN_YEARS = 2
PREDICT_SIZE = 24
REFIT_SIZE = 7
NUMBER_FOLDS = 10
IMPUTATION_WINDOW_SIZE = 24
N_TRIALS_SPOTOPTIM = 100
N_INITIAL_SPOTOPTIM = 50
N_TRIALS_OPTUNA = 15

# Packaged-copy divergences D2/D3: everything resolves relative to the package.
PACKAGE_ROOT = Path(__file__).resolve().parent

logger = logging.getLogger("team4_submit")


class Abort(Exception):
    """Raise to stop with a specific process exit code (see exit-code table)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Dates:
    now: pd.Timestamp
    today: pd.Timestamp
    yesterday: pd.Timestamp
    tomorrow: pd.Timestamp
    last_target: pd.Timestamp
    start_dl: str
    end_dl: str


@dataclass
class Coverage:
    last_full_hour: pd.Timestamp
    first_pred: pd.Timestamp
    live_n_steps: int


# --- CLI + logging ------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="team4_submit",
        description="Standalone team_4 live forecast + submission (chapter 14).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--team-id", default=DEFAULT_TEAM_ID,
                   help="registry check + submission directory")
    p.add_argument("--skip-download", action="store_true",
                   help="reuse the cached interim CSV; no ENTSO-E API call")
    p.add_argument("--max-retries", type=int, default=5,
                   help="outer download attempts (each wraps the library's 5x loop)")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="per-socket read timeout (s) for ENTSO-E downloads; "
                        "0 disables (sf2-safe >= 16.2.0)")
    p.add_argument("--backoff", type=float, default=5.0,
                   help="base seconds for exponential backoff between outer attempts")
    p.add_argument("--allow-stale", action="store_true",
                   help="permit cache fallback when the API is down "
                        "(still gated by the D-1 23:00 freshness check)")
    p.add_argument("--data-end", default=None,
                   help="override END_DOWNLOAD as YYYYMMDDHHMM (freezes the window)")
    p.add_argument("--as-of", default=None, metavar="ISO-UTC",
                   help="packaged-copy divergence D1: freeze 'now' (e.g. "
                        "2026-06-07T15:00:00Z) so the bundled data snapshot "
                        "replays its historical target day deterministically")
    p.add_argument("--no-validate", action="store_true",
                   help="skip the leaderboard validator subprocess")
    p.add_argument("--push", action="store_true",
                   help="branch + commit + open the auto-merging PR via gh")
    p.add_argument("--n-jobs", type=int, default=-1,
                   help="n_jobs_spotoptim (-1 = all cores; 0 = serial)")
    p.add_argument("--deterministic", action="store_true",
                   help="force serial SpotOptim (n_jobs=None) for bit-reproducible runs")
    p.add_argument("--n-trials", type=int, default=N_TRIALS_SPOTOPTIM,
                   help="n_trials_spotoptim (lower for a fast smoke run)")
    p.add_argument("--n-initial", type=int, default=N_INITIAL_SPOTOPTIM,
                   help="n_initial_spotoptim")
    p.add_argument("--train-years", type=int, default=TRAIN_YEARS,
                   help="training window length in years")
    p.add_argument("--tensorboard", action="store_true",
                   help="stream the SpotOptim tuning run to TensorBoard "
                        "(sf2 >= 5.1.0 / spotoptim >= 0.12.8; watch with "
                        "'tensorboard --logdir <data_home>/tensorboard')")
    p.add_argument("--tensorboard-path", default=None,
                   help="TensorBoard event-file directory "
                        "(default: <data_home>/tensorboard/<target-date>)")
    p.add_argument("--figures-dir", default=None,
                   help="where to save diagnostic PNGs "
                        "(default: <data_home>/figures/<target-date>/)")
    p.add_argument("--no-figures", action="store_true",
                   help="skip all diagnostic figures")
    p.add_argument("--leaderboard-root",
                   # Packaged-copy divergence D3: the package bundles teams.yml
                   # and scripts/validate_submission.py, so it IS a minimal
                   # leaderboard root (upstream default:
                   # ~/workspace/challenge-leaderboard).
                   default=str(PACKAGE_ROOT),
                   help="challenge-leaderboard clone or the package directory")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v: show downloader INFO; -vv: DEBUG everywhere")
    return p.parse_args(argv)


def setup_logging(verbosity: int) -> None:
    # Keep the packages quiet by default (like the chapter's basicConfig WARNING)
    # but give our own logger a dedicated handler so its INFO always shows.
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [team4_submit] %(levelname)s %(message)s",
                                           "%H:%M:%S"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbosity >= 2 else logging.INFO)
    logger.propagate = False
    if verbosity >= 1:
        logging.getLogger("spotforecast2_safe.downloader.entsoe").setLevel(logging.INFO)
    if verbosity >= 2:
        for name in ("spotforecast2", "spotforecast2_safe"):
            logging.getLogger(name).setLevel(logging.DEBUG)


# --- environment, registry, dates ---------------------------------------------
def configure_environment() -> None:
    """Mirror the qmd ``team4-imports`` cell: fork start method + isolated homes.

    Must run before any spotforecast2* import / MultiTask / download call;
    get_data_home()/get_cache_home() read these env vars at call time.
    """
    os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
    import multiprocessing as mp

    try:
        mp.set_start_method("fork", force=True)
    except (RuntimeError, ValueError):
        logger.warning("could not set multiprocessing start method to 'fork'; "
                       "pass --n-jobs 0 for serial tuning if pool workers fail.")
    # Packaged-copy divergence D2 (see module docstring): homes live INSIDE
    # the package so `unzip + uv sync + uv run` works with zero setup.
    # Upstream uses ~/spotforecast2_data/<DATA_SUBDIR> and
    # ~/.spotforecast2_cache/<CACHE_SUBDIR>.
    os.environ["SPOTFORECAST2_DATA"] = str(PACKAGE_ROOT / "data")
    os.environ["SPOTFORECAST2_CACHE"] = str(PACKAGE_ROOT / ".cache")


def check_team_registry(team_id: str, lb_root: Path) -> None:
    """qmd ``team4-registry`` (lines 54-66): fail-loud if team_id is unknown."""
    import yaml

    teams_yml = lb_root / "teams.yml"
    if not teams_yml.exists():
        raise Abort(3, f"teams.yml not found at {teams_yml}; "
                       f"is challenge-leaderboard cloned at {lb_root}?")
    teams = yaml.safe_load(teams_yml.read_text())["teams"]
    team = next((t for t in teams if t["id"] == team_id), None)
    if team is None:
        raise Abort(3, f"{team_id} is not registered in {teams_yml}")
    logger.info("team %s : %s | github: %s", team_id,
                team.get("display_name", "?"),
                ", ".join(team.get("github_handles", [])))


def compute_dates(now: pd.Timestamp, data_end: str | None) -> Dates:
    """qmd ``team4-constants`` (lines 146-167). `now` is captured once upstream."""
    today = now.normalize()
    yesterday = today - pd.Timedelta(days=1)
    tomorrow = today + pd.Timedelta(days=1)
    last_target = tomorrow + pd.Timedelta(hours=23)
    if data_end:
        try:
            pd.to_datetime(data_end, format="%Y%m%d%H%M", utc=True)
        except Exception as exc:  # noqa: BLE001
            raise Abort(1, f"--data-end {data_end!r} is not YYYYMMDDHHMM: {exc}")
        end_dl = data_end
    else:
        end_dl = (tomorrow + pd.Timedelta(days=1)).strftime("%Y%m%d%H%M")
    return Dates(now, today, yesterday, tomorrow, last_target, START_DOWNLOAD, end_dl)


# --- download (robust) + interim cache ----------------------------------------
def robust_download(api_key: str, dates: Dates, *, max_retries: int,
                    backoff: float, force: bool = True,
                    timeout: float | None = 60.0) -> bool:
    """qmd ``team4-download`` (lines 172-185), wrapped in an outer exponential-
    backoff loop. Returns True on success, False after exhausting attempts.

    The venv's download_new_data already retries 5x/5s internally and raises
    RuntimeError on persistent HTTP 504/503; this outer loop multiplies that
    resilience without editing shipped safety code.
    """
    from spotforecast2_safe.downloader.entsoe import download_new_data

    for attempt in range(1, max_retries + 1):
        try:
            download_new_data(
                api_key=api_key,
                country_code=COUNTRY,
                start=dates.start_dl,
                end=dates.end_dl,
                force=force,
                keep_forecast_future=True,
                timeout=timeout,
            )
            logger.info("ENTSO-E download succeeded on attempt %d/%d", attempt, max_retries)
            return True
        except Exception as exc:  # noqa: BLE001 -- download_new_data raises RuntimeError
            wait = backoff * (2 ** (attempt - 1))
            logger.warning("ENTSO-E download attempt %d/%d failed: %s",
                           attempt, max_retries, exc)
            if attempt < max_retries:
                logger.warning("retrying in %.0fs (exponential backoff) ...", wait)
                time.sleep(wait)
    logger.error("ENTSO-E download failed after %d outer attempts.", max_retries)
    return False


def download_side_tables(api_key: str, dates: Dates,
                         timeout: float | None = 60.0) -> None:
    """qmd renewable/price cells (``team4-renewable-inspect`` / ``team4-price-inspect``
    download companions): refresh the two provider side-tables.

    Failures are non-fatal by design: the providers behind the ``include_*``
    flags degrade via ``on_exog_provider_failure="skip"``, so a transient
    side-table outage costs features, not the submission.
    """
    from spotforecast2_safe.downloader.entsoe import (
        download_day_ahead_price,
        download_renewable_forecast,
    )

    for name, fn, country in (
        ("renewable forecast", download_renewable_forecast, COUNTRY),
        ("day-ahead price", download_day_ahead_price, "DE_LU"),
    ):
        try:
            fn(api_key=api_key, country_code=country,
               start=dates.start_dl, end=dates.end_dl, force=False,
               timeout=timeout)
            logger.info("side-table download ok: %s", name)
        except Exception as exc:  # noqa: BLE001 -- provider skip handles the gap
            logger.warning("side-table download failed (%s): %s -- the matching "
                           "provider will be skipped.", name, exc)


def load_interim() -> pd.DataFrame:
    """qmd ``team4-interim`` read (lines 236-238)."""
    from spotforecast2_safe.data.fetch_data import get_data_home

    interim_csv = get_data_home() / "interim" / "energy_load.csv"
    if not interim_csv.exists():
        raise Abort(2, f"no interim cache at {interim_csv}; cannot proceed "
                       f"without a successful download.")
    interim = pd.read_csv(interim_csv, index_col=0, parse_dates=True)
    interim.index = pd.to_datetime(interim.index, utc=True)
    logger.info("interim CSV: %s  (%d rows, %s -> %s)",
                interim_csv, len(interim), interim.index.min(), interim.index.max())
    return interim


def assert_coverage(interim: pd.DataFrame, dates: Dates, *, fallback: bool) -> Coverage:
    """qmd coverage guards (lines 240-262) + cutoff (lines 322-325).

    `fallback=True` (cache reused because the download was skipped/failed) adds a
    stricter gate: the published actuals must reach at least *yesterday 23:00 UTC*
    --- a full most-recent complete UTC day --- before we build a submission off
    cached data. (The plan's "D-1 23:00" phrasing resolves to yesterday 23:00 for
    a target day of *tomorrow*; today 23:00 is in the future and unpublishable.)
    """
    required_last = dates.today - pd.Timedelta(hours=1)
    if interim.index.max() < required_last:
        raise Abort(1, f"ENTSO-E coverage is stale: last interim row "
                       f"{interim.index.max()} < required {required_last}.")

    actual = interim["Actual Load"].dropna()
    if actual.empty:
        raise Abort(1, "interim CSV has no published Actual Load values.")
    last_actual = actual.index.max()
    if last_actual < dates.now - pd.Timedelta(hours=MAX_ACTUAL_LAG_HOURS):
        lag_h = int((dates.now - last_actual) / pd.Timedelta(hours=1))
        raise Abort(1, f"Actual Load is stale: last published {last_actual}, "
                       f"{lag_h} h before now; tolerance {MAX_ACTUAL_LAG_HOURS} h.")

    # Interior-gap guard (qmd team4-interim, third check): a publication outage
    # can hole the *middle* of the feed while both edge checks pass (observed
    # 2026-06-02 -- a full day of actuals published more than a day late).
    recent = actual.loc[dates.now - pd.Timedelta(days=GAP_SCAN_DAYS):]
    gaps = recent.index.to_series().diff()
    oversized = gaps[gaps > pd.Timedelta(hours=MAX_ACTUAL_GAP_HOURS)]
    if not oversized.empty:
        worst_end = oversized.idxmax()
        raise Abort(1, f"Actual Load has {len(oversized)} interior gap(s) wider "
                       f"than {MAX_ACTUAL_GAP_HOURS} h in the last "
                       f"{GAP_SCAN_DAYS} days (worst: "
                       f"{worst_end - oversized.max()} -> {worst_end}). "
                       f"Re-download once the late actuals are published.")

    # Frontier completeness guard (qmd team4-cutoff): only an hour with all of
    # its quarter-hour samples published may anchor the recursion -- a partial
    # frontier hour averages to an anomalous level and drags the whole first
    # forecast day (observed on the 2026-06-05 forecast). The expected count
    # per hour derives from the feed's own cadence (15 min for DE -> 4).
    cadence = actual.index.to_series().diff().mode().iloc[0]
    samples_per_hour = int(pd.Timedelta(hours=1) / cadence)
    samples_by_hour = actual.resample("h").count()
    last_full_hour = samples_by_hour[samples_by_hour >= samples_per_hour].index.max()
    if last_full_hour < last_actual.floor("h"):
        logger.warning("frontier hour %s is incomplete (<%d samples); end_train "
                       "stepped back to %s", last_actual.floor("h"),
                       samples_per_hour, last_full_hour)

    # Value-sanity tripwire (qmd team4-cutoff; 2026-06-03/04 incident): ENTSO-E
    # published complete but physically impossible quarter-hour actuals
    # (adjacent slots oscillating by +-10 GW, intra-hour ranges up to 15 GW,
    # identical in every raw vintage). Such values pass every staleness and
    # completeness check, then poison the recursion anchor and the seasonal
    # lags. Thresholds are the clean-data q0.9999 rounded up; the late-March
    # DST week can reach ~12.6 GW ranges and would need a temporary exemption.
    # Since sf2-safe 16.4.0 the rules live in the library (same detector as
    # the chapter cell); the call below is a PREVIEW -- the authoritative
    # policy run happens inside prepare_data via the target_qc_* knobs.
    # Default policy "truncate" (since 2026-06-05): training retracts to the
    # last sound hour and the recursion bridges the gap on published
    # day-ahead exog. Gate A of the incident forensics: ENTSO-E never
    # corrects this corruption class retroactively, so aborting buys no
    # better data later; "abort" remains the conservative alternative.
    from spotforecast2_safe.exceptions import TargetCorruptionError
    from spotforecast2_safe.preprocessing import apply_target_corruption_policy

    qc_recent = interim["Actual Load"].loc[
        actual.index.max() - pd.Timedelta(days=QC_WINDOW_DAYS):
    ]
    qc_hourly = qc_recent.resample("h")
    qc_range = (qc_hourly.max() - qc_hourly.min()).dropna()
    qc_step = qc_recent.diff().abs().dropna()
    # Deviation diagnostics (third rule, sf2-safe >= 18.1.0): dropout vs the
    # published day-ahead forecast — catches what the dynamics rules cannot
    # see (e9 calibration). NaN-safe by construction: the dropna() keeps the
    # publication-lag tail out of the minimum.
    qc_dev = (
        interim["Actual Load"] - interim[DEVIATION_REF]
    ).loc[qc_recent.index.min():].dropna()
    logger.info("value-sanity QC (last %d d): max intra-hour range %.0f MW "
                "(limit %d), max adjacent step %.0f MW (limit %d), "
                "min deviation vs forecast %.0f MW (limit -%d)",
                QC_WINDOW_DAYS, qc_range.max(), MAX_INTRAHOUR_RANGE_MW,
                qc_step.max(), MAX_ADJ_STEP_MW,
                qc_dev.min(), MAX_DEVIATION_MW)
    try:
        _, qc_report = apply_target_corruption_policy(
            interim[["Actual Load", DEVIATION_REF]],
            targets=["Actual Load"],
            policy=TARGET_CORRUPTION_POLICY,
            range_mw=MAX_INTRAHOUR_RANGE_MW,
            step_mw=MAX_ADJ_STEP_MW,
            window_days=QC_WINDOW_DAYS,
            max_heal_hours=0,
            anchor_zone_hours=168,
            cutoff=None,
            logger=logger,
            deviation_mw=MAX_DEVIATION_MW,
            deviation_ref=DEVIATION_REF,
            deviation_slots=DEVIATION_SLOTS,
        )
    except TargetCorruptionError as exc:
        raise Abort(1, str(exc))
    if qc_report.fired:
        last_sound = qc_report.first_flagged_hour - pd.Timedelta(hours=1)
        logger.warning("value-sanity QC: %d corrupt hour(s) in %d span(s) -> "
                       "policy %r; prepare_data will retract data_end to %s "
                       "and auto-extend predict_size",
                       qc_report.n_flagged_hours, len(qc_report.spans),
                       qc_report.action, last_sound)

    if fallback:
        gate = dates.yesterday + pd.Timedelta(hours=23)  # yesterday 23:00 UTC
        if last_full_hour < gate:
            raise Abort(2, f"cached actuals end {last_full_hour} < {gate} "
                           f"(yesterday 23:00 UTC); refusing to submit a stale "
                           f"forecast. Re-run when ENTSO-E is reachable.")

    first_pred = last_full_hour + pd.Timedelta(hours=1)
    live_n_steps = int((dates.last_target - first_pred).total_seconds() // 3600) + 1
    logger.info("end_train=%s  first_pred=%s  last_target=%s  predict_size=%d",
                last_full_hour, first_pred, dates.last_target, live_n_steps)
    return Coverage(last_full_hour, first_pred, live_n_steps)


def entsoe_predictions(interim: pd.DataFrame) -> pd.Series:
    """qmd ``team4-entsoe-predictions`` (lines 283-284): ENTSO-E's own day-ahead
    forecast on the hourly grid (baseline only; never trained on)."""
    preds = interim["Forecasted Load"].resample("h").mean().dropna()
    preds.name = "Forecasted Load"
    return preds


# --- PACF lag selection (logic + optional plot) -------------------------------
def select_key_lags(interim: pd.DataFrame, last_full_hour: pd.Timestamp,
                    figdir: Path | None) -> list[int]:
    """qmd ``fig-team4-acf`` (lines 518-537), plot stripped out (optional save)."""
    import numpy as np
    from spotforecast2.stats.autocorrelation import calculate_lag_autocorrelation

    acf_series = interim.loc[:last_full_hour, "Actual Load"].resample("h").mean().dropna()
    acf = calculate_lag_autocorrelation(acf_series, n_lags=200)
    conf = 1.96 / np.sqrt(len(acf_series))
    significant = acf[acf["partial_autocorrelation_abs"] > conf]
    key_lags = sorted(significant.nlargest(8, "partial_autocorrelation_abs")["lag"].astype(int))
    if not key_lags:  # degenerate fallback (very short series)
        key_lags = list(LAG_FALLBACK)
    logger.info("PACF key_lags (top %d by |PACF|): %s  [N=%d, band=+/-%.4f]",
                len(key_lags), key_lags, len(acf_series), conf)
    if figdir is not None:
        _try(lambda: _plot_acf(acf, key_lags, conf, figdir), "acf")
    return key_lags


# --- config + pipeline --------------------------------------------------------
def team4_lgbm_factory(config, *, weight_func=None, target=None):
    """qmd ``team4-factory``: LightGBM with anchored level windows (>= 72 h).

    Default L2 objective; window features deliberately exclude the 24-h scale.
    A first version (regression_l1 + 24-h mean/min/max) flattened the live
    forecast for 2026-06-05: short windows turn into pure self-feedback over
    the live recursion, and L1 + reg_alpha soft-thresholding removed the
    incentive to track the daily amplitude. The CV cannot detect this (each
    fold restarts from observed history), hence the structural fix here.
    """
    from lightgbm import LGBMRegressor
    from spotforecast2_safe.forecaster.recursive import ForecasterRecursive
    from spotforecast2_safe.preprocessing import RollingFeatures

    del target
    return ForecasterRecursive(
        estimator=LGBMRegressor(random_state=config.random_state, verbose=-1),
        lags=config.lags_consider[-1],
        window_features=[  # one instance per feature: keeps generated names unique
            RollingFeatures(stats="mean", window_sizes=config.window_size),  # 72 h, as stock
            RollingFeatures(stats="mean", window_sizes=24 * 7),
            RollingFeatures(stats="mean", window_sizes=24 * 30),
        ],
        weight_func=weight_func,
    )


def build_search_space(key_lags):
    """qmd ``team4-search-space``: weekly-anchored SpotOptim lag pool.

    The estimator dimensions follow the chapter's widened ranges (larger
    upper bounds for tree capacity, learning rate, and trees; tighter
    regularisation caps); every lag candidate carries the weekly anchor
    167/168 (lags beyond the live horizon keep reading observed history
    during the recursion -- the anchor-free stock candidate ``[1, 2, 24,
    48]`` won the flat 2026-06-05 run). ``warm_start_lags=True`` still
    prepends ``str(key_lags)``.
    """
    return {
        "estimator__num_leaves": (8, 1024),
        "estimator__max_depth": (3, 32),
        "estimator__learning_rate": (0.0001, 0.3, "log10"),
        # Linear integer range, deliberately NOT (10, 5000, "log10"): SpotOptim
        # < 0.12.7 int-cast log-transformed bounds, so an int+log10 dimension
        # collapsed to the decade exponents {10, 100, 1000, 10000} — the last
        # of which silently EXCEEDED the declared cap (observed 2026-06-05;
        # fixed upstream in spotoptim 0.12.7). The linear range stays:
        # transparent and optimizer-version independent.
        "estimator__n_estimators": (100, 5000),
        "estimator__bagging_fraction": (0.5, 1.0),
        "estimator__feature_fraction": (0.5, 1.0),
        "estimator__reg_alpha": (0.001, 10.0),
        "estimator__reg_lambda": (0.001, 10.0),
        "lags": [
            "[1, 2, 3, 11, 12, 22, 23, 24, 47, 48, 167, 168]",   # stock 12-lag
            "[1, 2, 11, 12, 23, 24, 167, 168]",                   # stock 8-lag
            "[1, 2, 24, 48, 167, 168]",                           # compact + weekly anchor
            "[1, 2, 23, 24, 47, 48, 167, 168]",                   # cycle neighbours + weekly
            str(sorted(set(key_lags) | {1, 2, 24, 48, 168})),     # PACF picks + canonical
            "[1, 2, 3, 23, 24, 25, 47, 48, 167, 168, 169, 336]",  # extended + 2-week lag
        ],
    }


def build_config(key_lags, cov: Coverage, *, n_jobs, n_trials, n_initial, train_years):
    """qmd ``team4-config``."""
    from spotforecast2_safe.data import Period
    from spotforecast2_safe.configurator.config_entsoe import ConfigEntsoe
    from spotforecast2.tasks.task_entsoe import (
        entsoe_data_loader,
        entsoe_test_data_loader,
    )

    periods = [
        Period(name="daily",     n_periods=12, column="hour",      input_range=(1, 24)),
        Period(name="weekly",    n_periods=7,  column="dayofweek", input_range=(0, 6)),
        Period(name="monthly",   n_periods=12, column="month",     input_range=(1, 12)),
        Period(name="quarterly", n_periods=4,  column="quarter",   input_range=(1, 4)),
        Period(name="yearly",    n_periods=12, column="dayofyear", input_range=(1, 365)),
    ]
    cfg = ConfigEntsoe(
        country_code=COUNTRY,
        data_filename="interim/energy_load.csv",
        targets=["Actual Load"],
        agg_weights=[1.0],
        bounds=None,
        data_loader=entsoe_data_loader,
        test_data_loader=entsoe_test_data_loader,
        forecaster_factory=team4_lgbm_factory,
        periods=periods,
        lags_consider=key_lags,
        train_size=pd.Timedelta(days=365 * train_years),
        end_train_default=cov.last_full_hour.isoformat(),
        delta_val=pd.Timedelta(hours=PREDICT_SIZE * REFIT_SIZE * NUMBER_FOLDS),
        predict_size=cov.live_n_steps,
        cv_block_size=PREDICT_SIZE,
        refit_size=REFIT_SIZE,
        number_folds=NUMBER_FOLDS,
        imputation_window_size=IMPUTATION_WINDOW_SIZE,
        n_trials_optuna=N_TRIALS_OPTUNA,
        n_trials_spotoptim=n_trials,
        n_initial_spotoptim=n_initial,
        n_jobs_spotoptim=n_jobs,
        warm_start_lags=True,
        include_weather_windows=True,
        include_holiday_features=True,
        include_holiday_adjacency_features=True,  # Brückentag + day before/after holiday (sf2-safe >= 15.9.0)
        poly_features_degree=2,
        max_poly_features=40,
        state="NW",
        random_state=42,
        on_weather_failure="skip",
        # Provider-based day-ahead/static priors (chapter `team4-config`, sf2-safe >= 16.1.0).
        # The forecast-load flag also decides DEFAULT_TEAM_ID (module constants).
        include_entsoe_forecast_load=INCLUDE_ENTSOE_FORECAST_LOAD,
        include_entsoe_renewable_forecast=True,
        include_entsoe_net_load=INCLUDE_ENTSOE_NET_LOAD,
        include_entsoe_day_ahead_price=True,
        include_covid_infection_rate=True,
        on_exog_provider_failure="skip",
        exog_max_gap_hours=3,
        exog_max_tail_gap_hours=48,
        exog_provider_window="train",
        # Target-side corruption policy (sf2-safe >= 16.4.0): same rules as
        # the assert_coverage value-sanity QC, applied authoritatively at
        # 15-min cadence inside prepare_data (qmd team4-config). Default
        # "truncate" (since 2026-06-05): retract data_end to the last sound
        # hour + auto-extend predict_size; "abort" = conservative alternative.
        target_qc_range_mw=MAX_INTRAHOUR_RANGE_MW,
        target_qc_step_mw=MAX_ADJ_STEP_MW,
        target_qc_window_days=QC_WINDOW_DAYS,
        target_corruption_policy=TARGET_CORRUPTION_POLICY,
        # Deviation rule (sf2-safe >= 18.1.0): catches dropouts that stay
        # below the dynamics thresholds (2026-06-07 frontier: 5.6 GW steps
        # at Actual - Forecast = -11.6 GW). Calibration: e9.
        target_qc_deviation_mw=MAX_DEVIATION_MW,
        target_qc_deviation_ref=DEVIATION_REF,
        target_qc_deviation_slots=DEVIATION_SLOTS,
    )
    cfg.data_frame_name = "bart26k-live-team4"
    logger.info("config ready: predict_size=%s n_trials_spotoptim=%s n_jobs=%s "
                "number_folds=%s", cfg.predict_size, cfg.n_trials_spotoptim,
                cfg.n_jobs_spotoptim, cfg.number_folds)
    return cfg


def run_pipeline(cfg, search_space):
    """qmd ``team4-prepare``...``team4-train``, figures dropped."""
    from spotforecast2.multitask import MultiTask

    mt = MultiTask(cfg, task="spotoptim")
    logger.info("prepare_data() ...")
    mt.prepare_data()

    # Outlier bounds from the load history (qmd team4-outlier-bounds, lines 1216-1223).
    s = mt.df_pipeline["Actual Load"].dropna()
    med = s.median()
    iqr = s.quantile(0.75) - s.quantile(0.25)
    low = max(0.0, med - OUTLIER_IQR_K * iqr)
    high = med + OUTLIER_IQR_K * iqr
    mt.config.bounds = [(low, high)]
    logger.info("outlier bounds (K=%d): [%.0f, %.0f] MW  (median=%.0f, IQR=%.0f)",
                OUTLIER_IQR_K, low, high, med, iqr)

    mt.detect_outliers()
    mt.impute()
    nan_after = int(mt.df_pipeline["Actual Load"].isna().sum())
    if nan_after != 0:
        raise Abort(1, f"imputation left {nan_after} NaN in Actual Load (must be 0)")
    logger.info("imputation OK (0 NaN remaining)")

    mt.build_exogenous_features()

    logger.info("starting SpotOptim search (n_trials=%s, n_jobs=%s) -- the slow "
                "step (~5-10 min on a laptop) ...", cfg.n_trials_spotoptim,
                cfg.n_jobs_spotoptim)
    t0 = time.monotonic()
    # qmd team4-train: run_task_spotoptim, not run() -- the generic dispatcher
    # does not forward a custom search space.
    mt.run_task_spotoptim(search_space=search_space, show=False)
    logger.info("SpotOptim done in %.1f s", time.monotonic() - t0)
    return mt


def extract_y0(mt, dates: Dates) -> pd.Series:
    """qmd (lines 1699-1705)."""
    future = mt.results["spotoptim"]["Actual Load"]["future_pred"]
    logger.info("raw forecast: %d steps  %s -> %s",
                len(future), future.index.min(), future.index.max())
    y0 = future.loc[dates.tomorrow:dates.last_target]
    logger.info("y_0 slice: %d steps  %s -> %s", len(y0),
                y0.index.min() if len(y0) else None,
                y0.index.max() if len(y0) else None)
    return y0


SHAPE_MIN_CORR = 0.6     # qmd team4-shape-check: profile agreement threshold
SHAPE_MIN_RANGE = 0.5    #   amplitude: forecast range >= half the reference range


def warn_if_implausible_shape(y0: pd.Series, preds_entsoe: pd.Series,
                              interim: pd.DataFrame) -> None:
    """qmd ``team4-shape-check``: warn-only daily-profile plausibility check.

    Compares y_0 against ENTSO-E's day-ahead profile (fallback: actuals one
    week earlier) on Pearson correlation and daily-range ratio. Never aborts
    -- the operator decides (Art. 14); the 2026-06-05 flat forecast (r < 0,
    range ratio 0.30) is the incident this catches.
    """
    reference, ref_name = preds_entsoe.reindex(y0.index).dropna(), "ENTSO-E day-ahead"
    if len(reference) < 12:  # fallback: same weekday one week earlier
        week_ago = interim["Actual Load"].resample("h").mean().reindex(
            y0.index - pd.Timedelta(hours=168))
        week_ago.index = week_ago.index + pd.Timedelta(hours=168)
        reference, ref_name = week_ago.dropna(), "actuals one week earlier"
    if len(reference) < 12:
        logger.warning("shape check skipped: no reference profile available.")
        return
    common = y0.index.intersection(reference.index)
    r = float(y0.loc[common].corr(reference.loc[common]))
    ref_range = float(reference.loc[common].max() - reference.loc[common].min())
    range_ratio = float((y0.max() - y0.min()) / ref_range) if ref_range > 0 else float("nan")
    logger.info("shape check vs. %s (%d h): r=%.2f (min %.1f), range ratio=%.2f (min %.1f)",
                ref_name, len(common), r, SHAPE_MIN_CORR, range_ratio, SHAPE_MIN_RANGE)
    if r < SHAPE_MIN_CORR or range_ratio < SHAPE_MIN_RANGE:
        logger.warning("=" * 72)
        logger.warning("FORECAST SHAPE IMPLAUSIBLE: daily profile does not track the")
        logger.warning("%s reference (r=%.2f, range ratio=%.2f). Inspect the", ref_name, r, range_ratio)
        logger.warning("diagnostic figures before pushing (warn-only by design, Art. 14).")
        logger.warning("=" * 72)
    else:
        logger.info("shape check passed: daily profile and amplitude look plausible.")


def assert_no_leakage(mt) -> None:
    """qmd ``team4-load-model`` leakage guard (lines 1739-1746). Always runs
    (even with --no-figures): ENTSO-E's own forecast must never reach the model."""
    try:
        fc = mt.results["spotoptim"]["Actual Load"]["forecaster"]
        feat_names = list(fc.estimator.feature_name_)
    except Exception as exc:  # noqa: BLE001
        logger.warning("leakage guard skipped (cannot read fitted features): %s", exc)
        return
    leak = {"Forecasted Load", "Actual"}
    dwe = getattr(mt, "data_with_exog", None)
    exog_names = getattr(mt, "exog_feature_names", None)
    targets = {
        "training frame": set(dwe.columns) if dwe is not None else set(),
        "selected exogenous features": set(exog_names) if exog_names is not None else set(),
        "fitted model features": set(feat_names),
    }
    for where, cols in targets.items():
        if leak & cols:
            raise Abort(1, f"ENTSO-E forecast leaked into the {where} -- "
                           f"refusing to submit (CR-3 data-governance invariant).")
    logger.info("leakage guard passed: ENTSO-E forecast absent from target, "
                "exog, and model (%d features).", len(feat_names))


# --- diagnostic figures (best-effort; never block the submission) -------------
def family_of(col: str) -> str:
    """qmd helper (lines 1384-1396): map a feature name to its family."""
    c = col.lower()
    if "holiday" in c or "brueckentag" in c:
        return "holiday"
    if "poly" in c:
        return "polynomial"
    if "window" in c:
        return "weather_window"
    if any(k in c for k in ("sin", "cos", "rbf")):
        return "cyclical/RBF"
    if c.startswith("lag"):
        return "lag"
    return "weather/other"


def _try(fn, name: str) -> None:
    """Run a plot closure; log success/failure but never raise (CSV is critical)."""
    try:
        fn()
        logger.info("saved diagnostic: %s.png", name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not save %s.png: %s", name, exc)


def _plot_acf(acf, key_lags, conf: float, figdir: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.5, 3.0))
    ax.bar(acf["lag"], acf["autocorrelation"], width=0.9, color="#1F4E79")
    ax.axhline(conf, color="#999999", lw=0.8, ls="--")
    ax.axhline(-conf, color="#999999", lw=0.8, ls="--")
    for lag in key_lags:
        row = acf[acf["lag"] == lag]
        if not row.empty:
            val = float(row["autocorrelation"].iloc[0])
            ax.annotate(f"lag {lag}", xy=(lag, val), xytext=(lag, val + 0.08),
                        ha="center", fontsize=8, color="#E07B00",
                        arrowprops=dict(arrowstyle="->", color="#E07B00", lw=0.8))
    ax.set_xlabel("lag (hours)")
    ax.set_ylabel("autocorrelation")
    ax.set_title("ACF of Actual Load (annotated: data-selected key_lags)")
    ax.grid(True, color="#E5E5E5", linewidth=0.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(figdir / "acf.png", dpi=150)
    plt.close(fig)


def _plot_importance(fc, feat_names, figdir: Path) -> None:
    import matplotlib.pyplot as plt

    importances = fc.estimator.feature_importances_
    ranking = sorted(zip(feat_names, importances), key=lambda kv: kv[1], reverse=True)[:20]
    family_color = {
        "lag": "#B22222", "weather/other": "#E07B00", "weather_window": "#F0A33C",
        "cyclical/RBF": "#1F4E79", "holiday": "#2E8B57", "polynomial": "#7B5EA7",
    }
    labels = [n for n, _ in ranking][::-1]
    values = [v for _, v in ranking][::-1]
    colors = [family_color.get(family_of(n), "#888888") for n in labels]
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    ax.barh(labels, values, color=colors)
    ax.set_xlabel("split count (feature importance)")
    ax.set_title("Top-20 feature importances (coloured by family; lags in red)")
    ax.grid(True, axis="x", color="#E5E5E5", linewidth=0.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in family_color.values()]
    ax.legend(handles, family_color.keys(), fontsize=7, loc="lower right")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(figdir / "feature_importance.png", dpi=150)
    plt.close(fig)


def _plot_shap(fc, X_tr, figdir: Path) -> None:
    import shap
    import matplotlib.pyplot as plt

    step = max(1, len(X_tr) // 2000)
    X_sample = X_tr.iloc[::step]
    explainer = shap.TreeExplainer(fc.estimator)
    sv = explainer.shap_values(X_sample)
    shap.summary_plot(sv, X_sample, plot_type="bar", show=False)
    fig = plt.gcf()
    fig.tight_layout()
    fig.savefig(figdir / "shap.png", dpi=150)
    plt.close(fig)


def _plot_forecast(mt, preds_entsoe, figdir: Path) -> None:
    import plotly.io as pio

    from spotforecast2.plots.plotter import make_plot

    pkg = mt.results["spotoptim"]["Actual Load"]
    pkg["future_forecast"] = preds_entsoe.reindex(pkg["future_pred"].index)
    fig = make_plot(pkg)
    # kaleido v1 serialises via orjson, which rejects raw pandas Timestamps
    # in the figure ("Type is not JSON serializable: Timestamp"); round-trip
    # through Plotly's own JSON encoder first (no-op once sf2 >= 5.1 builds
    # the figure JSON-safe).
    pio.from_json(fig.to_json()).write_image(str(figdir / "forecast.png"), scale=2)


def _plot_vs_entsoe(y0, preds_entsoe, figdir: Path) -> None:
    import matplotlib.pyplot as plt

    entsoe_fc = preds_entsoe.reindex(y0.index)
    overlap = entsoe_fc.dropna().index
    fig, ax = plt.subplots(figsize=(8.5, 3.5))
    ax.plot(y0.index, y0.values / 1000, marker="o", ms=3, color="#1F4E79",
            label="team_4 forecast (y_0)")
    if len(overlap) > 0:
        ax.plot(entsoe_fc.index, entsoe_fc.values / 1000, marker="x", ms=4,
                color="#E07B00", label="ENTSO-E day-ahead forecast")
        mad = float((y0.loc[overlap] - entsoe_fc.loc[overlap]).abs().mean())
        logger.info("mean |team_4 - ENTSO-E| over %d overlap hours: %.0f MW",
                    len(overlap), mad)
    else:
        logger.info("ENTSO-E day-ahead forecast not yet published for the target "
                    "day; plotting team_4 only.")
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Load [GW]")
    ax.set_title("team_4 vs. ENTSO-E day-ahead forecast -- target day")
    ax.grid(True, color="#E5E5E5", linewidth=0.5)
    ax.legend(fontsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(figdir / "team4_vs_entsoe.png", dpi=150)
    plt.close(fig)


def save_diagnostics(mt, y0, preds_entsoe, figdir: Path) -> None:
    """qmd figures fig-team4-importance / -shap / -predict / -vs-entsoe."""
    figdir.mkdir(parents=True, exist_ok=True)
    fc = mt.results["spotoptim"]["Actual Load"]["forecaster"]

    X_tr = feat_names = None
    try:
        X_tr, _ = fc.create_train_X_y(
            y=mt.data_with_exog["Actual Load"],
            exog=mt.data_with_exog[mt.exog_feature_names],
        )
        feat_names = list(fc.estimator.feature_name_)
    except Exception as exc:  # noqa: BLE001
        logger.warning("design-matrix reconstruction failed; skipping "
                       "importance/SHAP: %s", exc)

    if feat_names is not None:
        _try(lambda: _plot_importance(fc, feat_names, figdir), "feature_importance")
    if X_tr is not None:
        _try(lambda: _plot_shap(fc, X_tr, figdir), "shap")
    _try(lambda: _plot_forecast(mt, preds_entsoe, figdir), "forecast")
    _try(lambda: _plot_vs_entsoe(y0, preds_entsoe, figdir), "team4_vs_entsoe")


# --- submission write + validate + push ---------------------------------------
def assert_contract(y0: pd.Series, dates: Dates) -> None:
    """qmd ``team4-submission-write`` contract (lines 1919-1924)."""
    assert len(y0) == 24, f"expected 24 hourly steps for y_0, got {len(y0)}"
    assert y0.index.min() == dates.tomorrow, (
        f"first step {y0.index.min()} != TOMORROW {dates.tomorrow}")
    assert (y0 > 0).all(), "non-positive forecast value -- spec requires > 0"
    assert y0.notna().all(), "NaN in forecast -- spec forbids"


def write_submission(y0: pd.Series, dates: Dates, team_id: str, lb_root: Path) -> Path:
    """qmd ``team4-submission-write`` (lines 1926-1935)."""
    sub_dir = lb_root / "submissions" / team_id
    sub_dir.mkdir(parents=True, exist_ok=True)
    path = sub_dir / f"{dates.tomorrow.date().isoformat()}.csv"
    df = pd.DataFrame({
        "timestamp_utc": y0.index.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forecast_mw": y0.round(2).values,
    })
    df.to_csv(path, index=False)
    logger.info("wrote %s  (%d rows, %.1f-%.1f MW)",
                path, len(df), df.forecast_mw.min(), df.forecast_mw.max())
    return path


def validate_submission(path: Path, lb_root: Path) -> int:
    """qmd ``team4-validate`` (lines 1947-1962): run the leaderboard's validator."""
    rel = path.relative_to(lb_root)
    cmd = ["uv", "run", "python", "scripts/validate_submission.py",
           "--path", str(rel), "--skip-deadline"]
    logger.info("validating: %s  (cwd=%s)", " ".join(cmd), lb_root)
    res = subprocess.run(cmd, cwd=lb_root, capture_output=True, text=True)
    if res.stdout.strip():
        logger.info("validator stdout: %s", res.stdout.strip())
    if res.stderr.strip():
        logger.warning("validator stderr: %s", res.stderr.strip())
    logger.info("validator exit code: %d", res.returncode)
    return res.returncode


def _push_steps(path: Path, dates: Dates, team_id: str, lb_root: Path):
    target = dates.tomorrow.date().isoformat()
    branch = f"submission/{team_id}-{target}-{dates.now.strftime('%H%M%S')}"
    rel = path.relative_to(lb_root)
    title = f"submission({team_id}): forecast for {target}"
    return branch, target, [
        ["git", "switch", "-c", branch, "main"],
        ["git", "add", str(rel)],
        ["git", "commit", "-m", title],
        ["git", "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--base", "main",
         "--repo", "bartzbeielstein/challenge-leaderboard", "--head", branch,
         "--title", title,
         "--body", "Generated by bart26k-lecture scripts/team4_submit.py "
                   "(chapter 14 standalone)."],
    ]


def push_submission(path: Path, dates: Dates, team_id: str, lb_root: Path) -> int:
    """qmd push block (lines 1972-1988): branch, commit, open the auto-merging PR."""
    branch, target, steps = _push_steps(path, dates, team_id, lb_root)
    for cmd in steps:
        logger.info("$ %s", " ".join(cmd))
        res = subprocess.run(cmd, cwd=lb_root, capture_output=True, text=True)
        if res.stdout.strip():
            logger.info("%s", res.stdout.strip())
        if res.returncode != 0:
            logger.error("push step failed: %s\n%s", " ".join(cmd), res.stderr.strip())
            return res.returncode
    logger.info("submission PR opened for %s on branch %s", target, branch)
    return 0


def print_push_instructions(path: Path, dates: Dates, team_id: str, lb_root: Path) -> None:
    _, target, steps = _push_steps(path, dates, team_id, lb_root)
    logger.info("CSV written but NOT pushed. To submit, run (or pass --push):")
    logger.info("  cd %s", lb_root)
    for cmd in steps:
        logger.info("  %s", " ".join(cmd))


def _figures_dir(args: argparse.Namespace, dates: Dates) -> Path:
    if args.figures_dir:
        figdir = Path(args.figures_dir).expanduser()
    else:
        # Packaged-copy divergence D2: diagnostics follow the package-local
        # data home (upstream: ~/spotforecast2_data/<DATA_SUBDIR>/figures/).
        figdir = (PACKAGE_ROOT / "data" / "figures"
                  / dates.tomorrow.date().isoformat())
    figdir.mkdir(parents=True, exist_ok=True)
    logger.info("diagnostics -> %s", figdir)
    return figdir


def _log_banner(dates: Dates, args: argparse.Namespace, lb_root: Path) -> None:
    logger.info("=" * 72)
    logger.info("team4_submit -- standalone chapter-14 pipeline")
    logger.info("now=%s  today=%s  tomorrow(target)=%s",
                dates.now, dates.today.date(), dates.tomorrow.date())
    logger.info("download window: %s -> %s", dates.start_dl, dates.end_dl)
    logger.info("data_home=%s", os.environ.get("SPOTFORECAST2_DATA"))
    logger.info("cache_home=%s", os.environ.get("SPOTFORECAST2_CACHE"))
    logger.info("team=%s  leaderboard=%s", args.team_id, lb_root)
    logger.info("tuning: n_trials=%s n_initial=%s n_jobs=%s%s",
                args.n_trials, args.n_initial, args.n_jobs,
                " (deterministic -> serial)" if args.deterministic else "")
    logger.info("=" * 72)


# --- orchestration ------------------------------------------------------------
def _run(args: argparse.Namespace) -> int:
    configure_environment()
    lb_root = Path(args.leaderboard_root).expanduser()
    check_team_registry(args.team_id, lb_root)

    if args.as_of:  # packaged-copy divergence D1 (see module docstring)
        try:
            now = pd.to_datetime(args.as_of, utc=True)
        except Exception as exc:  # noqa: BLE001
            raise Abort(1, f"--as-of {args.as_of!r} is not an ISO-8601 UTC "
                           f"timestamp: {exc}")
        logger.warning(">> --as-of set: 'now' frozen to %s (historical replay)", now)
    else:
        now = pd.Timestamp.now(tz="UTC")
    dates = compute_dates(now, args.data_end)
    _log_banner(dates, args, lb_root)

    # ---- download / cache decision ----
    api_key = os.environ.get("ENTSOE_API_KEY")
    used_cache = False
    if args.skip_download:
        logger.warning(">> --skip-download set: reusing cached interim, NO live "
                       "ENTSO-E call.")
        used_cache = True
    elif not api_key:
        if args.allow_stale:
            logger.error(">> ENTSOE_API_KEY missing and --allow-stale set: "
                         "falling back to cached interim.")
            used_cache = True
        else:
            raise Abort(3, "ENTSOE_API_KEY is not set. Export it, or pass "
                           "--skip-download / --allow-stale to use cached data.")
    else:
        timeout = args.timeout if args.timeout > 0 else None
        if not robust_download(api_key, dates, max_retries=args.max_retries,
                               backoff=args.backoff, timeout=timeout):
            if args.allow_stale:
                logger.error("=" * 72)
                logger.error("ENTSO-E DOWNLOAD FAILED -- --allow-stale set: reusing "
                             "cached interim data.")
                logger.error("The submission will be built from possibly-stale "
                             "cached actuals (gated by the yesterday-23:00 check).")
                logger.error("=" * 72)
                used_cache = True
            else:
                raise Abort(2, "ENTSO-E download failed and --allow-stale not set; "
                               "aborting (no stale submission).")
    if api_key and not args.skip_download:
        download_side_tables(api_key, dates,
                             timeout=args.timeout if args.timeout > 0 else None)

    interim = load_interim()
    cov = assert_coverage(interim, dates, fallback=used_cache)
    preds_entsoe = entsoe_predictions(interim)

    figdir = None if args.no_figures else _figures_dir(args, dates)
    key_lags = select_key_lags(interim, cov.last_full_hour, figdir)

    n_jobs = None if (args.deterministic or args.n_jobs == 0) else args.n_jobs
    cfg = build_config(key_lags, cov, n_jobs=n_jobs, n_trials=args.n_trials,
                       n_initial=args.n_initial, train_years=args.train_years)
    if args.tensorboard:
        # Plain config attributes — SpotOptimStrategy (sf2 >= 5.1.0) forwards
        # tensorboard_* to the SpotOptim constructor; live per-eval scalars
        # under n_jobs != 1 need spotoptim >= 0.12.8.
        from spotforecast2_safe.data.fetch_data import get_data_home

        tb_path = args.tensorboard_path or str(
            get_data_home() / "tensorboard" / str(dates.tomorrow.date())
        )
        cfg.tensorboard_log = True
        cfg.tensorboard_path = tb_path
        logger.info("TensorBoard logging on -> %s  (watch: tensorboard --logdir %s)",
                    tb_path, tb_path)
    mt = run_pipeline(cfg, build_search_space(key_lags))
    y0 = extract_y0(mt, dates)
    warn_if_implausible_shape(y0, preds_entsoe, interim)

    assert_no_leakage(mt)
    if figdir is not None:
        save_diagnostics(mt, y0, preds_entsoe, figdir)

    assert_contract(y0, dates)
    path = write_submission(y0, dates, args.team_id, lb_root)

    code = 0
    if not args.no_validate:
        if validate_submission(path, lb_root) != 0:
            logger.error("validator rejected the submission.")
            code = 4

    if args.push:
        if code != 0:
            logger.error("skipping --push because validation failed.")
        elif push_submission(path, dates, args.team_id, lb_root) != 0:
            logger.error("push failed.")
            code = code or 1
    else:
        print_push_instructions(path, dates, args.team_id, lb_root)

    if code == 0:
        logger.info("DONE: submission for %s written%s.",
                    dates.tomorrow.date().isoformat(),
                    " and validated" if not args.no_validate else "")
    return code


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    try:
        return _run(args)
    except Abort as abort:
        logger.error("%s", abort)
        return abort.code
    except Exception:  # noqa: BLE001
        logger.exception("unexpected failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
