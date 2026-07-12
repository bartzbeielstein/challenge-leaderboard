# MANIFEST — macl2l correction package (2026-07-06)

Records exactly where and how the corrected `macl2l` / `macl2l_entsoe`
submissions (targets 2026-06-22..2026-07-06) were produced.

## Execution environment (correction replay, 2026-07-06 ~20:00–22:30 UTC)

| Item | Value |
|---|---|
| Hardware | Apple Silicon (arm64) |
| OS | macOS 26.5.2 (build 25F84), Darwin kernel 25.5.0 |
| Python | 3.14.2 |
| uv | 0.9.18 |
| MLX | 0.31.2 |

## Source provenance

| Item | Value |
|---|---|
| Submitter | `bart26l-vorlesung/scripts/macl2l.py` @ `95cf920` (`fix(macl2l): level-aware learned completion of the partial D-1`) |
| Model package | `macl2l` @ `ade3458` (imported via `--macl2l-root ~/workspace/macl2l/src`) |
| Leaderboard repo (pre-correction) | `bartzbeielstein/challenge-leaderboard` @ `59cabf7` |

## Model checkpoints

| Variant | Checkpoint (in `macl2l/scripts/entsoe/artifacts/`) | SHA256 | Trained |
|---|---|---|---|
| `macl2l` (nofc) | `macl2l_cov_nofc_dayahead_model.safetensors` — E189/H7/L5/M567, robust-target norm (clip 15), feat_clip 12, ctx 540 d, covariates solar/won/woff/price | `619176b37c95f69334aef4713f2047739f4958ed92782a140624ba34e7b60be8` | 2026-07-01 |
| `macl2l_entsoe` | `macl2l_cov_dayahead_model.safetensors` — E192/H6/L4/M384, mean/std norm, ctx 600 d, covariates fc_load/solar/won/woff/price | `00265f9139096b18800eaed5b56e95efa49960909b3eb1469b54b81fccd00399` | 2026-06-21 |

Checkpoint-vs-original disclosure: `macl2l` originals before 2026-07-02 were
submitted from an older checkpoint; the correction uses the current (2026-07-01)
checkpoint for all days. `macl2l_entsoe`'s checkpoint is unchanged since
2026-06-21, i.e. identical to every original in the corrected range.

## Data snapshot

ENTSO-E cache `~/spotforecast2_data/ddmo_macl2l/interim/{energy_load,renewable_forecast,day_ahead_price}.csv`,
fetched 2026-07-06 ~18:51–18:53 UTC (spans 2023-05-08 .. 2026-07-07 21:45 UTC:
actuals through the fetch time plus published day-ahead covariates). Replays are
origin-anchored: for target day D all model inputs are masked to timestamps
strictly before D 00:00 UTC, so the late cache snapshot does not leak into
earlier target days.

## Determinism statement

MLX inference and the Ridge blend are deterministic given the cache; the
`macl2l_entsoe` guard's LightGBM voter runs SpotOptim serially with a fixed
seed. Same-machine reruns reproduce the corrected CSVs; cross-machine float
accumulation may differ in the last decimals (verify `expected/SHA256SUMS`
with tolerance off-machine).

## Corrected-record decisions (summary; full text in README.md)

1. Replay standard: day-ahead complete D-1 (slightly favorable vs the original
   ~17–19 UTC partial-day conditions — disclosed).
2. Current checkpoints for all days (disclosed above).
3. All replays used, even where worse (no hindsight selection): both teams'
   2026-06-30 replays score worse than the originals and are kept anyway.
4. Guard sidecar rebuilt chronologically (no future-deviation leakage).
