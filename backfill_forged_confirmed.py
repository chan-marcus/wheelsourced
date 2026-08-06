#!/usr/bin/env python3
"""
Backfill forged_confirmed for all suppliers based on their forging_process.
Auto-confirms forging when vendor describes a forging process.
"""

import yaml
from pathlib import Path

WHEELS_DIR = Path("wheels")

def should_confirm_forged(forging_process):
    """Check if forging_process indicates actual forging (not flow forming)."""
    if not forging_process:
        return None

    fp = str(forging_process).lower()

    # Exclude flow forming/casting indicators
    if any(x in fp for x in ("flow form", "flow-form", "rotary forg", "spun", "spinning", "not forged", "cast")):
        return False

    # Confirm if forging keywords present
    if any(x in fp for x in ("forg", "press", "blank", "billet")):
        return True

    return None


changed = 0
for wheel_dir in WHEELS_DIR.iterdir():
    if not wheel_dir.is_dir():
        continue

    suppliers_dir = wheel_dir / "suppliers"
    if not suppliers_dir.exists():
        continue

    for supplier_file in sorted(suppliers_dir.glob("*.yaml")):
        rec = yaml.safe_load(supplier_file.read_text())

        # Only auto-confirm if forged_confirmed is currently None
        if rec.get("from_quote", {}).get("forged_confirmed") is None:
            forging_process = rec.get("from_quote", {}).get("forging_process")

            if forging_process:
                confirmed = should_confirm_forged(forging_process)
                if confirmed is not None:
                    rec["from_quote"]["forged_confirmed"] = confirmed
                    supplier_file.write_text(
                        yaml.safe_dump(rec, sort_keys=False, allow_unicode=True),
                        encoding="utf-8"
                    )
                    status = "✓ CONFIRMED" if confirmed else "✗ NOT FORGED"
                    print(f"{supplier_file.stem:<45} {status}")
                    changed += 1

print(f"\n{changed} record(s) updated")
