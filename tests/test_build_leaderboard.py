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


def test_annotate_leaderboard_best_marks_columnwise_winners():
    rows = [
        {"team_id": "a", "mean_mae": 100.0, "mean_rmse": 300.0,
         "mean_mape": 2.0, "mean_bias": -80.0, "mean_upr": 45.0,
         "n_submissions": 9},
        {"team_id": "b", "mean_mae": 200.0, "mean_rmse": 250.0,
         "mean_mape": 2.0, "mean_bias": 30.0, "mean_upr": 10.0,
         "n_submissions": 9},
    ]
    out = bl.annotate_leaderboard_best(rows)
    a, b = out[0], out[1]
    assert a["best_mae"] and not b["best_mae"]            # 100 < 200
    assert b["best_rmse"] and not a["best_rmse"]          # 250 < 300
    assert a["best_mape"] and b["best_mape"]              # Gleichstand 2.0
    assert b["best_bias"] and not a["best_bias"]          # |30| < |-80|
    assert a["best_upr"] and not b["best_upr"]            # |45-50| < |10-50|
    assert a["best_days"] and b["best_days"]              # Gleichstand max 9


def test_main_writes_html_and_json(tmp_path):
    _seed_teams(tmp_path)
    # Zieltage >= RESTART_DATE -> Live-Leaderboard (scores.json = Live-Wertung).
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-06-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1},
        {"team_id": "hot_rod", "target_date": "2026-06-26", "mae": 200.0,
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
    # (hier 2 Spalten mit jeweils genau einem Wert -> 5 x 2). Nur den
    # Tagesfehler-Teil zählen — beide Leaderboards haben eigene best-Zellen
    # (die Testphase steht direkt darunter).
    daily_html = html[html.index("Tagesfehler je Team [MAE]"):
                      html.index("<h2>Leaderboard Test Phase</h2>")]
    assert daily_html.count('class="num best"') == 10
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
    # Zieltage >= RESTART_DATE -> die „Mittlere … je Team"-Balken (Live-Board)
    # sind befüllt; vor dem Neustart wären sie leer und die Sektionen entfielen.
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-06-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-06-26", "mae": 200.0,
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
# "About the Models" section — driven by `model_card_link` in teams.yml.
# --------------------------------------------------------------------------

ZIP_URL = "https://example.org/team4-repro.zip"


def _seed_teams_with_model_card(tmp_path: Path, url: str):
    import yaml
    (tmp_path / "teams.yml").write_text(yaml.safe_dump({
        "teams": [
            {"id": "team_4", "display_name": "Team 4", "github_handles": [],
             "model_card_link": url, "software_link": ZIP_URL},
            {"id": "hot_rod", "display_name": "Hot Rod",
             "github_handles": []},   # no links -> warning + dash
        ]
    }))


def test_load_model_cards_lists_all_regular_teams(tmp_path):
    url = "https://example.org/MODEL_CARD.md"
    _seed_teams_with_model_card(tmp_path, url)
    # One row per regular team, file order; missing links -> None.
    assert bl.load_model_cards() == [
        {"display_name": "Team 4", "model_card_link": url,
         "software_link": ZIP_URL, "certified": None, "openssf": None},
        {"display_name": "Hot Rod", "model_card_link": None,
         "software_link": None, "certified": None, "openssf": None},
    ]


def test_load_model_cards_skips_pseudo_teams(tmp_path):
    _seed_teams(tmp_path)  # includes pseudo team "ENTSO-E"
    names = [mc["display_name"] for mc in bl.load_model_cards()]
    assert "ENTSO-E" not in names
    assert names == ["Team 4", "Hot Rod", "Team Neura"]


def test_main_renders_model_cards_section(tmp_path):
    url = "https://example.org/MODEL_CARD.md"
    _seed_teams_with_model_card(tmp_path, url)
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    assert "About the Models" in html
    assert f'<a href="{url}">' in html
    # Section appears below all metric sections, above the footer.
    assert html.rindex("About the Models") > html.rindex("Leaderboard")
    assert html.rindex("About the Models") < html.rindex("<footer>")


def test_main_flags_teams_without_model_card(tmp_path):
    url = "https://example.org/MODEL_CARD.md"
    _seed_teams_with_model_card(tmp_path, url)
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    # Hot Rod has no link -> warning entry instead of an anchor.
    section = html[html.index("About the Models"):html.index("<footer>")]
    assert '<td class="card-missing">⚠️ missing</td>' in section
    assert section.count("<tr>") == 1 + 2  # header + one row per team


def test_main_renders_software_column(tmp_path):
    url = "https://example.org/MODEL_CARD.md"
    _seed_teams_with_model_card(tmp_path, url)
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    section = html[html.index("About the Models"):html.index("<footer>")]
    # Column order: Team, Model Card, Software.
    head = section[section.index("<thead>"):section.index("</thead>")]
    assert (head.index(">Team<") < head.index(">Model Card<")
            < head.index(">Software<"))
    # Team 4 has a software_link -> ZIP anchor; Hot Rod -> dash, NO warning
    # (Software ist freiwillig, nur die Model Card wird angemahnt).
    row4 = section[section.index("<td>Team 4</td>"):]
    row4 = row4[:row4.index("</tr>")]
    assert f'<a href="{ZIP_URL}">ZIP</a>' in row4
    rowhr = section[section.index("<td>Hot Rod</td>"):]
    rowhr = rowhr[:rowhr.index("</tr>")]
    assert ZIP_URL not in rowhr
    assert '<td class="software na">—</td>' in rowhr
    # Software adds no warning; the row's warnings come from the missing
    # Model Card and the missing OpenSSF scorecard.
    assert rowhr.count("⚠️") == 2


def _seed_teams_with_certified(tmp_path: Path):
    import yaml
    (tmp_path / "teams.yml").write_text(yaml.safe_dump({
        "teams": [
            {"id": "team_4", "display_name": "Team 4", "github_handles": [],
             "certified": "Yes"},
            {"id": "hot_rod", "display_name": "Hot Rod", "github_handles": [],
             "certified": "No"},
            {"id": "neura", "display_name": "Team Neura",
             "github_handles": []},   # certified key absent
        ]
    }))


def test_main_renders_certified_column(tmp_path):
    _seed_teams_with_certified(tmp_path)
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    section = html[html.index("About the Models"):html.index("<footer>")]
    # Column order: Certified sits after Software.
    head = section[section.index("<thead>"):section.index("</thead>")]
    assert head.index(">Software<") < head.index(">Certified<")
    # "Yes" -> check mark; "No" and missing -> dash, never a check mark.
    row4 = section[section.index("<td>Team 4</td>"):]
    row4 = row4[:row4.index("</tr>")]
    assert "✅" in row4
    rowhr = section[section.index("<td>Hot Rod</td>"):]
    rowhr = rowhr[:rowhr.index("</tr>")]
    assert "✅" not in rowhr and '<td class="status na"' in rowhr
    rown = section[section.index("<td>Team Neura</td>"):]
    rown = rown[:rown.index("</tr>")]
    assert "✅" not in rown and '<td class="status na"' in rown


def test_main_renders_openssf_column(tmp_path):
    import yaml
    scorecard = "https://scorecard.dev/viewer/?uri=github.com/x/y"
    (tmp_path / "teams.yml").write_text(yaml.safe_dump({
        "teams": [
            {"id": "team_4", "display_name": "Team 4", "github_handles": [],
             "openssf": scorecard},
            {"id": "hot_rod", "display_name": "Hot Rod",
             "github_handles": []},   # no openssf -> missing + warning
        ]
    }))
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    section = html[html.index("About the Models"):html.index("<footer>")]
    # OpenSSF column sits after Certified.
    head = section[section.index("<thead>"):section.index("</thead>")]
    assert head.index(">Certified<") < head.index(">OpenSSF<")
    # Team 4 has a scorecard link; Hot Rod -> missing warning.
    row4 = section[section.index("<td>Team 4</td>"):]
    row4 = row4[:row4.index("</tr>")]
    assert f'<a href="{scorecard}">' in row4
    rowhr = section[section.index("<td>Hot Rod</td>"):]
    rowhr = rowhr[:rowhr.index("</tr>")]
    assert scorecard not in rowhr
    assert '<td class="card-missing">⚠️ missing</td>' in rowhr


def test_main_emits_certificate_template(tmp_path):
    # The build copies templates/Certificate.md verbatim into public/ so the
    # "About the Models" footnote can link to ./Certificate.md.
    _seed_teams_with_certified(tmp_path)
    bl.main()
    published = tmp_path / "public" / "Certificate.md"
    assert published.exists()
    source = bl.TEMPLATE_DIR / "Certificate.md"
    assert published.read_text() == source.read_text()


def test_about_the_models_links_certificate(tmp_path):
    _seed_teams_with_certified(tmp_path)
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    section = html[html.index("About the Models"):html.index("<footer>")]
    assert 'href="./Certificate.md"' in section


# --------------------------------------------------------------------------
# Leaderboard "Status" column — same source as "About the Models". Green
# only if ALL three artifacts are present: Model Card, Software, Certified.
# --------------------------------------------------------------------------

def _seed_teams_with_artifacts(tmp_path: Path, url: str):
    import yaml
    (tmp_path / "teams.yml").write_text(yaml.safe_dump({
        "teams": [
            {"id": "team_4", "display_name": "Team 4", "github_handles": [],
             "model_card_link": url, "software_link": ZIP_URL,
             "certified": "Yes"},     # alle drei Artefakte -> Haken
            {"id": "hot_rod", "display_name": "Hot Rod",
             "github_handles": []},   # nichts -> Warnung
        ]
    }))


def test_load_artifact_status_requires_all_three(tmp_path):
    import yaml
    url = "https://example.org/MODEL_CARD.md"
    full = {"model_card_link": url, "software_link": ZIP_URL,
            "certified": "Yes"}
    teams = [{"id": "all", "display_name": "All", "github_handles": [],
              **full}]
    # Je ein Team, dem genau ein Artefakt fehlt -> immer Warnung.
    for missing in full:
        partial = {k: v for k, v in full.items() if k != missing}
        teams.append({"id": f"no_{missing}", "display_name": missing,
                      "github_handles": [], **partial})
    teams.append({"id": "cert_no", "display_name": "CertNo",
                  "github_handles": [], **{**full, "certified": "No"}})
    (tmp_path / "teams.yml").write_text(yaml.safe_dump({"teams": teams}))
    status = bl.load_artifact_status()
    assert status["all"] is True
    assert status["no_model_card_link"] is False
    assert status["no_software_link"] is False
    assert status["no_certified"] is False
    assert status["cert_no"] is False


def test_load_artifact_status_pseudo_is_none(tmp_path):
    _seed_teams(tmp_path)  # includes pseudo team "entsoe"
    status = bl.load_artifact_status()
    assert status["entsoe"] is None
    assert status["team_4"] is False


def test_main_leaderboard_status_column(tmp_path):
    url = "https://example.org/MODEL_CARD.md"
    _seed_teams_with_artifacts(tmp_path, url)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1},
        {"team_id": "hot_rod", "target_date": "2026-05-26", "mae": 200.0,
         "rmse": 200.0, "mape": 0.2},
    ])
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    table = html[html.index('<table class="ranking">'):]
    table = table[:table.index("</table>")]
    # Header: Status sits between Team and Mean MAE.
    head = table[:table.index("</thead>")]
    assert head.index(">Team<") < head.index(">Status<") < head.index("Mean MAE")
    # Team 4 has all three artifacts -> check mark; Hot Rod none -> warning.
    row4 = table[table.index("<td>Team 4</td>"):]
    row4 = row4[:row4.index("</tr>")]
    assert "✅" in row4 and "⚠️" not in row4
    rowhr = table[table.index("<td>Hot Rod</td>"):]
    rowhr = rowhr[:rowhr.index("</tr>")]
    assert "⚠️" in rowhr and "✅" not in rowhr


