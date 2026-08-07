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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import date, datetime

import yaml
from anthropic import Anthropic

MODEL = "claude-haiku-4-5-20251001"
client = Anthropic()
TODAY = date.today().isoformat()

# Fields a listing can never give you honestly. These drive the RFQ.
#
# Round one. `forged_confirmed` and `piece_count` are deliberately separate
# questions: a cast 3-piece is a real, honest product, so neither field may
# ever stand in for the other. `forging_process` keeps their claim verbatim,
# which is what lets the flag logic tell press forging from flow forming.
QUOTE_ONLY = {
    # Every price on the site is a 3-piece price. Say so in the question, or
    # you get one vendor's 2-piece quote sitting in the same column as another
    # vendor's 3-piece and the table silently compares different products.
    # Hantuo quoted 2-piece, then quoted 3-piece when asked, and the second
    # answer overwrote the first: it read as a price rise and cost them their
    # ranking for answering an extra question.
    "price_per_wheel_1set":  "For the 3-PIECE construction: price per single wheel, and price for 1 set (4 pcs)",
    "price_per_wheel_4set":  None,   # asked together with 1set, stored separately
    "forging_process":       "Is the wheel press-forged, or flow-formed / rotary-forged? What press tonnage, and which alloy?",
    # A capability question, not a description of one product. Suppliers
    # routinely make all three, and the answer is a set.
    "piece_count":           "Which constructions can you make: monoblock (single-piece), 2-piece, 3-piece? List every one you offer, not just this listing",
    "cert_document":         "Which certifications do you have? (DOT, JWL, VIA, TUV, etc.) Does it cover this width? Certificate PDF?",
    "freight_cif_usd":       "Total landed cost to Los Angeles CA 90210: all shipping, duty, port fees, tariffs, and other costs to get the wheels in our warehouse",
    "hs_code":               "HS code / tariff classification for your wheels",
    "is_manufacturer":       "Are you the manufacturer or a trading company? Factory address?",
}

# Round two. These matter when you are close to ordering, not at first
# contact. Sending all fourteen questions cold tanks the response rate, so
# they wait until the round-one basics are in.
ROUND2 = {
    "cnc_inhouse":    "Do you do the CNC machining in house, or outsource it?",
    "heat_treat":     "What is your heat treat process? Which temper, and how long?",
    "payment_terms":  "What are your payment terms? Is it 50% deposit?",
    "defect_policy":  "What happens if a wheel arrives out of round or with finish damage?",
}

ALL_FIELDS = {**QUOTE_ONLY, **ROUND2}

# Answers we keep but never ask as a numbered question. REPLY_SCHEMA already
# extracts all four: weight and load rating because suppliers volunteer them
# unprompted, forged_confirmed because it is derived from forging_process
# rather than asked, size_chart because they either attach one or do not.
#
# They are not in ALL_FIELDS, and the copy loops used to iterate ALL_FIELDS,
# so the model returned these values and the code dropped them on the floor.
# Meanwhile ROUND1_CORE gated on them and _flags() penalised their absence:
# suppliers were being marked down for answers we had thrown away. Taizhou
# Gushi replied "Weight is about 11 kg, load capacity is 850 kg" and both
# fields stayed null. Older records still hold values only because these
# fields were in the asked set before the question set was trimmed.
EXTRACTED_ONLY = ("weight_kg", "load_rating_kg", "forged_confirmed", "size_chart",
                  # Which construction the stored price is actually for. Not a
                  # question: it qualifies the price. Without it a 2-piece
                  # quote and a 3-piece quote sit in the same column looking
                  # comparable, and ask() treats the 2-piece one as answered
                  # and never asks again.
                  "quote_construction")

# What gets persisted. ALL_FIELDS stays the *asked* set, so it remains the
# denominator for questions_answered: a supplier has not failed to answer
# something nobody put to them.
STORED_FIELDS = tuple(ALL_FIELDS) + EXTRACTED_ONLY

# Constructions, in the order they are displayed. Also the whitelist: a reply
# saying "all options" or "any style" is not a capability, it is a sales line.
CONSTRUCTIONS = ("monoblock", "2-piece", "3-piece")

# Every price on the sheet is for this construction. Pricing is only
# comparable if the thing being priced is the same, and construction drives
# cost harder than anything else: a 3-piece has a barrel, a lip and about
# thirty bolts that a monoblock does not. Which constructions a supplier can
# build is a separate question, answered as a set of tags.
QUOTE_CONSTRUCTION = "3-piece"

