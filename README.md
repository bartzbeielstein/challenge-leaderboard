# challenge-leaderboard

Automatisierte Bewertung der Live-Lastprognose-Challenge der Vorlesung
*Sicherheitskritische Zeitreihenprognose mit spotforecast2-safe*
(Numerische Mathematik, SS 2026, TH Köln, Bartz-Beielstein).

Die Spielregeln stehen in Kapitel 12 des Skripts (`lecture/12_challenge.qmd`,
gerendert auf der Lehrstuhl-Webseite). Dieses Repo ist die
*Bewertungs-Infrastruktur*: hier reichen Teams ihre täglichen
Vorhersagen ein, hier läuft das Scoring, hier wird das Leaderboard
gebaut und auf GitHub Pages publiziert.

## Setup für Lehrende (einmalig)

1. Repo öffentlich auf GitHub anlegen, z.B.
   `https://github.com/<lehrstuhl>/challenge-leaderboard`.
2. Diesen Verzeichnisbaum committen und pushen.
3. Repo-Secret `ENTSOE_API_KEY` setzen
   (Settings → Secrets and variables → Actions).
4. GitHub Pages aktivieren: Source = "GitHub Actions".
5. Branch protection auf `main`: PRs müssen `validate-pr.yml` grün haben
   und dürfen via *Auto-merge* zusammengeführt werden.
6. `teams.yml` mit den angemeldeten Teams pflegen (siehe Schema unten).
7. Den Kickoff-Termin (`KICKOFF_DATE` in `scripts/score_day.py`) bei
   Bedarf anpassen.

## Setup für Teams (einmalig)

1. Forken Sie dieses Repo.
2. Lokal clonen, `pyproject.toml` per `uv sync` installieren.
3. Submission lokal erzeugen (siehe `make_submission.py` in Kapitel 12).
4. Für jede Submission: Feature-Branch → `submissions/<team_id>/<D>.csv`
   commiten → PR gegen `main` → automatischer Merge bei grünem Check.

## Verzeichnisbaum

```
challenge-leaderboard/
├── teams.yml                          # Team-Registry
├── pyproject.toml                     # gepinnten Python-Stack für CI
├── submissions/<team_id>/<D>.csv      # Einreichungen
├── data/scores.parquet                # append-only Score-Historie
├── public/                            # gh-pages-Quelle (build artefact)
├── scripts/
│   ├── validate_submission.py        # Schema + Deadline + Auth-Check
│   ├── score_day.py                  # täglicher ENTSO-E-Pull + MAE
│   └── build_leaderboard.py          # Aggregat + HTML
├── templates/leaderboard.html.j2
└── .github/workflows/
    ├── validate-pr.yml
    ├── score-daily.yml
    └── build-and-deploy.yml
```

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

- **Primär**: MAE [MW] über die 24 Stunden eines Zieltages.
- **Aggregat (öffentliches Ranking)**: mittlere MAE = Summe der
  Tages-MAEs / Anzahl bewerteter Tage (aufsteigend).
- **LOCF**: Reicht ein Team an einem Zieltag keine Prognose ein, wird
  die jeweils letzte vorhandene Submission des Teams fortgeschrieben
  (last observation carried forward) und zählt als bewerteter Tag.
- **Tie-Break**: Anzahl bewerteter Tage (absteigend).

Details und die Formeln in `lecture/12_challenge.qmd` (§
"Bewertungsmethodik im Detail").

## Reproduzierbarkeit (CR-2)

Der Scoring-Workflow pinnt:

- Python-Version + Abhängigkeiten via `uv.lock` (commitet im Repo).
- `PYTHONHASHSEED=0`.
- ENTSO-E-Antwort als Snapshot im selben Commit wie das Score-Ergebnis.

Damit ist jeder Score-Stand bitweise nachvollziehbar — das ist
Art. 12 KI-VO (Aufzeichnung) plus CR-2 (Determinismus).
