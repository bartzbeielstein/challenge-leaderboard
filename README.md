# challenge-leaderboard

Automatisierte Bewertung der Live-Lastprognose-Challenge der Vorlesungen
*Sicherheitskritische Zeitreihenprognose mit spotforecast2-safe*
(Numerische Mathematik / DDMO, SoSe 2026, TH Köln, Bartz-Beielstein).

Webseite Leaderboard: https://bartzbeielstein.github.io/challenge-leaderboard/

Webseite ENTSO-E: https://transparency.entsoe.eu

Die Spielregeln stehen in Kapitel 12 des Skripts (`lecture/12_challenge.qmd`).
Dieses Repo ist die
*Bewertungs-Infrastruktur*: hier reichen Teams ihre täglichen
Vorhersagen ein, hier läuft das Scoring, hier wird das Leaderboard
gebaut und auf GitHub Pages publiziert.

## Setup für Teams (einmalig)

1. Forken Sie dieses Repo.
2. Lokal clonen, `pyproject.toml` per `uv sync` installieren.
3. Submission lokal erzeugen (siehe `make_submission.py` in Kapitel 12).
4. Für jede Submission: Feature-Branch → `submissions/<team_id>/<D>.csv`
   commiten → PR gegen `main` → automatischer Merge bei grünem Check.


### Sicherheitsmodell der PR-Pipeline

`validate-pr.yml` läuft unter `pull_request` und führt damit (bei
Fork-PRs) ungeprüften Team-Code aus — deshalb bekommt es **bewusst keine
Secrets** und kein Schreib-Token. Das Mergen erledigt das separate
`auto-merge.yml` per `workflow_run` im vertrauenswürdigen Basis-Repo-Kontext
(ohne PR-Code auszuführen) mit einem kurzlebigen **GitHub-App-Token**, und
nur für grün validierte PRs mit genau einer `submissions/**`-Datei. Setup:
siehe `DEPLOYMENT.md` Abschnitt 4a.

## Tageslauf-Timing & Robustheit

Der Score-Cron läuft *täglich* und bewertet „gestern" (UTC).
ENTSO-E veröffentlicht *Actual Total Load* (6.1.A) regulatorisch bis H+1,
real treten jedoch TSO-Verzögerungen, einzelne fehlende Stunden (DST) und
„HTTP 200 + No matching data" auf. 09:00 UTC gibt Sicherheitsmarge nach der
H+1-Frist der letzten UTC-Stunde (01:00 UTC). Ergänzend härtet
`score_day.py` den Abruf:

- **Retry/Backoff** bei transienten API-/Netzfehlern.
- **Sauberes Aufschieben** (`GroundTruthNotReady`) bei unvollständigem Tag —
  lieber morgen via **Catch-up** nachholen als raten (CR-3).
- **Lauter Fehlschlag**: kann der *primäre* Zieltag nicht gescort werden,
  endet der Lauf rot (Alarm); Nebentage werden still nachgeholt.

## `teams.yml`-Schema

```yaml
teams:
  - id: team_lambda                   # filename-safe, lowercase
    display_name: "Team Lambda"
    github_handles:
      - alice42
      - bob99
      - carol7
```

Nur Personen aus `github_handles` dürfen PRs für dieses Team mergen
(via `validate-pr.yml`-Check).

## Score-Logik

- *Primär*: MAE [MW] über die 24 Stunden eines Zieltages.
- *Aggregat (öffentliches Ranking)*: mittlere MAE = Summe der
  Tages-MAEs / Anzahl bewerteter Tage (aufsteigend).
- *LOCF*: Reicht ein Team an einem Zieltag keine Prognose ein, wird
  die jeweils letzte vorhandene Submission des Teams fortgeschrieben
  (last observation carried forward) und zählt als bewerteter Tag.
