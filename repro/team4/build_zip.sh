#!/usr/bin/env bash
# Build the distributable reproducibility ZIP (maintainer tool, not shipped).
#
# Stages the committed package files plus the data snapshot under data/
# (git-ignored; integrity pinned via expected/SHA256SUMS) into
# dist/team4-repro-2026-06-08/ and zips it. Refuses to build if any staged
# file drifts from its pinned checksum.
set -euo pipefail
cd "$(dirname "$0")"

NAME="team4-repro-2026-06-08"
STAGE="dist/$NAME"

# 1. Integrity gate: every pinned file must match expected/SHA256SUMS.
shasum -a 256 -c expected/SHA256SUMS --quiet \
  || { echo "ERROR: checksum drift -- refusing to build." >&2; exit 1; }

# 2. Stage.
rm -rf "$STAGE" "dist/$NAME.zip"
mkdir -p "$STAGE/scripts" "$STAGE/expected" "$STAGE/data/interim"
cp team4_submit.py pyproject.toml uv.lock .python-version requirements.txt \
   teams.yml README.md MANIFEST.md "$STAGE/"
cp scripts/validate_submission.py "$STAGE/scripts/"
cp expected/* "$STAGE/expected/"
cp data/interim/*.csv "$STAGE/data/interim/"

# 3. Zip (deterministic-ish: fixed order, no extra attrs).
(cd dist && zip -r -X -q "$NAME.zip" "$NAME")
rm -rf "$STAGE"
ls -lh "dist/$NAME.zip"