def test_main_leaderboard_status_warns_without_certified(tmp_path):
    # Model Card + Software allein reichen nicht mehr: ohne
    # certified == "Yes" zeigt die Status-Spalte das Warn-Icon.
    url = "https://example.org/MODEL_CARD.md"
    _seed_teams_with_model_card(tmp_path, url)   # team_4: Card+ZIP, kein Cert
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1},
    ])
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    table = html[html.index('<table class="ranking">'):]
    table = table[:table.index("</table>")]
    row4 = table[table.index("<td>Team 4</td>"):]
    row4 = row4[:row4.index("</tr>")]
    assert "⚠️" in row4 and "✅" not in row4


def test_main_leaderboard_status_dash_for_pseudo(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-05-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
    ])
    _write_actuals(tmp_path, "2026-05-26")        # ranks the pseudo-team
    bl.main()
    html = (tmp_path / "public" / "index.html").read_text()
    table = html[html.index('<table class="ranking">'):]
    table = table[:table.index("</table>")]
    row = table[table.index("<td>ENTSO-E</td>"):]
    row = row[:row.index("</tr>")]
    assert '<td class="status na"' in row     # dash, not a warning
    assert "⚠️" not in row and "✅" not in row


# --------------------------------------------------------------------------
# Live phase: only teams with a FRESH submission since RESTART_DATE — LOCF
# carry-forward rows alone do not qualify (clean cut vs. the test phase).
# --------------------------------------------------------------------------

