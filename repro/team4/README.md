# team_4 reproducibility package — Lastprognose-Challenge SoSe26

Re-run the exact forecasting pipeline behind the `team_4` entries on the
[leaderboard](https://bartzbeielstein.github.io/challenge-leaderboard/):
ENTSO-E data → coverage guards → PACF lag selection → SpotOptim-tuned
LightGBM (recursive 24-h forecast) → submission CSV.

## Quickstart (offline replay of the 2026-06-08 forecast)

```sh
# prerequisite: uv (https://docs.astral.sh/uv/) —
#   curl -LsSf https://astral.sh/uv/install.sh | sh
unzip team4-repro-2026-06-08.zip && cd team4-repro-2026-06-08
uv sync --frozen
uv run python team4_submit.py --skip-download --as-of 2026-06-07T15:00:00Z \
    --deterministic --n-trials 20 --n-initial 10
shasum -a 256 submissions/team_4/2026-06-08.csv \
              expected/2026-06-08_reference_deterministic.csv
```

The two checksums match **bit-exactly** on arm64 macOS (the original
architecture, see `MANIFEST.md`; verified by two independent runs on
2026-06-07). The run takes ~45 min (serial SpotOptim is the price of
bit-reproducibility) and needs no API key and no network beyond `uv sync`.

## What "reproduce" means here — two profiles

| Profile | Command core | Guarantee |
|---|---|---|
| **Repro** (default above) | `--deterministic --n-trials 20 --n-initial 10` | bit-identical to `expected/2026-06-08_reference_deterministic.csv` on arm64 macOS |
| **Operational** | (no flags) = parallel SpotOptim, 100 trials | the settings that produced the submitted forecast; **not** bit-reproducible (parallel scheduling variance) — compare against `expected/2026-06-08_submitted.csv` with tolerance |

The submitted leaderboard forecast (`expected/2026-06-08_submitted.csv`) was
produced with the operational profile; the deterministic reference exists so
you have something exactly checkable.

## Live run (any future target day)

Forecast *tomorrow* instead of replaying history — requires a free
[ENTSO-E Transparency](https://transparency.entsoe.eu/) API token:

```sh
export ENTSOE_API_KEY=...
uv run python team4_submit.py            # full operational run, ~5–10 min
uv run python team4_submit.py --help     # all flags
```

## Package contents

| Path | Purpose |
|---|---|
| `team4_submit.py` | the pipeline (copy of `bart26k-lecture/scripts/team4_submit.py@87e27e1`, 3 documented divergences — see module docstring) |
| `pyproject.toml`, `uv.lock`, `.python-version`, `requirements.txt` | ==-pinned environment (Python 3.14.2; `requirements.txt` is the non-uv fallback: `pip install -r requirements.txt`) |
| `data/interim/*.csv` | frozen ENTSO-E snapshot (load + renewables + price), 2026-06-07 ~15:04 UTC |
| `expected/` | submitted CSV, deterministic reference CSV, `SHA256SUMS` |
| `teams.yml`, `scripts/validate_submission.py` | minimal leaderboard root: the output CSV is schema-checked offline after every run |
| `MANIFEST.md` | architecture, provenance, pins, determinism statement |

Integrity check: `shasum -a 256 -c expected/SHA256SUMS`

## Troubleshooting

- **ENTSO-E HTTP 504/503 (live runs):** the script retries with exponential
  backoff (`--max-retries`, `--backoff`); `--allow-stale` falls back to the
  bundled snapshot, gated by a freshness check.
- **`--as-of` errors about stale coverage:** keep the bundled snapshot and the
  documented `--as-of 2026-06-07T15:00:00Z` together — other dates need
  matching data.
- **Different output on Linux/x86:** expected; bit-exactness is only claimed
  for arm64 macOS (see MANIFEST.md, Determinism statement).
- **No network at run time:** weather/COVID features are skipped gracefully;
  the result then deviates from the reference (documented in MANIFEST.md).