_CONSTRUCTION_ALIASES = {
    "single-piece": "monoblock", "single piece": "monoblock",
    "1-piece": "monoblock", "one-piece": "monoblock", "1 piece": "monoblock",
    "two-piece": "2-piece", "2 piece": "2-piece",
    "three-piece": "3-piece", "3 piece": "3-piece",
}


# Fields where a changed answer is a real contradiction worth flagging.
# Deliberately only numbers and booleans. The prose fields are the model's
# summary of what a supplier wrote, and re-reading the same reply rewords it:
# Gushi's defect_policy went from "compensation provided if damage occurs" to
# "compensation provided for damage" and earned a "changed answer between
# rounds" flag for a sentence the supplier never revised.
REVISION_FIELDS = (
    "price_per_wheel_1set", "price_per_wheel_4set", "freight_cif_usd",
    "weight_kg", "load_rating_kg", "forged_confirmed", "is_manufacturer",
    "cnc_inhouse", "hs_code",
)


def merge_constructions(prev, new):
    """Union of what a supplier says they can build.

    piece_count is a capability set, not a description of whichever product
    they last quoted, so it only ever grows. Overwriting it lost the earlier
    answer and logged a revision, which put "changed answer between rounds" on
    a supplier who had simply answered a second question: Hantuo said 2-piece
    in round one, said 3-piece when asked for a 3-piece quote, and got flagged
    for contradicting themselves.
    """
    def norm(v):
        if v in (None, ""):
            return []
        out = []
        for item in (v if isinstance(v, list) else [v]):
            s = str(item).strip().lower()
            s = _CONSTRUCTION_ALIASES.get(s, s)
            if s in CONSTRUCTIONS:
                out.append(s)
        return out

    seen = set(norm(prev)) | set(norm(new))
    return [c for c in CONSTRUCTIONS if c in seen] or None


# Round two unlocks once these are known. Not the whole of round one: a
# supplier who answered everything except the size chart has clearly engaged,
# and holding the detail questions back over one soft field wastes a round.
ROUND1_CORE = ("price_per_wheel_1set", "weight_kg", "load_rating_kg",
               "forged_confirmed", "piece_count", "freight_cif_usd")

# Rendered as bullets at the end rather than numbered questions. These are
# nice-to-haves, and burying them keeps the numbered list short enough that
# suppliers actually answer all of it.
EXTRAS = set()  # No extras currently

# Rough weight model for a forged monoblock: kg ~= K * diameter * width.
# K = 0.055 puts an 18x9.5 at about 9.4 kg, which matches the light end of
# real forged wheels. CALIBRATE THIS once you have a dozen real quotes with
# verified weights; it is an estimate, not a measurement.
FORGED_K = 0.055
MONOBLOCK_TOLERANCE = 1.25   # honest monoblocks land under 25% over model
MULTIPIECE_TOLERANCE = 1.45  # barrel, lip and ~30 bolts are real weight

