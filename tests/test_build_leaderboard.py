"""Tests for scripts/build_leaderboard.py."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import build_leaderboard as bl


@pytest.fixture(autouse=True)
def isolate_paths(monkeypatch, tmp_path, repo_root):
    monkeypatch.setattr(bl, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bl, "SCORES_PATH", tmp_path / "data" / "scores.parquet")
    monkeypatch.setattr(bl, "TEAMS_PATH", tmp_path / "teams.yml")
    monkeypatch.setattr(bl, "PUBLIC_DIR", tmp_path / "public")
    # Re-use the real Jinja template (live behaviour) — that's the
    # contract we care about; only inputs/outputs are isolated.
    monkeypatch.setattr(bl, "TEMPLATE_DIR", repo_root / "templates")
    (tmp_path / "data").mkdir()
    yield


def _seed(tmp_path: Path, rows):
    df = pd.DataFrame(rows)
    # Score-Zeilen führen seit den Bias/UPR-Spalten beide Metriken; Fixtures
    # ohne explizite Werte bekommen neutrale Defaults.
    if "bias" not in df.columns:
        df["bias"] = 0.0
    if "upr" not in df.columns:
        df["upr"] = 50.0
    df.to_parquet(tmp_path / "data" / "scores.parquet", index=False)


def _seed_teams(tmp_path: Path):
    import yaml
    (tmp_path / "teams.yml").write_text(yaml.safe_dump({
        "teams": [
            {"id": "team_4", "display_name": "Team 4", "github_handles": []},
            {"id": "hot_rod", "display_name": "Hot Rod", "github_handles": []},
            {"id": "neura", "display_name": "Team Neura", "github_handles": []},
            {"id": "entsoe", "display_name": "ENTSO-E", "pseudo": True},
        ]
    }))


def test_aggregate_ranks_by_mean_mae_then_n_submissions_desc(tmp_path):
    _seed_teams(tmp_path)
    scores = pd.DataFrame([
        {"team_id": "team_4", "mae": 1000.0, "rmse": 1200.0, "mape": 2.0,
         "bias": -500.0, "upr": 60.0},
        {"team_id": "team_4", "mae": 3000.0, "rmse": 3400.0, "mape": 6.0,
         "bias": 1500.0, "upr": 40.0},   # mean 2000, n=2
        {"team_id": "hot_rod", "mae": 2000.0, "rmse": 2500.0, "mape": 4.0,
         "bias": -2000.0, "upr": 100.0},  # mean 2000, n=1
        {"team_id": "neura", "mae": 500.0, "rmse": 700.0, "mape": 1.0,
         "bias": 0.0, "upr": 50.0},       # mean 500, n=1
    ])
    names = bl.load_teams()
    out = bl.aggregate(scores, names)
    assert list(out["team_id"]) == ["neura", "team_4", "hot_rod"]
    # tie at 2000 -> more submissions ranks higher
    assert list(out["rank"]) == [1, 2, 3]
    # Sekundärmetriken: jeweils Mittel der Tageswerte (Bias signiert!).
    assert list(out["mean_rmse"]) == [700.0, 2300.0, 2500.0]
    assert list(out["mean_mape"]) == [1.0, 4.0, 4.0]
    assert list(out["mean_bias"]) == [0.0, 500.0, -2000.0]
    assert list(out["mean_upr"]) == [50.0, 50.0, 100.0]


def test_main_writes_html_and_json(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 200.0,
         "rmse": 200.0, "mape": 0.2},
    ])
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert "Team 4" in html
    assert "Hot Rod" in html
    assert "Mean RMSE [MW]" in html
    assert "Mean MAPE [%]" in html
    assert "Mean Bias [MW]" in html
    assert "UPR [%]" in html
    assert "arxiv.org/abs/2302.11017" in html   # Referenz Möbius et al. 2023
    assert "Sum MAE" not in html
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    assert [r["team_id"] for r in data] == ["team_4", "hot_rod"]
    assert data[0]["rank"] == 1
    assert data[0]["mean_rmse"] == 100.0


def test_daily_breakdown_pivots_by_team_and_date():
    scores = pd.DataFrame([
        {"team_id": "team_4", "target_date": "2026-05-12",
         "mae": 1931.26, "rmse": 2362.61, "mape": 3.66, "carried_forward": False},
        {"team_id": "team_4", "target_date": "2026-05-26",
         "mae": 3466.19, "rmse": 4090.82, "mape": 6.74, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26",
         "mae": 1961.51, "rmse": 2236.25, "mape": 3.77, "carried_forward": True},
    ])
    names = {"team_4": "Team 4", "hot_rod": "Hot Rod"}
    out = bl.daily_breakdown(scores, names, ["hot_rod", "team_4"])
    assert out["dates"] == ["2026-05-12", "2026-05-26"]
    assert [t["team_id"] for t in out["teams"]] == ["hot_rod", "team_4"]
    assert out["teams"][0]["cells"][0]["mae"] is None
    assert out["teams"][0]["cells"][1]["mae"] == 1961.51
    assert out["teams"][0]["cells"][1]["carried"] is True
    assert out["teams"][1]["cells"][0]["mae"] == 1931.26
    assert out["teams"][1]["cells"][1]["carried"] is False
    # Tagesbester je Spalte: 05-12 nur team_4 (best), 05-26 gewinnt hot_rod
    # (auch als LOCF-Wert); None-Zellen sind nie best. RMSE analog.
    assert out["teams"][1]["cells"][0]["best_mae"] is True   # team_4 @ 05-12
    assert out["teams"][0]["cells"][0]["best_mae"] is False  # hot_rod @ 05-12 (None)
    assert out["teams"][0]["cells"][1]["best_mae"] is True   # hot_rod @ 05-26
    assert out["teams"][1]["cells"][1]["best_mae"] is False  # team_4 @ 05-26
    assert out["teams"][1]["cells"][0]["best_rmse"] is True  # team_4 @ 05-12
    assert out["teams"][0]["cells"][1]["best_rmse"] is True  # hot_rod 2236 < 4090
    assert out["teams"][1]["cells"][1]["best_rmse"] is False
    # Zeilen ohne bias/upr-Spalten -> None-Zellen, nie best.
    assert out["teams"][1]["cells"][0]["bias"] is None
    assert out["teams"][1]["cells"][0]["best_bias"] is False
    # Anzeige-Metadaten: deutsches Label + ISO-Woche (05-12/05-26 = Dienstage).
    assert out["date_meta"][0]["label"] == "Di, 12.5.26"
    assert out["date_meta"][1]["label"] == "Di, 26.5.26"
    assert out["date_meta"][0]["week"] == "2026-W20"
    assert out["date_meta"][1]["week"] == "2026-W22"


def test_daily_breakdown_best_bias_and_upr_use_ideal_value():
    # Bias: bester = am nächsten an 0 (|+50| < |-100|).
    # UPR: bester = am nächsten an 50 % (|70-50| < |10-50|).
    scores = pd.DataFrame([
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 1.0,
         "rmse": 1.0, "mape": 1.0, "bias": -100.0, "upr": 10.0,
         "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 2.0,
         "rmse": 2.0, "mape": 2.0, "bias": 50.0, "upr": 70.0,
         "carried_forward": False},
    ])
    out = bl.daily_breakdown(scores, {}, ["team_4", "hot_rod"])
    by_id = {t["team_id"]: t["cells"][0] for t in out["teams"]}
    assert by_id["hot_rod"]["best_bias"] is True
    assert by_id["team_4"]["best_bias"] is False
    assert by_id["hot_rod"]["best_upr"] is True
    assert by_id["team_4"]["best_upr"] is False


def test_daily_breakdown_marks_ties_as_best():
    scores = pd.DataFrame([
        {"team_id": "team_4", "target_date": "2026-05-26",
         "mae": 100.0, "rmse": 1.0, "mape": 1.0, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26",
         "mae": 100.0, "rmse": 1.0, "mape": 1.0, "carried_forward": False},
    ])
    out = bl.daily_breakdown(scores, {}, ["team_4", "hot_rod"])
    assert all(t["cells"][0]["best_mae"] for t in out["teams"])
    assert all(t["cells"][0]["best_rmse"] for t in out["teams"])


def test_main_writes_daily_section_in_html(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-12", "mae": 1931.26,
         "rmse": 2362.61, "mape": 3.66, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 1961.51,
         "rmse": 2236.25, "mape": 3.77, "carried_forward": False},
    ])
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    for metric in ("MAE", "RMSE", "MAPE", "Bias", "UPR"):
        assert f"Tagesfehler je Team [{metric}]" in html
    # Spalten-Header: deutsches Kurzformat mit Wochentag statt ISO-Datum.
    assert "Di, 12.5.26" in html
    assert "Di, 26.5.26" in html
    # Wochen-Umschalter-Markup + data-week-Attribute vorhanden.
    assert 'class="week-nav"' in html
    assert 'data-week="2026-W20"' in html
    assert 'data-week="2026-W22"' in html
    # Tagesbeste fett: je Spalte genau eine best-Zelle, in allen 5 Tabellen
    # (hier 2 Spalten mit jeweils genau einem Wert -> 5 x 2). Mit
    # data-week-Suffix im Tag, daher Prefix-Zählung über beide Varianten.
    assert html.count('class="num best"') == 10
    daily = json.loads((tmp_path / "public" / "data" / "daily.json").read_text())
    assert daily["dates"] == ["2026-05-12", "2026-05-26"]
    assert daily["date_meta"][0]["week"] == "2026-W20"


def test_main_handles_empty_scores(tmp_path):
    _seed_teams(tmp_path)
    # SCORES_PATH does not exist -> main must still render an (empty) page.
    bl.main()
    assert (tmp_path / "public" / "index.html").exists()
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    assert data == []


# --------------------------------------------------------------------------
# Charts: embedded, with graceful degradation for the actuals-dependent one.
# --------------------------------------------------------------------------

def _write_actuals(tmp_path, date: str):
    ts = pd.date_range(f"{date}T00:00:00Z", periods=24, freq="h", tz="UTC")
    pd.DataFrame({
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "load_mw": [1000.0 + i for i in range(24)],
        "entsoe_forecast_mw": [990.0 + i for i in range(24)],
    }).to_parquet(tmp_path / "data" / "actual_load.parquet", index=False)


def _write_submission(tmp_path, team: str, date: str):
    d = tmp_path / "submissions" / team
    d.mkdir(parents=True, exist_ok=True)
    ts = pd.date_range(f"{date}T00:00:00Z", periods=24, freq="h", tz="UTC")
    pd.DataFrame({
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forecast_mw": [1010.0 + i for i in range(24)],
    }).to_csv(d / f"{date}.csv", index=False)


def test_main_embeds_charts_but_not_forecast_without_actuals(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 200.0,
         "rmse": 200.0, "mape": 0.2, "carried_forward": False},
    ])
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert 'id="fig-leaderboard"' in html
    assert 'id="fig-leaderboard-rmse"' in html
    assert "Mittlerer RMSE je Team" in html
    # Balkendiagramme für alle Sekundärmetriken, in Spaltenreihenfolge.
    assert 'id="fig-leaderboard-mape"' in html
    assert 'id="fig-leaderboard-bias"' in html
    assert 'id="fig-leaderboard-upr"' in html
    assert html.index("Mittlere MAPE je Team") < html.index(
        "Mittlerer Bias je Team") < html.index("Mittlere UPR je Team")
    assert 'id="fig-mae-time"' in html
    assert 'id="fig-forecast"' not in html       # no actuals -> self-disabled
    assert "Prognose vs. Ist-Last" not in html
    # The Plotly library bundle is embedded exactly once.
    assert html.count("plotly.js v") == 1
    # Layout: the Leaderboard table comes before the charts.
    assert html.index("<h2>Leaderboard</h2>") < html.index("Mittlere MAE je Team")


def test_main_renders_forecast_chart_when_actuals_present(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
    ])
    _write_actuals(tmp_path, "2026-05-26")
    _write_submission(tmp_path, "team_4", "2026-05-26")
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert 'id="fig-forecast"' in html
    assert "Prognose vs. Ist-Last" in html
    # ENTSO-E day-ahead forecast plotted as a baseline trace.
    assert "ENTSO-E Prognose" in html
    # Calendar date picker shipped alongside the forecast figure (kept
    # hidden by its JS when only one day is available).
    assert 'id="cal-forecast"' in html
    assert 'id="cal-forecast-grid"' in html
    # Layout: the Leaderboard table is above the forecast chart.
    assert html.index("<h2>Leaderboard</h2>") < html.index("Prognose vs. Ist-Last")


def test_main_empty_scores_does_not_embed_plotly_bundle(tmp_path):
    _seed_teams(tmp_path)
    bl.main()  # no scores -> no charts -> no 4.8 MB bundle
    html = (tmp_path / "public" / "index.html").read_text()
    assert "plotly.js v" not in html


def test_load_logo_uri_missing_is_empty(tmp_path):
    assert bl.load_logo_uri(tmp_path / "logo" / "spotlogo.png") == ""


def test_main_embeds_logo_when_present(tmp_path):
    _seed_teams(tmp_path)
    (tmp_path / "logo").mkdir()
    (tmp_path / "logo" / "spotlogo.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert "data:image/png;base64," in html
    assert 'class="hero-logo"' in html


def test_main_omits_logo_when_absent(tmp_path):
    _seed_teams(tmp_path)
    bl.main()  # no logo file under tmp_path -> hero renders without it
    html = (tmp_path / "public" / "index.html").read_text()
    # The CSS rule `.hero-logo {` is always present; the <img> tag is not.
    assert 'class="hero-logo"' not in html
    assert "data:image/png;base64," not in html


# --------------------------------------------------------------------------
# ENTSO-E day-ahead forecast as the ranked pseudo-team `entsoe`.
# --------------------------------------------------------------------------

def _actuals_frame(date: str = "2026-05-26", with_forecast: bool = True,
                   hours: int = 24) -> pd.DataFrame:
    ts = pd.date_range(f"{date}T00:00:00Z", periods=hours, freq="h", tz="UTC")
    data = {
        "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "load_mw": [1000.0] * hours,
    }
    if with_forecast:
        data["entsoe_forecast_mw"] = [1100.0] * hours   # constant error of 100
    return pd.DataFrame(data)


def test_entsoe_pseudo_scores_computes_mae():
    out = bl.entsoe_pseudo_scores(_actuals_frame("2026-05-26"), {"2026-05-26"})
    assert list(out["team_id"]) == ["entsoe"]
    assert out["target_date"].iloc[0] == "2026-05-26"
    assert out["mae"].iloc[0] == 100.0          # |1100 - 1000|
    # Bias/UPR wie bei den Teams: konstante Überprognose von +100 MW.
    assert out["bias"].iloc[0] == 100.0
    assert out["upr"].iloc[0] == 0.0
    assert not bool(out["carried_forward"].iloc[0])


def test_entsoe_pseudo_none_without_forecast_column():
    df = _actuals_frame("2026-05-26", with_forecast=False)
    assert bl.entsoe_pseudo_scores(df, {"2026-05-26"}).empty


def test_entsoe_pseudo_none_when_actuals_missing():
    assert bl.entsoe_pseudo_scores(None, {"2026-05-26"}).empty


def test_entsoe_pseudo_omits_incomplete_scored_day(capsys):
    df = _actuals_frame("2026-05-26", hours=10)   # partial day
    assert bl.entsoe_pseudo_scores(df, {"2026-05-26"}).empty
    assert "übersprungen" in capsys.readouterr().out   # logged, not silent


def test_entsoe_pseudo_restricted_to_scored_dates():
    # Sync rule: actuals carry TWO complete days, but only one is a scored
    # target_date of the regular teams -> entsoe gets exactly that one day.
    df = pd.concat([_actuals_frame("2026-05-26"), _actuals_frame("2026-05-27")],
                   ignore_index=True)
    out = bl.entsoe_pseudo_scores(df, {"2026-05-26"})
    assert list(out["target_date"]) == ["2026-05-26"]


def test_load_pseudo_ids_reads_flag(tmp_path):
    _seed_teams(tmp_path)
    assert bl.load_pseudo_ids() == {"entsoe"}


def test_entsoe_pseudo_authoritative_overrides_parquet(tmp_path):
    # A persisted entsoe row in scores.parquet (e.g. from a historic CSV
    # submission) must be IGNORED — the derived value wins.
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
        {"team_id": "entsoe", "target_date": "2026-05-26", "mae": 42.0,
         "rmse": 42.0, "mape": 0.04, "carried_forward": True},
    ])
    _write_actuals(tmp_path, "2026-05-26")        # forecast = load-10 -> MAE 10
    bl.main()
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    entsoe = next(r for r in data if r["team_id"] == "entsoe")
    assert float(entsoe["mean_mae"]) == 10.0      # derived, not the stale 42
    daily = json.loads((tmp_path / "public" / "data" / "daily.json").read_text())
    cell = next(t for t in daily["teams"] if t["team_id"] == "entsoe")["cells"][0]
    assert cell["carried"] is False               # never LOCF for the pseudo-team


def test_main_ranks_entsoe_pseudo_in_leaderboard(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
    ])
    _write_actuals(tmp_path, "2026-05-26")        # carries entsoe_forecast_mw
    bl.main()
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    assert "entsoe" in [r["team_id"] for r in data]
    assert "ENTSO-E" in [r["display_name"] for r in data]
