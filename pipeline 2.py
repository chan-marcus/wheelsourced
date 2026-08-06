#!/usr/bin/env python3
"""
pipeline.py - from hand-picked Alibaba listings to publishable vendor records.

You do the photo search and pick the listings. This handles everything after.

    python pipeline.py pull    --wheel mesh-futura        # listings -> YAML
    python pipeline.py ask     --wheel mesh-futura        # YAML -> tailored RFQs
    python pipeline.py ingest  --wheel mesh-futura        # replies -> YAML
    python pipeline.py check   --wheel mesh-futura        # what is still missing

Every field records where it came from. `listing` values are what the platform
claims. `quote` values are what a human told you. The site should show which
is which, because listing prices are placeholders and quote prices are real.

Layout:
    wheels/mesh-futura/picks.txt        one listing URL per line
    wheels/mesh-futura/suppliers/*.yaml one record per supplier
    wheels/mesh-futura/outbox/*.txt     generated RFQs, you paste these
    wheels/mesh-futura/replies/*.txt    raw pasted replies, named supplier__YYYY-MM-DD.txt
"""

import argparse, json, re, sys, time, random, hashlib
from pathlib import Path
from datetime import date, datetime

import yaml
from anthropic import Anthropic

MODEL = "claude-sonnet-5"
client = Anthropic()
TODAY = date.today().isoformat()

# Fields a listing can never give you honestly. These drive the RFQ.
QUOTE_ONLY = {
    "price_per_wheel_1set":  "Price per wheel, for 1 set (4 pcs) and for 4 sets",
    "price_per_wheel_4set":  None,   # asked together with 1set, stored separately
    "weight_kg":             "Actual weight per wheel at this exact size",
    "load_rating_kg":        "Load rating per wheel, in kg",
    "forging_process":       "Is the wheel press-forged, or flow-formed / rotary-forged? What press tonnage, and which alloy?",
    "cert_document":         "JWL / VIA / TUV certificate PDF, and does it cover this width?",
    "freight_cif_usd":       "Shipping cost to Los Angeles USA, and the HS code you declare",
    "is_manufacturer":       "Are you the manufacturer or a trading company? Factory address?",
    "size_chart":            "Your full size chart, with the offset range available at each width",
}

# Rendered under "extras" instead of as a numbered demand. Softer ask, and
# these are the two that most often come back as an attachment.
EXTRAS = {"size_chart"}

# Rough weight model for a forged monoblock: kg ~= K * diameter * width.
# K = 0.055 puts an 18x9.5 at about 9.4 kg, which matches the light end of
# real forged wheels. CALIBRATE THIS once you have a dozen real quotes with
# verified weights; it is an estimate, not a measurement.
FORGED_K = 0.055
MONOBLOCK_TOLERANCE = 1.25   # honest monoblocks land under 25% over model
MULTIPIECE_TOLERANCE = 1.45  # barrel, lip and ~30 bolts are real weight

# Rendered as bullets at the end rather than numbered questions. These are
# nice-to-haves, and burying them keeps the numbered list short enough that
# suppliers actually answer all of it.
EXTRAS = {"size_chart"}

# Rough weight model for a forged monoblock: kg ~= K * diameter * width.
# K = 0.055 puts an 18x9.5 at about 9.4 kg, which matches the light end of
# real forged wheels. CALIBRATE THIS once you have a dozen real quotes with
# verified weights; it is an estimate, not a measurement.
FORGED_K = 0.055
MONOBLOCK_TOLERANCE = 1.25   # honest monoblocks land under 25% over model
MULTIPIECE_TOLERANCE = 1.45  # barrel, lip and ~30 bolts are real weight
PHOTO_ASK = ("A photo of the wheel on a scale, and one of the back of the spokes")


def wdir(wheel): return Path("wheels") / wheel
def sdir(wheel): return wdir(wheel) / "suppliers"


