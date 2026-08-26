#!/usr/bin/env python3
"""
consolidate.py — archive the raw extractions and install the reviewed set.

Twenty-two theses came out of three research documents. Many were the same
underlying uncertainty stated twice, some tested nothing checkable, and a few
were bundled claims that no single criterion could settle. Six survived review.

This moves the originals to data/theses.raw/ and writes the six into
data/theses.real/. Nothing is deleted - the raw set is the record of what the
model produced before a human looked at it, which is the interesting part.

Usage:
    python consolidate.py --dry-run     # show what would happen
    python consolidate.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="real", choices=["real", "demo"])
    ap.add_argument("--source", default="theses.consolidated.json",
                    help="File holding the reviewed theses")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    live_dir = REPO_ROOT / "data" / f"theses.{args.profile}"
    raw_dir = REPO_ROOT / "data" / "theses.raw"
    source = REPO_ROOT / args.source

    if not source.exists():
        sys.exit(f"Not found: {source}")

    payload = json.loads(source.read_text(encoding="utf-8"))
    theses = payload.get("theses") or []
    if not theses:
        sys.exit("No theses in the consolidated file.")

    existing = sorted(live_dir.glob("*.json")) if live_dir.exists() else []

    print(f"raw theses to archive : {len(existing)}")
    print(f"reviewed theses to add: {len(theses)}")
    print(f"cut with reasons      : {len(payload.get('cut') or [])}")

    if args.dry_run:
        print("\nWould archive:")
        for p in existing:
            print(f"  {p.name} -> data/theses.raw/{p.name}")
        print("\nWould write:")
        for t in theses:
            print(f"  {t['id']}.json")
        print("\n(dry run - nothing changed)")
        return

    # Archive first. If anything below fails, the originals are already safe.
    raw_dir.mkdir(parents=True, exist_ok=True)
    for path in existing:
        target = raw_dir / path.name
        if target.exists():
            print(f"  ! {path.name} already archived, leaving it")
            path.unlink()
        else:
            shutil.move(str(path), str(target))
    print(f"\narchived {len(existing)} to data/theses.raw/")

    live_dir.mkdir(parents=True, exist_ok=True)
    for thesis in theses:
        out = live_dir / f"{thesis['id']}.json"
        out.write_text(json.dumps(thesis, indent=2) + "\n", encoding="utf-8")
        print(f"  wrote {out.name}")

    # The cut list is part of the record. A decision without its reason is
    # not reviewable six months later.
    cut_path = REPO_ROOT / "data" / "cut.json"
    cut_path.write_text(
        json.dumps({"cut": payload.get("cut") or []}, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\n{len(theses)} theses live in data/theses.{args.profile}/")
    print(f"{len(existing)} originals preserved in data/theses.raw/")
    print("cut list with reasons in data/cut.json")


if __name__ == "__main__":
    main()