PHOTO_ASK = "A photo of the wheel on a scale, and one showing the back of the spokes"


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
  store_rating        number or null, e.g. 4.8 from "4.8/5"
  review_count        integer or null, e.g. 18 from "4.8/5(18)"
  response_time       string as shown, e.g. "<=2h"
  on_time_dispatch    string as shown, e.g. ">=89%"
  reorder_rate        string as shown, e.g. "18%"
  units_sold          integer or null, e.g. 240 from "240 sold"
  transit_time        string as shown, e.g. "Est. 17-24 days"

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
                              "years_on_platform", "certs_claimed", "response_rate",
                              "store_rating", "review_count", "response_time",
                              "on_time_dispatch", "reorder_rate", "units_sold",
                              "moq", "transit_time"):
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
                for k in STORED_FIELDS:
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
                    "from_quote": {k: None for k in STORED_FIELDS},
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
        # Iterate the field definitions, not the record: YAML round-trips
        # alphabetise keys, and this also ignores keys left over from an
        # older question set while backfilling any newly added ones.
        for k in STORED_FIELDS:
            rec["from_quote"].setdefault(k, None)

        def answered(k):
            # A price quoted for the wrong construction is not an answer to
            # the question we now ask. Every figure on the sheet is a
            # QUOTE_CONSTRUCTION price, so a 2-piece quote left over from
            # before that rule has to be asked again, or it sits in the table
            # looking comparable to a 3-piece one forever. Unknown is left
            # alone: re-asking everyone who never named a construction would
            # spam suppliers who may well have quoted the right thing.
            if k in ("price_per_wheel_1set", "price_per_wheel_4set", "freight_cif_usd"):
                qc = rec["from_quote"].get("quote_construction")
                if qc and qc != QUOTE_CONSTRUCTION:
                    return False
            return rec["from_quote"].get(k) not in (None, "")

        # Round two once the supplier has actually engaged. The gate exists to
        # keep first contact short, not to withhold questions forever: requiring
        # every ROUND1_CORE field meant one dodged answer (piece_count, always)
        # pinned a supplier in round one no matter how many times they replied.
        # Anyone who has answered a core field or sent a reply has engaged.
        replied = bool((rec.get("meta") or {}).get("reply_received"))
        round2 = replied or all(answered(k) for k in ROUND1_CORE)
        pool = {**QUOTE_ONLY, **ROUND2} if round2 else QUOTE_ONLY

        missing = [k for k in pool if not answered(k)]
        if not missing:
            continue

        # Only ask what the listing could not answer. Shorter asks get
        # better response rates and signal you actually read their page.
        numbered = [k for k in missing if k not in EXTRAS and pool.get(k)]
        extras = [k for k in missing if k in EXTRAS and pool.get(k)]

        qs = "\n".join(f"  {i}. {pool[k]}" for i, k in enumerate(numbered, 1))

        ex = [pool[k] for k in extras]
        ex_block = "\n".join(f"  - {e}" for e in ex) if ex else ""

        body = f"""Hello,

I'm looking at your wheels for my own car and would like a quote.

Reference spec for all pricing:
  {size} \u00b7 {offset} \u00b7 {pcd} \u00b7 {finish} \u00b7 {QUOTE_CONSTRUCTION}

Could you confirm:

{qs}

{f"An extra if you can:{chr(10)}{chr(10)}{ex_block}" if ex else ""}

Thank you.""".strip() + "\n"
        (out / f"{rec['id']}.txt").write_text(body, encoding="utf-8")
        # rfq_sent is the latest ask, which is what reply_days measures from.
        # Keep the first ask separately so re-running this does not erase when
        # contact actually started.
        rec["meta"].setdefault("rfq_first_sent", TODAY)
        rec["meta"]["rfq_sent"] = TODAY
        rec["meta"]["rfq_round"] = 2 if round2 else 1
        rec["meta"]["ref_spec"] = f"{size} {offset} {pcd} {finish} {QUOTE_CONSTRUCTION}"
        f.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"  {rec['id']}: round {2 if round2 else 1}, asking {len(missing)} questions")

    print(f"-> {out}  (paste these into Alibaba chat)")


