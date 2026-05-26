"""make_commit.py

Berechnet die SHA-256-Hex-Summe einer Submission-CSV und schreibt sie
in die zugehörige `.commit`-Datei daneben. Dient als Helfer für den
Commit-Phase-PR im zweistufigen Commit-Reveal-Verfahren.

Aufruf:
    python scripts/make_commit.py submissions/<team_id>/<YYYY-MM-DD>.csv

Erzeugt `submissions/<team_id>/<YYYY-MM-DD>.commit` mit der
64-stelligen Hex-Summe. Die CSV darf danach **nicht mehr** verändert
werden, sonst schlägt der Reveal-Hash-Check fehl.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path,
                        help="Pfad zur Submission-CSV")
    args = parser.parse_args()

    if not args.csv.is_file():
        print(f"ERROR: {args.csv} existiert nicht oder ist keine Datei",
              file=sys.stderr)
        return 1
    if args.csv.suffix != ".csv":
        print(f"ERROR: {args.csv} hat nicht die Endung .csv",
              file=sys.stderr)
        return 1

    digest = hashlib.sha256(args.csv.read_bytes()).hexdigest()
    commit_path = args.csv.with_suffix(".commit")
    commit_path.write_text(digest + "\n", encoding="utf-8")
    print(f"OK: {commit_path} <- {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
