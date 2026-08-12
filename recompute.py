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

        # Collapse certification spellings to the canonical tags. Records
        # pulled before the whitelist existed hold "VIA" beside "VIA A-351"
        # and "IATF 16949" beside "IATF16949", and the sheet prints both.
        # No model call and nothing invented: this only ever renames or drops
        # what a record already claims, so it is safe to re-run.
        for side in ("from_listing", "from_quote"):
            block = rec.get(side) or {}
            if "certs_claimed" not in block:
                continue
            was = block["certs_claimed"] or []
            now = P.cert_names(was)
            if now != list(was):
                print(f"{f.stem}\n   certs    {side}: {was or '-'} -> {now or '-'}")
                changed += 1
            block["certs_claimed"] = now if side == "from_listing" else (now or None)

        # questions_answered counts populated ALL_FIELDS, so changing the
        # question set leaves every stored fraction wrong until it is redone.
        # Derived straight from the record, no model call, same as the flags.
        n = len(P.answered_keys(rec["from_quote"]))
        qa = f"{n}/{len(P.ALL_FIELDS)}"
        if rec.setdefault("meta", {}).get("questions_answered") not in (None, qa):
            print(f"{f.stem}\n   answered {rec['meta']['questions_answered']} -> {qa}")
            changed += 1
        if rec["meta"].get("questions_answered") is not None:
            rec["meta"]["questions_answered"] = qa

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