def _ask_model(text, schema, images=None):
    content = [{"type": "text", "text": f"{schema}\n\n--- SOURCE ---\n{text}"}]
    msg = client.messages.create(
        model=MODEL, max_tokens=4000,
        system="You extract structured data. Output valid JSON only, no prose, no fences.",
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("  !! unparseable model output", file=sys.stderr)
        return {}


# --------------------------------------------------------------------------
# 1. pull
# --------------------------------------------------------------------------
LISTING_SCHEMA = """
Return one JSON object about this Alibaba product listing and its supplier.

  supplier_name       string
  supplier_url        string or null
  business_type       "manufacturer" | "trading company" | "manufacturer+trading" | "unknown"
  years_on_platform   integer or null
  staff_count         integer or null
  factory_area_sqm    number or null
  verified_supplier   boolean
  trade_assurance     boolean
  response_rate       string or null
  listed_price_low    number or null   (USD per piece, as shown)
  listed_price_high   number or null
  moq                 string or null
  listed_lead_time    string or null
  sizes_offered       array of strings, e.g. ["17x8.0","18x9.5"]
  bolt_patterns       array of strings
  offsets_offered     array of strings
  finishes            array of strings
  construction        "cast" | "flow formed" | "forged" | "2-piece" | "3-piece" | "unknown"
  certs_claimed       array of strings
  main_image_url      string or null

Use null or [] for anything absent. Do not infer. Output ONLY the JSON object.
"""


def pull(wheel):
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup

    picks = (wdir(wheel) / "picks.txt").read_text().split()
    picks = [u.strip() for u in picks if u.strip().startswith("http")]
    sdir(wheel).mkdir(parents=True, exist_ok=True)
    print(f"{len(picks)} listings")

    def clean(html):
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript", "svg", "iframe"]):
            t.decompose()
        for a in soup.find_all("a", href=True):
            a.append(f" <<{a['href']}>>")
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))[:45_000]

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir="data/browser-profile", headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for i, url in enumerate(picks, 1):
            print(f"  [{i}/{len(picks)}] {url[:70]}")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)
            for _ in range(5):
                page.mouse.wheel(0, 900); page.wait_for_timeout(500)
            body = clean(page.content())

            data = _ask_model(body, LISTING_SCHEMA)
            if not data:
                continue

            # Second hop: the supplier's own profile page has the real
            # business-type and factory facts. Worth the extra load.
            if data.get("supplier_url"):
                try:
                    page.goto(data["supplier_url"], wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_timeout(2000)
                    prof = _ask_model(clean(page.content()), LISTING_SCHEMA)
                    for k in ("business_type", "staff_count", "factory_area_sqm",
                              "years_on_platform", "certs_claimed", "response_rate"):
                        if prof.get(k) not in (None, [], "unknown"):
                            data[k] = prof[k]
                except Exception as e:
                    print(f"     profile hop failed: {e}")

            sid = re.sub(r"[^a-z0-9]+", "-",
                         (data.get("supplier_name") or hashlib.md5(url.encode()).hexdigest()[:8]).lower()).strip("-")[:48]

            path = sdir(wheel) / f"{sid}.yaml"

            if path.exists():
                # Same supplier, different listing. One supplier commonly
                # lists the same wheel several times across size ranges, so
                # merge rather than clobber.
                rec = yaml.safe_load(path.read_text())
                rec.setdefault("listings",
                               [rec.pop("listing_url")] if "listing_url" in rec else [])
                if url not in rec["listings"]:
                    rec["listings"].append(url)
                for k, v in data.items():
                    if k == "supplier_url":
                        continue
                    old = rec["from_listing"].get(k)
                    if old in (None, [], "", "unknown"):
                        rec["from_listing"][k] = v
                    elif isinstance(old, list) and isinstance(v, list):
                        rec["from_listing"][k] = sorted(set(old) | set(v))
                # New questions added later should appear on old records too.
                for k in QUOTE_ONLY:
                    rec["from_quote"].setdefault(k, None)
                rec["meta"]["listing_seen"] = TODAY
                print(f"     merged into {sid} ({len(rec['listings'])} listings)")
            else:
                rec = {
                    "id": sid,
                    "wheel": wheel,
                    "listings": [url],
                    "supplier_url": data.get("supplier_url"),
                    "from_listing": {k: v for k, v in data.items() if k != "supplier_url"},
                    "from_quote": {k: None for k in QUOTE_ONLY},
                    "meta": {"listing_seen": TODAY, "rfq_sent": None,
                             "reply_received": None, "questions_answered": None},
                    "flags": [],
                }

            path.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True),
                            encoding="utf-8")
            time.sleep(random.uniform(4, 8))
        ctx.close()

    print(f"-> {sdir(wheel)}")


