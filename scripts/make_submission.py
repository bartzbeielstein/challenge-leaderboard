"""
make_submission.py --- erzeugt <repo-root>/submissions/<team>/<D>.csv.

Aufruf (aus dem Wurzelverzeichnis des challenge-leaderboard-Repos):
    cd .../challenge-leaderboard
    python .../lecture/scripts/make_submission.py \
        --team team_lambda --target-date 2026-05-15

Optional explizit:
    python .../make_submission.py --team … --target-date … \
        --repo-root /pfad/zum/challenge-leaderboard

Voraussetzungen:
- ENTSOE_API_KEY ist in der Shell gesetzt.
- Im --repo-root existiert ein Verzeichnis `submissions/` (Konvention
  des Leaderboard-Repos, siehe dortige README.md).

Minimal-Pipeline: reines Lag-Modell (168 h ~ Wochen-Persistenz), ohne
Kalender-/Wetter-Exogene. Genauer und kursnah ist die volle Pipeline aus
Kap. 06--08; fuer den ersten Submission-Lauf reicht dieses Skript.
"""
import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from spotforecast2_safe import ForecasterRecursiveLGBM, LinearlyInterpolateTS
from spotforecast2_safe.data.fetch_data import fetch_data, get_data_home
from spotforecast2_safe.downloader.entsoe import download_new_data


def make_submission(team: str, target_date: str, repo_root: Path) -> Path:
    target = datetime.fromisoformat(target_date).replace(tzinfo=timezone.utc)
    train_end = target - timedelta(hours=1)
    train_start = train_end - timedelta(days=30)

    api_key = os.environ["ENTSOE_API_KEY"]
    download_new_data(
        api_key=api_key,
        country_code="DE",
        start=train_start.strftime("%Y%m%d%H%M"),
        end=train_end.strftime("%Y%m%d%H%M"),
        force=True,
    )

    interim = get_data_home() / "interim" / "energy_load.csv"
    df_raw = fetch_data(filename=str(interim))
    df_raw.index = pd.to_datetime(df_raw.index, utc=True)

    load_col = next(c for c in df_raw.columns if "Actual" in c and "Load" in c)
    y = df_raw[load_col].astype(float).rename("load")
    if y.index.inferred_freq != "h":
        y = y.resample("h").mean()
    y = y.loc[train_start:train_end]
    # ENTSO-E publiziert die "Actual Load" mit einigen Stunden Verzug.
    # Wir schneiden ab der letzten tatsächlich vorhandenen Stunde ab und
    # lassen den Forecaster die Lücke bis zum Ende des Zieltags überbrücken.
    y = y.loc[:y.last_valid_index()]

    # CR-3: Lücken explizit behandeln, statt sie still zu interpolieren
    y = LinearlyInterpolateTS(on_missing="raise").fit_transform(y)
    # Forecaster verlangt einen DatetimeIndex mit gesetzter Frequenz
    y.index = pd.DatetimeIndex(y.index, freq="h")

    model = ForecasterRecursiveLGBM(
        iteration=0,
        lags=168,
        random_state=2026,  # CR-2: deterministisch
    )
    model.fit(y)

    target_hours = pd.date_range(target, periods=24, freq="h", tz="UTC")
    horizon = int((target_hours[-1] - y.index[-1]) / pd.Timedelta(hours=1))
    y_pred_full = model.forecaster.predict(steps=horizon)
    y_pred = y_pred_full.iloc[-24:]

    submission = pd.DataFrame({
        "timestamp_utc": target_hours.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forecast_mw": y_pred.values.round(2),
    })

    submissions_dir = repo_root / "submissions"
    if not submissions_dir.is_dir():
        raise SystemExit(
            f"Verzeichnis '{submissions_dir}' nicht gefunden. "
            f"Aufruf vom Wurzelverzeichnis des challenge-leaderboard-Repos "
            f"erwartet, oder --repo-root explizit angeben."
        )

    out = submissions_dir / team / f"{target_date}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    submission.to_csv(out, index=False)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", required=True)
    parser.add_argument("--target-date", required=True,
                        help="YYYY-MM-DD (UTC date of forecast target)")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(),
                        help="Wurzelverzeichnis des challenge-leaderboard-"
                             "Repos (Default: aktuelles Arbeitsverzeichnis)")
    args = parser.parse_args()
    path = make_submission(args.team, args.target_date, args.repo_root.resolve())
    print(f"Submission geschrieben: {path}")