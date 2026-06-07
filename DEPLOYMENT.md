# Schritt-für-Schritt: `challenge-leaderboard` deployen

Reihenfolge ist wichtig --- `uv lock` **vor** dem ersten Push, damit die
CI-Workflows nicht gleich auf `uv sync --frozen` aussteigen.

## 0. Voraussetzungen

Auf dem lokalen Rechner:

```sh
uv --version           # uv >= 0.4
gh --version           # GitHub CLI; alternativ Web-UI
gh auth status         # GitHub-Login aktiv? sonst: gh auth login
```

`pyproject.toml` pinnt `requires-python = ">=3.13"` --- identisch zum
übergeordneten Kurs-Stack, weil `spotforecast2-safe>=3.0` Python 3.13
verlangt.

Außerdem brauchen Sie:

- Den ENTSO-E-API-Token (gleiche Quelle wie für die Vorlesung).
- Einen GitHub-Account/Org, der das Repo öffentlich hostet (für freies
  GitHub Pages reicht ein normaler User-Account).

---

## 1. Lockfile lokal erzeugen

```sh
cd /Users/bartz/workspace/Lehre.d/numerische-mathematik-sose-26/challenge-leaderboard
uv lock
```

Das erzeugt `uv.lock` aus `pyproject.toml`. Diese Datei **muss** ins
Repo, sonst schlägt `uv sync --frozen` in der CI fehl.

Kontrolle:

```sh
ls -la uv.lock         # sollte existieren
uv sync --frozen       # lokaler Trockenlauf; baut .venv/
```

Falls Sie keine lokale `.venv/` im Leaderboard-Repo wollen: `.venv/` ist
bereits in `.gitignore`, der Aufruf legt sie nur lokal an und stört nichts.

---

## 2. Git initialisieren und ersten Commit machen

```sh
cd /Users/bartz/workspace/Lehre.d/numerische-mathematik-sose-26/challenge-leaderboard
git init -b main
git add .
git status              # Sichtcheck: keine __pycache__/, kein .venv/
git commit -m "init: Challenge-Leaderboard scaffolding"
```

---

## 3. GitHub-Repo anlegen und pushen

**Variante A: GitHub CLI (eine Zeile, empfohlen):**

```sh
gh repo create challenge-leaderboard \
    --public \
    --source . \
    --remote origin \
    --description "Live-Lastprognose-Challenge SoSe26 — Bewertung & Leaderboard" \
    --push
```

Das legt das Remote-Repo unter
`https://github.com/<ihr-handle>/challenge-leaderboard` an und pusht
`main` direkt hoch.

**Variante B: Web-UI:**

1. Auf <https://github.com/new> ein **öffentliches** Repo
   `challenge-leaderboard` anlegen --- **ohne** README, .gitignore oder
   Lizenz (haben wir lokal schon).
2. Lokales Remote setzen und pushen:
   ```sh
   git remote add origin git@github.com:<ihr-handle>/challenge-leaderboard.git
   git push -u origin main
   ```

Im Folgenden ersetzen Sie `<owner>` durch Ihren GitHub-Handle bzw. die Org.

---

## 4. ENTSO-E-Secret setzen

**Variante A: GitHub CLI:**

```sh
gh secret set ENTSOE_API_KEY \
    --repo <owner>/challenge-leaderboard \
    --body "<ihr-entsoe-token>"
```

**Variante B: Web-UI:**

1. <https://github.com/><owner>/challenge-leaderboard/settings/secrets/actions
2. *New repository secret* → Name: `ENTSOE_API_KEY`, Value: Ihr Token
   → *Add secret*.

Verifizieren:

```sh
gh secret list --repo <owner>/challenge-leaderboard
# erwartete Ausgabe: ENTSOE_API_KEY    Updated YYYY-MM-DD
```

Der Token erscheint nicht im Klartext --- das ist korrekt (Art. 12/15
KI-VO, CR-4).

---

## 4a. Auto-Merge-Bot (GitHub App) einrichten