- *Tie-Break*: Anzahl bewerteter Tage (absteigend).
- *Sekundärmetriken (Anzeige, kein Ranking)*: mittlere RMSE, mittlere
  MAPE (De-facto-Referenzmetrik der Lastprognose-Praxis, vgl. Hong,
  Pinson & Fan 2016, IJF), **Mean Bias** = Ø(Prognose − Ist) (signiert;
  negativ = systematische Unterprognose) und **UPR** (Under-Prediction
  Rate) = Anteil der Stunden mit Prognose < Ist. Bias/UPR machen die im
  Netzbetrieb teurere Unterprognose-Richtung sichtbar, die
  vorzeichenblinde Metriken verbergen — die offiziellen
  ENTSO-E-Day-ahead-Lastprognosen sind nachweislich systematisch
  verzerrt: Möbius, Watermeyer, Grothe & Müsgens (2023), *Enhancing
  Energy System Models Using Better Load Forecasts*,
  [arXiv:2302.11017](https://arxiv.org/abs/2302.11017).

### Pseudo-Team ENTSO-E

Die offizielle ENTSO-E-Day-ahead-Prognose nimmt als **Pseudo-Team**
`entsoe` (teams.yml: `pseudo: true`) am Ranking teil — in allen Tabellen
und Figuren. Seine Scores werden **nicht** über `submissions/`-CSVs
eingereicht, sondern zur Build-Zeit direkt aus den committeten
ENTSO-E-Daten abgeleitet (`data/actual_load.parquet`, Spalte
`entsoe_forecast_mw` → `entsoe_pseudo_scores()` in
`scripts/build_leaderboard.py`). Bewertet wird exakt der Zeitraum der
regulären Teams (erster bis letzter gescorter Zieltag, synchron); CSV-
Submissions für Pseudo-Teams lehnt `validate_submission.py` ab, und
`score_day.py` schließt sie vom täglichen Scoring aus.

Details und die Formeln in `lecture/12_challenge.qmd` (§
"Bewertungsmethodik im Detail").

## Visualisierung des Leaderboards

Zusätzlich zu den Tabellen rendert `scripts/build_leaderboard.py`
interaktive Plotly-Diagramme auf der GitHub-Pages-Seite (Figuren in
`scripts/charts.py`):

- **Prognose vs. Ist-Last** — pro Zieltag (Kalender rechts neben dem
  Plot) die 24-h-Prognose jedes Teams gegen die gemessene DE-Netzlast
  (ENTSO-E *Actual Total Load*), MAE je Team in der Legende.
- **Mittlere MAE je Team** — horizontales Balkendiagramm (grün = gut → rot).
- **Mittlerer RMSE / MAPE / Bias / UPR je Team** — analoge Balkendiagramme
  für jede Leaderboard-Spalte, je mit Erklärung. RMSE/MAPE: kleiner =
  besser; Bias (signiert) und UPR sortieren nach Distanz zum Idealwert
  (0 MW bzw. 50 %, gepunktete Referenzlinie). Anzeige-Metriken — das
  Ranking bleibt bei der MAE.
- **MAE-Verlauf** — Tages-MAE je Team über die Zeit; offene Marker
  kennzeichnen via LOCF fortgeschriebene Tage.
- **Tagesfehler je Team [MAE] / [RMSE] / [MAPE] / [Bias] / [UPR]** —
  Tabellen mit Tageswerten, Wochen-Umschalter (Default: jüngste Woche)
  und deutschen Datums-Headern („Di, 26.5.26"); fett = Tagesbester
  (Spaltenminimum bzw. am nächsten an 0/50 % für Bias/UPR).

Plotly.js ist einmalig in `index.html` eingebettet (self-contained,
offline-fähig, via `uv.lock` gepinnt; deterministische `div_id`s → CR-2).
Der Build läuft auf GitHub **ohne** API-Key und liest ausschließlich
committete Dateien.

### Ist-Last-Daten aktualisieren

Das Prognose-vs-Ist-Last-Diagramm (und das Pseudo-Team `entsoe`) braucht
die gemessene Netzlast samt Day-ahead-Prognose als committete Zeitreihe
(`data/actual_load.parquet`). Seit 2026-06-04 hält der tägliche
`Daily Scoring`-Workflow diese Datei **automatisch** aktuell
(`scripts/fetch_actuals.py` läuft nach dem Scoring mit dem
`ENTSOE_API_KEY`-Secret und committet das Ergebnis im selben Score-PR).

Ein lokaler Lauf ist nur noch als **Fallback/Backfill** nötig
(`ENTSOE_API_KEY` in der Umgebung):

```bash
uv run python scripts/fetch_actuals.py                  # alle relevanten Tage (Default)
uv run python scripts/fetch_actuals.py --from 2026-05-26 --to 2026-06-01
uv run python scripts/fetch_actuals.py --force          # bereits vollständige Tage neu laden
```

Anschließend `data/actual_load.parquet` per PR nach `main` bringen
(Admin-Merge — der PR berührt keine `submissions/**`-Datei, wird also von
`validate-pr.yml` pass-through abgenickt, aber bewusst **nicht**
auto-gemerged). Details: DEPLOYMENT.md, Abschnitt „Laufender Betrieb".

Das Skript nutzt dieselbe Download-Logik wie das Scoring
(`score_day.fetch_ground_truth`): **Retry/Backoff** bei transienten
Fehlern, **sauberes Aufschieben** noch unveröffentlichter Tage und
**Überspringen** bereits vollständig geladener Tage. Fehlt die Datei,
blendet der Build das Prognose-vs-Ist-Last-Diagramm sauber aus — die
übrigen Charts und Tabellen bleiben erhalten.

## Reproduzierbarkeit (CR-2)

Der Scoring-Workflow pinnt:

- Python-Version + Abhängigkeiten via `uv.lock` (commitet im Repo).
- `PYTHONHASHSEED=0`.
- ENTSO-E-Antwort als Snapshot im selben Commit wie das Score-Ergebnis.

Damit ist jeder Score-Stand bitweise nachvollziehbar — das ist
Art. 12 KI-VO (Aufzeichnung) plus CR-2 (Determinismus).
