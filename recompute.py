#!/usr/bin/env python3
"""
recompute.py - re-run flag logic over every stored record.

Flags are computed at ingest time and written into the YAML, so changing
_flags() in pipeline.py does NOT update records you already have. Run this
after any change to the flag rules.

    python recompute.py
    python recompute.py --wheel mesh-futura
"""

import argparse
from pathlib import Path
import yaml

import pipeline as P

ap = argparse.ArgumentParser()
ap.add_argument("--wheel", default=None)
a = ap.parse_args()

root = Path("wheels")
dirs = [root / a.wheel] if a.wheel else [d for d in root.iterdir() if d.is_dir()]

changed = 0
for d in dirs:
    sup = d / "suppliers"
    if not sup.exists():
        continue
    for f in sorted(sup.glob("*.yaml")):
        rec = yaml.safe_load(f.read_text())
        for k in P.STORED_FIELDS:
            rec.setdefault("from_quote", {}).setdefault(k, None)

        before = list(rec.get("flags") or [])
        after = P._flags(rec)
        if before != after:
            added = [x for x in after if x not in before]
            gone = [x for x in before if x not in after]
            print(f"{f.stem}")
            for x in gone:
                print(f"   removed  {x}")
            for x in added:
                print(f"   added    {x}")
            changed += 1

        rec["flags"] = after
        f.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")

print(f"\n{changed} record(s) changed")