def test_filter_live_teams_drops_locf_only_teams():
    scores = pd.DataFrame([
        {"team_id": "team_4", "target_date": "2026-06-10", "mae": 100.0,
         "carried_forward": False},
        {"team_id": "team_4", "target_date": "2026-06-11", "mae": 120.0,
         "carried_forward": True},   # LOCF eines Live-Teams bleibt (Strafe)
        {"team_id": "team_4_optuna", "target_date": "2026-06-10", "mae": 90.0,
         "carried_forward": True},   # nur LOCF -> raus aus der Live-Wertung
    ])
    out = bl.filter_live_teams(scores)
    assert set(out["team_id"]) == {"team_4"}
    assert len(out) == 2


def test_filter_live_teams_missing_column_or_nan_counts_as_fresh():
    no_col = pd.DataFrame([{"team_id": "team_4",
                            "target_date": "2026-06-10", "mae": 100.0}])
    assert bl.filter_live_teams(no_col).equals(no_col)
    nan_row = pd.DataFrame([{"team_id": "entsoe",
                             "target_date": "2026-06-10", "mae": 100.0,
                             "carried_forward": None}])
    assert list(bl.filter_live_teams(nan_row)["team_id"]) == ["entsoe"]


def test_main_live_board_excludes_locf_only_team(tmp_path):
    _seed_teams(tmp_path)
    _seed(tmp_path, [
        # Testphase: beide Teams mit frischen Einreichungen.
        {"team_id": "team_4", "target_date": "2026-06-07", "mae": 150.0,
         "rmse": 150.0, "mape": 0.15, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-06-07", "mae": 250.0,
         "rmse": 250.0, "mape": 0.25, "carried_forward": False},
        # Live-Phase: team_4 frisch, hot_rod nur per LOCF fortgeschrieben.
        {"team_id": "team_4", "target_date": "2026-06-10", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
        {"team_id": "hot_rod", "target_date": "2026-06-10", "mae": 200.0,
         "rmse": 200.0, "mape": 0.2, "carried_forward": True},
    ])
    bl.main()
    # Live-Wertung (scores.json = Live-Board): nur team_4 — hot_rod hat ab
    # RESTART_DATE keine frische Einreichung.
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    assert [r["team_id"] for r in data] == ["team_4"]
    # Volle Historie unverändert: hot_rod bleibt in den Tagesfehler-Tabellen
    # und im eingefrorenen Testphasen-Board.
    daily = json.loads((tmp_path / "public" / "data" / "daily.json").read_text())
    assert "hot_rod" in [t["team_id"] for t in daily["teams"]]
    html = (tmp_path / "public" / "index.html").read_text()
    test_board = html[html.index("Leaderboard Test Phase"):]
    assert "Hot Rod" in test_board


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
        {"team_id": "team_4", "target_date": "2026-06-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
        {"team_id": "entsoe", "target_date": "2026-06-26", "mae": 42.0,
         "rmse": 42.0, "mape": 0.04, "carried_forward": True},
    ])
    _write_actuals(tmp_path, "2026-06-26")        # forecast = load-10 -> MAE 10
    bl.main()
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    entsoe = next(r for r in data if r["team_id"] == "entsoe")
    assert float(entsoe["mean_mae"]) == 10.0      # derived, not the stale 42
    daily = json.loads((tmp_path / "public" / "data" / "daily.json").read_text())
    cell = next(t for t in daily["teams"] if t["team_id"] == "entsoe")["cells"][0]
    assert cell["carried"] is False               # never LOCF for the pseudo-team


def test_main_ranks_entsoe_pseudo_in_leaderboard(tmp_path):
    _seed_teams(tmp_path)
    # Zieltag >= RESTART_DATE -> entsoe-Pseudo-Team im Live-Leaderboard.
    _seed(tmp_path, [
        {"team_id": "team_4", "target_date": "2026-06-26", "mae": 100.0,
         "rmse": 100.0, "mape": 0.1, "carried_forward": False},
    ])
    _write_actuals(tmp_path, "2026-06-26")        # carries entsoe_forecast_mw
    bl.main()
    data = json.loads((tmp_path / "public" / "data" / "scores.json").read_text())
    assert "entsoe" in [r["team_id"] for r in data]
    assert "ENTSO-E" in [r["display_name"] for r in data]