Das automatische Mergen (Team-Submission-PRs **und** die täglichen
Score-PRs) läuft über eine **GitHub App** mit kurzlebigen, pro Lauf neu
erzeugten Installations-Tokens. Das ersetzt den früheren PAT
`SCORE_BOT_TOKEN`, dessen Ablauf die häufigste stille Fehlerquelle war
(abgelaufener Token → PRs blieben unbemerkt offen). Vorteile: Least
Privilege (nur dieses Repo), kein jährlicher Token-Rotations-Zwang, klare
Bot-Identität.

> **Wichtig:** Erst nach diesem Schritt (App erstellt, installiert,
> Secrets gesetzt) dürfen `score-daily.yml` / `auto-merge.yml` mit
> App-Token gemergt/aktiviert werden. Ohne die Secrets schlägt der
> `create-github-app-token`-Schritt **laut** fehl.

**1. App erstellen**

<https://github.com/settings/apps/new> (oder für eine Org:
`https://github.com/organizations/<org>/settings/apps/new`)

- *GitHub App name*: z. B. `challenge-leaderboard-bot`
- *Homepage URL*: die Repo-URL genügt
- *Webhook*: **deaktivieren** (Haken bei „Active" entfernen)
- *Repository permissions*:
  - **Contents: Read and write**
  - **Pull requests: Read and write**
- *Where can this GitHub App be installed?*: *Only on this account*
- **Create GitHub App**

**2. Private Key erzeugen & App installieren**

- Auf der App-Seite unten *Generate a private key* → lädt eine `.pem`-Datei.
- *Install App* (linke Seitenleiste) → auf dem Account installieren und
  auf **Only select repositories → challenge-leaderboard** beschränken.
- Die **App ID** steht oben auf der App-Seite (*About* → „App ID").

**3. Secrets setzen**

```sh
gh secret set APP_ID \
    --repo <owner>/challenge-leaderboard \
    --body "<app-id-zahl>"

gh secret set APP_PRIVATE_KEY \
    --repo <owner>/challenge-leaderboard \
    --body "$(cat /pfad/zu/challenge-leaderboard-bot.*.pem)"
```

Verifizieren:

```sh
gh secret list --repo <owner>/challenge-leaderboard
# erwartet: APP_ID, APP_PRIVATE_KEY, ENTSOE_API_KEY
```

Den alten PAT anschließend entfernen (falls vorhanden):

```sh
gh secret delete SCORE_BOT_TOKEN --repo <owner>/challenge-leaderboard
```

> **Sicherheitsmodell (Fork-PRs):** `validate-pr.yml` läuft unter
> `pull_request` und führt damit ungeprüften Fork-Code aus — ihm werden
> **bewusst keine Secrets** gegeben (GitHub blendet sie für Fork-PRs aus).
> Das Mergen übernimmt `auto-merge.yml` per `workflow_run` im
> vertrauenswürdigen Basis-Repo-Kontext, **ohne** den PR-Code auszuführen,
> und nur für grün validierte PRs mit genau einer `submissions/**`-Datei.

---

## 5. GitHub Pages aktivieren

**Web-UI (es gibt keinen offiziellen gh-Befehl dafür):**

1. <https://github.com/><owner>/challenge-leaderboard/settings/pages
2. **Source**: *GitHub Actions* (nicht "Deploy from a branch").
3. Speichern.

Der Workflow `build-and-deploy.yml` ist bereits so konfiguriert, dass er
das Pages-Artefakt korrekt hochlädt (`actions/upload-pages-artifact` +
`actions/deploy-pages`).

---

## 6. Erst-Build des Leaderboards anstoßen

Das Leaderboard ist noch leer (kein Scoring gelaufen). Erzwingen Sie
einen ersten Build, damit die Pages-URL existiert:

```sh
gh workflow run "Build & Deploy Leaderboard" \
    --repo <owner>/challenge-leaderboard \
    --ref main
```

Status verfolgen:

```sh
gh run watch --repo <owner>/challenge-leaderboard
```

Nach ~1 Minute liefert die URL
`https://<owner>.github.io/challenge-leaderboard/` die Seite mit dem
Hinweis "Noch keine bewerteten Submissions" --- genau richtig vor dem
Kickoff.

---

## 7. Branch Protection für `main` (empfohlen)

Damit Teams nicht versehentlich `data/scores.parquet` überschreiben
können und der Auto-Merge des Validators sauber greift:

```sh
gh api repos/<owner>/challenge-leaderboard/branches/main/protection \
    --method PUT \
    --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["validate"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

Auto-Merge in den Repo-Einstellungen aktivieren:

1. <https://github.com/><owner>/challenge-leaderboard/settings
2. Abschnitt *Pull Requests* → **Allow auto-merge** anhaken.

---

## 8. Trockenlauf des Score-Workflows

Bevor der Kickoff läuft, einmal manuell für einen Tag scoren, an dem
ENTSO-E definitiv Daten hat (z.B. gestern):

```sh
# Linux:
YESTERDAY=$(date -u -d "yesterday" +"%Y-%m-%d")
# macOS:
# YESTERDAY=$(date -u -v-1d +"%Y-%m-%d")

gh workflow run "Daily Scoring" \
    --repo <owner>/challenge-leaderboard \
    --ref main \
    -f target_date="$YESTERDAY"
gh run watch --repo <owner>/challenge-leaderboard
```

Da es noch keine Submissions gibt, sollte der Lauf mit
`[score_day] Keine Submissions für YYYY-MM-DD — fertig.` enden und
`data/scores.parquet` nicht ändern. Damit ist verifiziert, dass:

- der ENTSO-E-Token zieht,
- der Cron-Pfad bis zum Commit-Schritt durchläuft,
- der nachfolgende Build-Workflow korrekt re-deployed.

---

## 9. Teams eintragen

Nach Eingang der Anmeldungen `teams.yml` pflegen:

```yaml
teams:
  - id: team_lambda
    display_name: "Team Lambda"
    github_handles: [alice42, bob99, carol7]
  - id: team_omega
    display_name: "Team Omega"
    github_handles: [dora88, eve12]
```

Commit & push direkt auf `main` (Lehrende haben Bypass-Recht):

```sh
git add teams.yml
git commit -m "teams: Anmeldungen Welle 1"
git push
```

Der `build-and-deploy.yml`-Trigger reagiert auf Änderungen an
`teams.yml` und re-deployed das Leaderboard automatisch.

---

## 10. Im Kapitel 12 die echte URL eintragen

Sobald `<owner>` feststeht, in `lecture/12_challenge.qmd` zwei
Platzhalter ersetzen:

```sh
cd /Users/bartz/workspace/Lehre.d/numerische-mathematik-sose-26/lecture
sed -i.bak \
    -e 's|<lehrstuhl>/challenge-leaderboard|<owner>/challenge-leaderboard|g' \
    -e 's|<lehrstuhl>.github.io/challenge-leaderboard|<owner>.github.io/challenge-leaderboard|g' \
    12_challenge.qmd
rm 12_challenge.qmd.bak
make render
```

Danach `_book/` bzw. die generierten `.ipynb` an die Studierenden
ausrollen.

---

## Checkliste vor dem Kickoff (2026-05-11)

- [ ] `uv.lock` committet
- [ ] Repo öffentlich auf GitHub
- [ ] Secret `ENTSOE_API_KEY` gesetzt
- [ ] Auto-Merge-Bot (GitHub App) erstellt + installiert; Secrets
      `APP_ID` und `APP_PRIVATE_KEY` gesetzt; alter `SCORE_BOT_TOKEN` entfernt
- [ ] GitHub Pages aktiviert (Source = Actions)
- [ ] Pages-URL erreichbar, zeigt "Noch keine bewerteten Submissions"
- [ ] Branch protection auf `main` + Auto-Merge erlaubt
- [ ] `Daily Scoring`-Trockenlauf grün
- [ ] Mindestens ein Team in `teams.yml`
- [ ] URL in `lecture/12_challenge.qmd` aktualisiert

Wenn alle Häkchen sitzen, ist die Challenge live.

---

## Laufender Betrieb: Ist-Last-Daten für „Prognose vs. Ist-Last"

Das Leaderboard zeigt neben der Tabelle die interaktive Plotly-Grafik
**„Prognose vs. Ist-Last"** (`scripts/charts.py` →
`fig_forecast_vs_actual`): die 24-h-Prognose jedes Teams und die
ENTSO-E-Day-ahead-Prognose als Baseline, jeweils gegen den tatsächlich
gemessenen Netz-Ist-Load (*Actual Total Load* 6.1.A). Die **Ist-Last-Spur**
stammt aus `data/actual_load.parquet` — einer **committeten** Zeitreihe,
die der Pages-Build (`build_leaderboard.py`) **ohne API-Key** liest.

Aus derselben Datei (Spalte `entsoe_forecast_mw`) leitet der Build auch
die Scores des **Pseudo-Teams `entsoe`** ab (teams.yml: `pseudo: true`):
es nimmt in allen Tabellen/Figuren am Ranking teil, exakt über den
Zeitraum der regulären Teams, reicht aber keine CSVs ein —
`validate_submission.py` lehnt Submissions für Pseudo-Teams ab,
`score_day.py` schließt sie vom täglichen Scoring aus.

**Seit 2026-06-04 vollautomatisch:** Der `Daily Scoring`-Workflow
(`score-daily.yml`) führt nach dem Scoring zusätzlich
`scripts/fetch_actuals.py --to <Zieltag>` aus (er hat den
`ENTSOE_API_KEY` als Secret) und nimmt die aktualisierte
`data/actual_load.parquet` mit in den täglichen Score-PR auf. Der
Build-/Deploy-Workflow bleibt bewusst ohne API-Key. Ein lokaler
Operator-Lauf ist im Normalbetrieb **nicht mehr nötig**.

> **Historie:** Bis 2026-06-04 war dies ein manueller, lokaler Schritt.
> Symptom, wenn er vergessen wurde: Tabelle und MAE sprangen nach dem
> täglichen CI-Scoring auf den neuen Tag, aber die
> „Prognose vs. Ist-Last"-Grafik blieb am Vortag stehen.

**Fallback / Backfill (nur noch bei Bedarf,** z. B. ENTSO-E-Nachlieferung
älterer Tage oder erzwungenes Neuladen; `ENTSOE_API_KEY` muss in der
Umgebung gesetzt sein):

```sh
# Alle relevanten Tage (aus Submissions + Scores abgeleitet, <= heute UTC).
# Bereits vollständige Tage werden übersprungen; der noch unfertige heutige
# Tag wird automatisch aufgeschoben (ENTSO-E publiziert mit ein paar Stunden
# Verzug):
uv run python scripts/fetch_actuals.py

# Alternativ ein gezieltes Datumsfenster:
uv run python scripts/fetch_actuals.py --from 2026-06-01 --to 2026-06-01

# Bereits vollständige Tage erzwungen neu laden:
uv run python scripts/fetch_actuals.py --force
```

Anschließend Commit + PR; der PR berührt keine `submissions/**`-Datei,
daher nickt `validate-pr.yml` ihn pass-through ab; `auto-merge.yml` mergt
ihn **bewusst nicht** automatisch — als Lehrende:r selbst (Admin) mergen:

```sh
# macOS:
git switch -c data/actuals-$(date -u +%F)
git add data/actual_load.parquet
git commit -m "data: Ist-Load bis YYYY-MM-DD nachgezogen"
git push -u origin HEAD
gh pr create --fill --base main
gh pr merge --squash --admin
```

**Redeploy** passiert automatisch: Sobald `data/actual_load.parquet`
auf `main` landet, startet `build-and-deploy.yml` — die Datei steht in
dessen `push`-`paths:`-Filter (neben `data/scores.parquet`, `teams.yml`,
dem Template, `scripts/build_leaderboard.py` und `scripts/charts.py`).
Zusätzlich re-rendert der `workflow_run`-Trigger die Seite nach jedem
`Daily Scoring`-Lauf aus der committeten `actual_load.parquet`.

Ein manueller Anstoß ist nur nötig, um **ohne neuen Commit** neu zu bauen
(z. B. nach einem direkten Push an `main` ohne PR):

```sh
gh workflow run "Build & Deploy Leaderboard" --ref main
gh run watch
```
