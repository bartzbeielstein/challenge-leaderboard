"""Tests for scripts/charts.py — hermetic, no network, no API key.

Pins the graceful-degradation contract (figures return None when their
data is missing) and the determinism guarantees (stable div_id).
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import pytest

import charts


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------

def _actuals(date: str = "2026-05-26", load: float = 1000.0,
             with_forecast: bool = True) -> pd.DataFrame:
    ts = pd.date_range(f"{date}T00:00:00Z", periods=24, freq="h", tz="UTC")
    data = {
        "ts": ts,
        "date": ts.strftime("%Y-%m-%d"),
        "load_mw": [load + i for i in range(24)],
    }
    if with_forecast:
        data["entsoe_forecast_mw"] = [load + 30 + i for i in range(24)]
    return pd.DataFrame(data)


def _sub(date: str = "2026-05-26") -> pd.DataFrame:
    ts = pd.date_range(f"{date}T00:00:00Z", periods=24, freq="h", tz="UTC")
    return pd.DataFrame({"ts": ts, "forecast_mw": [1010.0 + i for i in range(24)]})


def _scores() -> pd.DataFrame:
    return pd.DataFrame([
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 120.0, "mape": 1.0, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 200.0,
         "rmse": 220.0, "mape": 2.0, "carried_forward": True},
        {"team_id": "team_4", "target_date": "2026-05-27", "mae": 150.0,
         "rmse": 170.0, "mape": 1.5, "carried_forward": False},
    ])


def _board() -> pd.DataFrame:
    return pd.DataFrame([
        {"rank": 1, "team_id": "team_4", "display_name": "Team 4",
         "mean_mae": 125.0, "sum_mae": 250.0, "n_submissions": 2},
        {"rank": 2, "team_id": "hot_rod", "display_name": "Hot Rod",
         "mean_mae": 200.0, "sum_mae": 200.0, "n_submissions": 1},
    ])


NAMES = {"team_4": "Team 4", "hot_rod": "Hot Rod"}


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def test_load_actuals_missing_file_returns_none(tmp_path):
    assert charts.load_actuals(tmp_path / "nope.parquet") is None


def test_load_actuals_reads_parquet(tmp_path):
    p = tmp_path / "actual_load.parquet"
    ts = pd.date_range("2026-05-26T00:00:00Z", periods=24, freq="h", tz="UTC")
    pd.DataFrame({
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "load_mw": [1000.0] * 24,
    }).to_parquet(p, index=False)
    df = charts.load_actuals(p)
    assert df is not None
    assert list(df.columns) == ["timestamp_utc", "load_mw", "ts", "date"]
    assert df["date"].iloc[0] == "2026-05-26"
    assert len(df) == 24


def test_load_actuals_keeps_forecast_column(tmp_path):
    p = tmp_path / "actual_load.parquet"
    ts = pd.date_range("2026-05-26T00:00:00Z", periods=24, freq="h", tz="UTC")
    pd.DataFrame({
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "load_mw": [1000.0] * 24,
        "entsoe_forecast_mw": [990.0] * 24,
    }).to_parquet(p, index=False)
    df = charts.load_actuals(p)
    assert "entsoe_forecast_mw" in df.columns
    assert df["entsoe_forecast_mw"].iloc[0] == 990.0


def test_load_submissions_reads_team_csvs(tmp_path):
    team_dir = tmp_path / "submissions" / "team_4"
    team_dir.mkdir(parents=True)
    _sub("2026-05-26").assign(
        timestamp_utc=lambda d: d["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    )[["timestamp_utc", "forecast_mw"]].to_csv(
        team_dir / "2026-05-26.csv", index=False)
    out = charts.load_submissions(tmp_path / "submissions", ["team_4", "ghost"])
    assert set(out) == {"team_4"}             # ghost dir absent -> omitted
    assert "2026-05-26" in out["team_4"]
    assert list(out["team_4"]["2026-05-26"].columns) == ["ts", "forecast_mw"]


# --------------------------------------------------------------------------
# Chart 1: forecast vs actual
# --------------------------------------------------------------------------

def test_forecast_none_when_actuals_missing():
    subs = {"team_4": {"2026-05-26": _sub()}}
    assert charts.fig_forecast_vs_actual(
        None, subs, _scores(), NAMES) is None


def test_forecast_none_when_no_overlapping_day():
    actuals = _actuals("2026-05-26")
    subs = {"team_4": {"2026-05-27": _sub("2026-05-27")}}  # different day
    assert charts.fig_forecast_vs_actual(
        actuals, subs, _scores(), NAMES) is None


def test_forecast_builds_figure_with_actual_and_team_traces():
    actuals = _actuals("2026-05-26")
    subs = {"team_4": {"2026-05-26": _sub("2026-05-26")},
            "hot_rod": {"2026-05-26": _sub("2026-05-26")}}
    fig = charts.fig_forecast_vs_actual(actuals, subs, _scores(), NAMES)
    assert isinstance(fig, go.Figure)
    names = [t.name for t in fig.data]
    assert "Ist-Last" in names
    # MAE pulled into legend label (figure.py-style).
    assert any("Team 4" in n and "MAE" in n for n in names)
    # Single day -> exactly one actual trace, all traces visible.
    assert sum(1 for t in fig.data if t.name == "Ist-Last") == 1
    assert all(t.visible for t in fig.data)


def test_forecast_includes_entsoe_baseline_trace():
    actuals = _actuals("2026-05-26", with_forecast=True)
    subs = {"team_4": {"2026-05-26": _sub("2026-05-26")}}
    fig = charts.fig_forecast_vs_actual(actuals, subs, _scores(), NAMES)
    entsoe = [t for t in fig.data if t.name.startswith("ENTSO-E Prognose")]
    assert len(entsoe) == 1                       # one ENTSO-E baseline trace
    assert entsoe[0].line.dash == "dash"          # distinct dashed style
    assert "MAE" in entsoe[0].name                # MAE in the legend label


def test_forecast_no_entsoe_trace_when_column_absent():
    actuals = _actuals("2026-05-26", with_forecast=False)
    subs = {"team_4": {"2026-05-26": _sub("2026-05-26")}}
    fig = charts.fig_forecast_vs_actual(actuals, subs, _scores(), NAMES)
    assert not any(t.name.startswith("ENTSO-E Prognose") for t in fig.data)


def test_forecast_multi_day_adds_dropdown_and_hides_nondefault():
    actuals = pd.concat([_actuals("2026-05-26", with_forecast=False),
                         _actuals("2026-05-27", with_forecast=False)],
                        ignore_index=True)
    subs = {"team_4": {"2026-05-26": _sub("2026-05-26"),
                       "2026-05-27": _sub("2026-05-27")}}
    fig = charts.fig_forecast_vs_actual(actuals, subs, _scores(), NAMES)
    assert fig.layout.updatemenus, "multi-day chart needs a date dropdown"
    buttons = fig.layout.updatemenus[0].buttons
    assert [b.label for b in buttons] == ["2026-05-26", "2026-05-27"]
    # Default visible = latest day; earlier day hidden (actual + team_4 per day).
    visibilities = [bool(t.visible) for t in fig.data]
    assert visibilities.count(True) == 2
    assert visibilities.count(False) == 2


# --------------------------------------------------------------------------
# Chart 2: mean-MAE bar
# --------------------------------------------------------------------------

def test_mean_mae_bar_none_for_empty():
    assert charts.fig_mean_mae_bar(pd.DataFrame()) is None


def test_mean_mae_bar_builds_horizontal_bar():
    fig = charts.fig_mean_mae_bar(_board())
    assert isinstance(fig, go.Figure)
    assert fig.data[0].type == "bar"
    assert fig.data[0].orientation == "h"
    assert list(fig.data[0].y) == ["Team 4", "Hot Rod"]
    assert fig.layout.yaxis.autorange == "reversed"  # rank 1 on top


# --------------------------------------------------------------------------
# Chart 3: MAE over time
# --------------------------------------------------------------------------

def test_mae_over_time_none_for_empty():
    assert charts.fig_mae_over_time(pd.DataFrame(), NAMES) is None


def test_mae_over_time_one_trace_per_team_with_locf_markers():
    fig = charts.fig_mae_over_time(_scores(), NAMES)
    assert isinstance(fig, go.Figure)
    assert {t.name for t in fig.data} == {"Team 4", "Hot Rod"}
    hot = next(t for t in fig.data if t.name == "Hot Rod")
    # hot_rod's single point is LOCF-carried -> open marker symbol.
    assert "circle-open" in list(hot.marker.symbol)


# --------------------------------------------------------------------------
# Embedding
# --------------------------------------------------------------------------

def test_fig_to_html_none_is_empty_string():
    assert charts.fig_to_html(None, "fig-x") == ""


def test_fig_to_html_uses_stable_div_id_and_no_inline_lib():
    fig = charts.fig_mean_mae_bar(_board(), div_id="fig-leaderboard")
    html = charts.fig_to_html(fig, "fig-leaderboard")
    assert 'id="fig-leaderboard"' in html
    # include_plotlyjs=False -> the library is NOT inlined per figure.
    assert "plotly.js v" not in html