# --------------------------------------------------------------------------
# 2. ask
# --------------------------------------------------------------------------
def ask(wheel, size="18x9.5J", offset="ET+22", pcd="5x114.3", finish="Bronze"):
    out = wdir(wheel) / "outbox"
    out.mkdir(parents=True, exist_ok=True)

    for f in sorted(sdir(wheel).glob("*.yaml")):
        rec = yaml.safe_load(f.read_text())
        # Iterate QUOTE_ONLY, not the record: YAML round-trips alphabetise
        # keys, and this also ignores keys left over from an older question
        # set while backfilling any newly added ones.
        for k in QUOTE_ONLY:
            rec["from_quote"].setdefault(k, None)
        missing = [k for k in QUOTE_ONLY if rec["from_quote"].get(k) in (None, "")]
        if not missing:
            continue

        # Only ask what the listing could not answer. Shorter asks get
        # better response rates and signal you actually read their page.
        numbered = [k for k in missing if k not in EXTRAS and QUOTE_ONLY.get(k)]
        extras = [k for k in missing if k in EXTRAS and QUOTE_ONLY.get(k)]

        qs = "\n".join(f"  {i}. {QUOTE_ONLY[k]}" for i, k in enumerate(numbered, 1))

        ex = ["A photo of the wheel on a scale, and one showing the back of the spokes"]
        ex += [QUOTE_ONLY[k] for k in extras]
        ex_block = "\n".join(f"  - {e}" for e in ex)

        body = f"""Hello,

I'm looking at your wheels for my own car and would like a quote.

Reference spec for all pricing:
  {size} \u00b7 {offset} \u00b7 {pcd} \u00b7 {finish}

Could you confirm:

{qs}

Two extras if you can:

{ex_block}

Thank you."""
        (out / f"{rec['id']}.txt").write_text(body, encoding="utf-8")
        rec["meta"]["rfq_sent"] = TODAY
        rec["meta"]["ref_spec"] = f"{size} {offset} {pcd} {finish}"
        f.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"  {rec['id']}: asking {len(missing)} questions")

    print(f"-> {out}  (paste these into Alibaba chat)")


# --------------------------------------------------------------------------
# 3. ingest
# --------------------------------------------------------------------------
REPLY_SCHEMA = """
This is a supplier's reply to a sourcing enquiry, often in broken English.
Return one JSON object. Extract ONLY what the supplier actually stated.

  price_per_wheel_1set   number or null (USD)
  price_per_wheel_4set   number or null
  weight_kg              number or null
  load_rating_kg         number or null
  forging_process        string or null. Record their claim verbatim in
                         summary, e.g. "press forged, 6000T press, 6061-T6"
                         or "flow formed" or "rotary forged" or
                         "not stated clearly". Note: "machined from a forged
                         blank" describes normal forging and is NOT a
                         downgrade. "Flow formed" and "rotary forged" mean a
                         CAST centre with a spun barrel -- record those terms
                         exactly, do not translate them to "forged".
  cert_document          "provided" | "promised" | "claimed without document" | null
  size_chart             string or null. If they listed sizes or attached a
                         chart, summarise the range, e.g. "17-20in, 8-12J,
                         ET-20 to +45, chart attached". Null if not addressed.
  freight_cif_usd        number or null
  is_manufacturer        true | false | null
  factory_address        string or null
  catalog_offered        true | false
  notes                  string, one line on anything notable

Critical rules:
- If they claim a certification but do not attach or offer the document, that
  is "claimed without document", NOT "provided".
- If a price is given without stating quantity, put it in
  price_per_wheel_1set and say so in notes.
- For forging_process, record their exact claim. "Forged" alone with no
  process detail or press tonnage is "not stated clearly".
- Never fill a field the supplier did not address.

Output ONLY JSON.
"""


def ingest(wheel):
    rep = wdir(wheel) / "replies"
    if not rep.exists():
        raise SystemExit(f"put raw replies in {rep}/ named supplier-id__YYYY-MM-DD.txt")

    for f in sorted(rep.glob("*.txt")):
        sid, _, datestr = f.stem.partition("__")
        target = sdir(wheel) / f"{sid}.yaml"
        if not target.exists():
            print(f"  no record for '{sid}', skipping {f.name}")
            continue

        rec = yaml.safe_load(target.read_text())
        got = _ask_model(f.read_text(encoding="utf-8"), REPLY_SCHEMA)

        filled = 0
        for k in QUOTE_ONLY:
            if got.get(k) not in (None, ""):
                rec["from_quote"][k] = got[k]
                filled += 1

        asked = rec["meta"].get("rfq_sent")
        rec["meta"]["reply_received"] = datestr or TODAY
        rec["meta"]["questions_answered"] = f"{filled}/{len(QUOTE_ONLY)}"
        if asked and datestr:
            try:
                rec["meta"]["reply_days"] = (datetime.fromisoformat(datestr).date()
                                             - datetime.fromisoformat(asked).date()).days
            except ValueError:
                pass
        if got.get("notes"):
            rec["meta"]["reply_note"] = got["notes"]
        if got.get("catalog_offered"):
            rec["meta"]["catalog_offered"] = True
        if got.get("factory_address"):
            rec["meta"]["factory_address"] = got["factory_address"]

        rec["flags"] = _flags(rec)
        target.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"  {sid}: {filled}/{len(QUOTE_ONLY)} answered  {rec['flags'] or ''}")