# --------------------------------------------------------------------------
# 3. ingest
# --------------------------------------------------------------------------
REPLY_SCHEMA = """
This is a supplier's reply to a sourcing enquiry, often in broken English.
Return one JSON object. Extract ONLY what the supplier actually stated.

  price_per_wheel_1set   number or null (USD). We always ask for the 3-piece
                         price. If they quote several constructions, take the
                         3-piece figure. If they quote only one construction
                         and do not say which, take it.
  price_per_wheel_4set   number or null. Same rule.
  weight_kg              number or null
  load_rating_kg         number or null
  forged_confirmed       true | false | null. TRUE only for genuine press
                         forging. "Machined from a forged blank", "forged
                         billet" and "6061-T6 forged" all describe normal
                         forging and are TRUE. FALSE when they describe flow
                         forming, rotary forging or a spun barrel, which mean
                         a CAST centre. NULL if they never addressed it, or
                         said only "forged" with no process detail at all.
  piece_count            ARRAY of every construction the supplier says they
                         can make, from "monoblock", "2-piece", "3-piece".
                         Null if they never addressed it. This is a
                         CAPABILITY, not a description of one product: a
                         supplier who says "we make all three" is
                         ["monoblock", "2-piece", "3-piece"], and one who
                         quotes a 3-piece but mentions offering 2-piece is
                         ["2-piece", "3-piece"]. Do not narrow the list to
                         whichever one they happened to quote.
                         INDEPENDENT of forging. A cast 3-piece is a real,
                         honest product and a forged monoblock is not
                         "more pieces". Never infer either field from the
                         other, and never guess one because you know the other.
  forging_process        string or null. Record their claim verbatim in
                         summary, e.g. "press forged, 6000T press, 6061-T6"
                         or "flow formed" or "rotary forged" or
                         "not stated clearly". Note: "machined from a forged
                         blank" describes normal forging and is NOT a
                         downgrade. "Flow formed" and "rotary forged" mean a
                         CAST centre with a spun barrel -- record those terms
                         exactly, do not translate them to "forged".
  quote_construction     "monoblock" | "2-piece" | "3-piece" | null. Which
                         construction the price above is for. If they name it
                         ("the three-piece style is 550 USD"), use that. If
                         they give one price and never say, null.
  cert_document          "provided" | "promised" | "claimed without document" | null
  size_chart             string or null. If they listed sizes or attached a
                         chart, summarise the range, e.g. "17-20in, 8-12J,
                         ET-20 to +45, chart attached". Null if not addressed.
  freight_cif_usd        number or null
  is_manufacturer        true | false | null
  cnc_inhouse            true | false | null. TRUE only if they say the
                         machining happens at their own factory.
  heat_treat             string or null, their process verbatim, e.g.
                         "T6, solution then age 8h". Null if not addressed.
  payment_terms          string or null, e.g. "50% deposit, 50% before shipping"
  defect_policy          string or null. What they say happens if a wheel
                         arrives out of round or with finish damage.
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
- forged_confirmed and piece_count are separate axes. Answering one tells you
  nothing about the other. Leave either null rather than deducing it.
- forged_confirmed must agree with forging_process. If the process they
  describe is flow forming, forged_confirmed is false even when they used the
  word "forged" elsewhere in the message.
- Never fill a field the supplier did not address.

Output ONLY JSON.
"""


def _reply_hash(text):
    """One definition, so mark_parsed and ingest can never disagree and turn
    a cache hit into a silent miss (or worse, a silent skip)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def mark_parsed(wheel):
    """Record every reply's hash without calling the model.

    Adopting the cache on an existing archive would otherwise cost one last
    full re-parse, and a re-parse is not free of consequences: the model is
    not deterministic, so re-reading a reply rewrites its extracted values
    with a slightly different wording every time. This says "what is already
    stored is what these replies said" and starts the cache from there.
    """
    rep, n = wdir(wheel) / "replies", 0
    for target in sorted(sdir(wheel).glob("*.yaml")):
        rec = yaml.safe_load(target.read_text())
        seen = rec.setdefault("meta", {}).setdefault("parsed_replies", {})
        for f in sorted(rep.glob(f"{target.stem}__*.txt")):
            seen[f.name] = _reply_hash(f.read_text(encoding="utf-8"))
            n += 1
        target.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"  marked {n} reply(s) as already parsed. ingest will now only read new ones.")


def ingest(wheel, force=False, workers=6):
    rep = wdir(wheel) / "replies"
    if not rep.exists():
        raise SystemExit(f"put raw replies in {rep}/ named supplier-id__YYYY-MM-DD.txt")

    # Work out what actually needs the model before calling it. Ingest used to
    # re-parse every reply on every run, so the cost grew with the whole
    # archive rather than with what you just pasted: 23 files, one new, 23
    # calls. The hash is of the file's content, so an edited reply reparses on
    # its own. `--force` exists because adding a field to ALL_FIELDS means
    # backfilling it from the stored replies, which needs the full re-read.
    todo, skipped = [], 0
    for f in sorted(rep.glob("*.txt")):
        sid, _, datestr = f.stem.partition("__")
        # Several replies can land on the same day, so app.py writes the
        # second as "__2026-08-07-2". Keep only the date: the suffix used to
        # land in reply_received verbatim, giving "2026-08-07-3", which is not
        # a date, and which then made reply_days throw and be silently
        # skipped. It only looked fine because ".txt" sorts after "-2.txt", so
        # the unsuffixed file was normally written last and overwrote it.
        m = re.match(r"\d{4}-\d{2}-\d{2}", datestr)
        datestr = m.group(0) if m else ""
        target = sdir(wheel) / f"{sid}.yaml"
        if not target.exists():
            print(f"  no record for '{sid}', skipping {f.name}")
            continue
        text = f.read_text(encoding="utf-8")
        digest = _reply_hash(text)
        if not force:
            seen = (yaml.safe_load(target.read_text()).get("meta") or {}).get("parsed_replies") or {}
            if seen.get(f.name) == digest:
                skipped += 1
                continue
        todo.append((f, sid, datestr, target, text, digest))

    if skipped:
        print(f"  {skipped} reply(s) unchanged, skipping. --force to re-read all.")
    if not todo:
        print("  nothing new to parse.")
        return

    # The model calls are independent, so they overlap. Applying the results
    # does NOT: one supplier can have a dozen replies and each is folded into
    # the same YAML cumulatively, so parallel read-modify-write would drop
    # answers. Fan out the parsing, then apply in sorted order exactly as
    # before.
    parsed = {}
    if len(todo) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
            futs = {pool.submit(_ask_model, t[4], REPLY_SCHEMA): t[0] for t in todo}
            for fut in as_completed(futs):
                parsed[futs[fut]] = fut.result()
    else:
        for t in todo:
            parsed[t[0]] = _ask_model(t[4], REPLY_SCHEMA)

    for f, sid, datestr, target, text, digest in todo:
        rec = yaml.safe_load(target.read_text())
        for k in STORED_FIELDS:
            rec["from_quote"].setdefault(k, None)
        got = parsed[f]

        # Store everything the schema extracted, count only what was asked.
        filled = 0
        for k in STORED_FIELDS:
            if got.get(k) not in (None, ""):
                if k == "piece_count":
                    rec["from_quote"][k] = merge_constructions(
                        rec["from_quote"].get(k), got[k])
                else:
                    rec["from_quote"][k] = got[k]
                if k in ALL_FIELDS:
                    filled += 1

        # Auto-confirm forging if they described a forging process and didn't
        # explicitly say "not forged". "Made with forged blanks" = forged.
        if got.get("forging_process") and rec["from_quote"].get("forged_confirmed") is None:
            fp = str(got.get("forging_process", "")).lower()
            if not any(x in fp for x in ("flow form", "flow-form", "rotary forg", "spun", "spinning", "not forged", "cast")):
                if any(x in fp for x in ("forg", "press", "blank", "billet")):
                    rec["from_quote"]["forged_confirmed"] = True

        # Cumulative across every reply, matching what the GUI reports. A
        # per-reply count made a supplier's second message look like a regression.
        total_filled = sum(1 for k in ALL_FIELDS
                           if rec["from_quote"].get(k) not in (None, ""))

        asked = rec["meta"].get("rfq_sent")
        rec["meta"]["reply_received"] = datestr or TODAY
        rec["meta"]["questions_answered"] = f"{total_filled}/{len(ALL_FIELDS)}"
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
        # Recorded only after everything above succeeded, so a crash mid-parse
        # leaves the file looking unparsed and it gets retried next run.
        rec["meta"].setdefault("parsed_replies", {})[f.name] = digest
        target.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"  {sid}: +{filled} this reply, {total_filled}/{len(ALL_FIELDS)} total  {rec['flags'] or ''}")


def _ref_size(rec):
    """Diameter and width from the spec this supplier was quoted on."""
    m = re.search(r"(\d{2})\s*[xX]\s*(\d+\.?\d*)",
                  str((rec.get("meta") or {}).get("ref_spec") or "18x9.5"))
    return (float(m.group(1)), float(m.group(2))) if m else (18.0, 9.5)


# Principle 5 says a false accusation against an honest supplier is the worst
# thing this file can produce, and a bare `"flow-form" in fp` produces one:
# Taizhou Hantuo wrote "fully forged from 6061-T6 billets, not flow-formed"
# and got flagged for the exact process it ruled out. Match the claim only
# where the supplier did not negate it.
_NEGATOR = re.compile(
    r"\b(not|never|no|non|isn'?t|aren'?t|doesn'?t|don'?t|rather than|instead of|"
    r"as opposed to|without)\b[\s,\-]*$"
)


def _asserted(text, keywords, window=30):
    """True if a keyword appears in text without a negation just before it."""
    for k in keywords:
        for m in re.finditer(re.escape(k), text):
            if not _NEGATOR.search(text[max(0, m.start() - window):m.start()]):
                return True
    return False


def _flags(rec):
    fl, L, Q = [], rec["from_listing"], rec["from_quote"]
    meta = rec.get("meta") or {}
    p = Q.get("price_per_wheel_1set")

    # Forged and piece count are independent axes. Each reads its own field,
    # and neither is ever deduced from the other. Where a quoted answer
    # exists it wins outright; the listing is only a fallback, and any flag
    # resting on it says so, because a listing claim is not a quoted fact.
    quoted_forged = Q.get("forged_confirmed")
    listing_forged = "forged" in str(L.get("construction", "")).lower()
    forged = listing_forged if quoted_forged is None else bool(quoted_forged)
    src = "listing" if quoted_forged is None else "quoted"

    if forged and p and p < 150:
        fl.append("price implausible for forged")
    if Q.get("cert_document") == "claimed without document":
        fl.append("cert claimed, no document")
    # The listing sells it as forged and the supplier will not say so.
    if listing_forged and quoted_forged is False:
        fl.append("listing says forged, supplier says not forged")

    w = Q.get("weight_kg")
    if forged and w:
        # Scale with the wheel. A flat ceiling accuses honest suppliers of
        # big sizes, and gives multi-piece no allowance for its hardware.
        dia, wid = _ref_size(rec)
        # The quote, and therefore the weight quoted against it, is always for
        # QUOTE_CONSTRUCTION. Do not read the allowance off piece_count: that
        # is now the set of constructions a supplier *can* build, and someone
        # who also offers monoblock is not quoting one.
        multi = QUOTE_CONSTRUCTION in ("2-piece", "3-piece")
        ceiling = FORGED_K * dia * wid * (MULTIPIECE_TOLERANCE if multi else MONOBLOCK_TOLERANCE)
        if w > ceiling:
            fl.append(f"weight {w}kg high for forged ({src}) at {dia:g}x{wid:g} "
                      f"(expected under {ceiling:.1f}kg)")
    if Q.get("is_manufacturer") is True and "trading" in str(L.get("business_type", "")).lower():
        fl.append("claims factory, profile says trading")
    if Q.get("load_rating_kg") is None:
        fl.append("no load rating given")
    # Falsy covers the empty list merge_constructions can no longer produce,
    # but would otherwise slip past an `in (None, "")` test.
    if not Q.get("piece_count"):
        fl.append("constructions offered not confirmed")
    fp = str(Q.get("forging_process") or "").lower()
    # Every forged wheel is machined from a forged blank -- that IS the
    # process, so it is not a red flag. The actual fraud vector is a cast
    # centre with a spun barrel being sold as forged.
    if _asserted(fp, ("flow form", "flow-form", "rotary forg", "spun", "spinning")):
        fl.append("flow formed, cast centre, not press forged")
    if fp and not any(k in fp for k in ("press", "forg", "ton", "flow", "rotary", "spun")):
        fl.append("forging process not actually described")
    if Q.get("forging_process") and "not stated" in fp:
        fl.append("dodged the forging question")
    if Q.get("size_chart") in (None, ""):
        fl.append("no size chart supplied")
    if (meta.get("reply_days") or 0) > 3:
        fl.append("slow to reply")
    if (L.get("years_on_platform") or 99) < 3:
        fl.append("under 3 years listed")
    # Derived here rather than appended by the caller. The GUI used to tack
    # this on after _flags() returned, so every recompute.py run silently
    # wiped it. meta.revisions is the durable record, so read that instead.
    if meta.get("revisions"):
        fl.append("changed answer between rounds")
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
    for name in ("pull", "ask", "ingest", "check", "mark-parsed"):
        p = sub.add_parser(name); p.add_argument("--wheel", required=True)
        if name == "ask":
            p.add_argument("--size", default="18x9.5J"); p.add_argument("--offset", default="ET+22")
            p.add_argument("--pcd", default="5x114.3"); p.add_argument("--finish", default="Bronze")
        if name == "ingest":
            p.add_argument("--force", action="store_true",
                           help="re-read every reply, not just changed ones. Needed after adding a field to ALL_FIELDS.")
            p.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    if a.cmd == "pull": pull(a.wheel)
    elif a.cmd == "ask": ask(a.wheel, a.size, a.offset, a.pcd, a.finish)
    elif a.cmd == "ingest": ingest(a.wheel, a.force, a.workers)
    elif a.cmd == "mark-parsed": mark_parsed(a.wheel)
    elif a.cmd == "check": check(a.wheel)