def _ref_size(rec):
    """Diameter and width from the spec this supplier was quoted on."""
    m = re.search(r"(\d{2})\s*[xX]\s*(\d+\.?\d*)",
                  str((rec.get("meta") or {}).get("ref_spec") or "18x9.5"))
    return (float(m.group(1)), float(m.group(2))) if m else (18.0, 9.5)


def _flags(rec):
    fl, L, Q = [], rec["from_listing"], rec["from_quote"]
    p = Q.get("price_per_wheel_1set")
    forged = "forged" in str(L.get("construction", "")).lower()

    if forged and p and p < 150:
        fl.append("price implausible for forged")
    if Q.get("cert_document") == "claimed without document":
        fl.append("cert claimed, no document")
    w = Q.get("weight_kg")
    if forged and w:
        # Scale with the wheel. A flat ceiling accuses honest suppliers of
        # big sizes, and gives multi-piece no allowance for its hardware.
        dia, wid = _ref_size(rec)
        multi = any(k in str(L.get("construction", "")).lower()
                    for k in ("2-piece", "3-piece", "two piece", "three piece"))
        ceiling = FORGED_K * dia * wid * (MULTIPIECE_TOLERANCE if multi else MONOBLOCK_TOLERANCE)
        if w > ceiling:
            fl.append(f"weight {w}kg high for forged at {dia:g}x{wid:g} "
                      f"(expected under {ceiling:.1f}kg)")
    if Q.get("is_manufacturer") is True and "trading" in str(L.get("business_type", "")).lower():
        fl.append("claims factory, profile says trading")
    if Q.get("load_rating_kg") is None:
        fl.append("no load rating given")
    fp = str(Q.get("forging_process") or "").lower()
    # Every forged wheel is machined from a forged blank -- that IS the
    # process, so it is not a red flag. The actual fraud vector is a cast
    # centre with a spun barrel being sold as forged.
    if any(k in fp for k in ("flow form", "flow-form", "rotary forg", "spun", "spinning")):
        fl.append("flow formed, cast centre, not press forged")
    if fp and not any(k in fp for k in ("press", "forg", "ton", "flow", "rotary", "spun")):
        fl.append("forging process not actually described")
    if Q.get("forging_process") and "not stated" in fp:
        fl.append("dodged the forging question")
    if Q.get("size_chart") in (None, ""):
        fl.append("no size chart supplied")
    if (rec["meta"].get("reply_days") or 0) > 3:
        fl.append("slow to reply")
    if (L.get("years_on_platform") or 99) < 3:
        fl.append("under 3 years listed")
    return fl


# --------------------------------------------------------------------------
# 4. check
# --------------------------------------------------------------------------
def check(wheel):
    recs = [yaml.safe_load(f.read_text()) for f in sorted(sdir(wheel).glob("*.yaml"))]
    if not recs:
        raise SystemExit("no supplier records yet")

    print(f"\n{'supplier':<30} {'answered':<10} {'$1set':<8} {'wt':<7} {'cert':<24} flags")
    print("-" * 108)
    for r in sorted(recs, key=lambda r: -(len([v for v in r["from_quote"].values() if v not in (None, "")]))):
        q = r["from_quote"]
        print(f"{r['id'][:29]:<30} "
              f"{str(r['meta'].get('questions_answered') or '-'):<10} "
              f"{str(q.get('price_per_wheel_1set') or '-'):<8} "
              f"{str(q.get('weight_kg') or '-'):<7} "
              f"{str(q.get('cert_document') or '-')[:23]:<24} "
              f"{', '.join(r['flags'])}")

    stale = [r for r in recs if not r["meta"].get("reply_received")]
    if stale:
        print(f"\nno reply yet from: {', '.join(r['id'] for r in stale)}")
    print("\nDash means asked and not answered. Publish it that way. "
          "'Not disclosed after two asks' is a real data point.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pull", "ask", "ingest", "check"):
        p = sub.add_parser(name); p.add_argument("--wheel", required=True)
        if name == "ask":
            p.add_argument("--size", default="18x9.5J"); p.add_argument("--offset", default="ET+22")
            p.add_argument("--pcd", default="5x114.3"); p.add_argument("--finish", default="Bronze")
    a = ap.parse_args()

    if a.cmd == "pull": pull(a.wheel)
    elif a.cmd == "ask": ask(a.wheel, a.size, a.offset, a.pcd, a.finish)
    elif a.cmd == "ingest": ingest(a.wheel)
    elif a.cmd == "check": check(a.wheel)
