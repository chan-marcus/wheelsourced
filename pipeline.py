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
# Every price on the sheet is for this construction, and a quote for anything
# else is discarded rather than stored. Pricing only compares if the thing
# being priced is the same, and construction drives cost harder than anything
# else: a 3-piece has a barrel, a lip and about thirty bolts a monoblock does
# not. Which constructions a supplier can build is a separate question,
# answered as a set of tags.
#
# Defined here, above the question set, because the question text is built
# from it. They were once written out separately and drifted: the constant
# said 2-piece while the RFQ still asked for a 3-PIECE price, so every reply
# was answering a question whose answer the ingest guard then threw away, and
# ask() re-sent it forever.
QUOTE_CONSTRUCTION = "2-piece"

# The questions are read by sales staff working in a second language, and
# they are answered one number at a time. Keep each one to a single line of
# short sentences, ask one thing per sentence, and prefer the plain word:
# "factory" over "manufacturer", "how is it made" over "what process". A
# question that has to be re-read is a question that gets skipped, and a
# skipped question is a null on the sheet.
QUOTE_ONLY = {
    # No longer names the construction directly, on request. It is still in
    # the reference spec block above the questions, but that is context, not
    # a direct ask, and the direct ask is what used to make a supplier price
    # QUOTE_CONSTRUCTION specifically rather than whatever they build by
    # default. A supplier who prices something else still has that price
    # discarded by take_quote() and asked again, same as before; expect more
    # replies to land in meta.off_basis_quotes instead of on the sheet.
    "price_per_wheel_1set":  "Price for one wheel, and for a set of 4.",
    "price_per_wheel_4set":  None,   # asked together with 1set, stored separately
    "forging_process":       "How is it made: press-forged, or flow-formed (rotary forged)? What press tonnage? Which alloy?",
    # A capability question, not a description of one product. Suppliers
    # routinely make all three, and the answer is a set. The second sentence
    # is what stops them answering only for the wheel in the listing.
    "piece_count":           "Which types can you make: monoblock (1-piece), 2-piece, 3-piece? List all, not only this wheel.",
    # Two answers in one question, and both are kept: certs_claimed takes the
    # names, cert_document takes whether the certificate arrived. Drop the
    # second sentence and cert_document stops being asked at all.
    "cert_document":         "Which certifications does this wheel have? (DOT, JWL, VIA, TUV, etc.) Can you send the certificate?",
    # "Total cost to deliver" reads as the grand total, not the shipping half,
    # and Maxtera answered it that way three times: "the total price is 1500
    # USD, including wheels, shipping fees, and customs duties". That is a
    # landed figure, and it went into a column that means ex-freight. Ask for
    # shipping on its own. Suppliers will still answer all-in sometimes, which
    # is what landed_total_usd is for.
    "freight_cif_usd":       "How much is the shipping to Los Angeles CA 90210, USA? Please give the shipping cost by itself. Do not include the price of the wheels. Include duty, port fees and tariffs.",
    # Two answers on one line, the way price 1set/4set and the old weight pair
    # are. Suppliers already volunteer both together, unprompted and in this
    # order: "production cycle 30-40 days, transportation 25-30 days".
    # shipping_time carries no question text of its own, see PAIRED_WITH.
    "manufacturing_time":    "How many days to make the wheels? How many days to ship them to Los Angeles CA 90210, USA?",
    "shipping_time":         None,   # asked with manufacturing_time, stored separately
    # hs_code was asked here but never appeared in REPLY_SCHEMA below, so
    # nothing was ever extracted from an answer no matter how clearly a
    # supplier gave one: a dead question from the start, confirmed against
    # every stored record before removal. Not something we need, dropped
    # rather than kept.
    "is_manufacturer":       "Are you the factory, or a trading company? What is the factory address?",
}

# Round two. These matter when you are close to ordering, not at first
# contact. Sending all ten questions cold tanks the response rate, so they
# wait until the round-one basics are in.
ROUND2 = {
    "cnc_inhouse":    "Do you do the CNC machining in your own factory, or outsource it?",
    "heat_treat":     "What is your heat treatment process? Which temper (for example T6)? How many hours?",
    "payment_terms":  "What are your payment terms? Is it 50% deposit?",
    "defect_policy":  "If a wheel arrives out of round, or the finish is damaged, what do you do?",
}

ALL_FIELDS = {**QUOTE_ONLY, **ROUND2}

# Fields answered inside somebody else's question, mapped to the question that
# carries them. Written out rather than inferred from "the entry above with
# text", because that is dict order doing load-bearing work, and because ask()
# needs the mapping in reverse to re-send a question whose second half went
# unanswered.
PAIRED_WITH = {
    "price_per_wheel_4set": "price_per_wheel_1set",
    "shipping_time":        "manufacturing_time",
}

# Every text-less field must name its carrier, or it silently becomes a field
# that is stored, counted in the denominator, and never actually asked.
_orphans = [k for k, v in ALL_FIELDS.items() if not v and k not in PAIRED_WITH]
if _orphans:
    raise RuntimeError(f"asked fields with no question and no pairing: {_orphans}")

# Answers we keep but never ask as a numbered question.
#
# Weight and load rating were a question for exactly one commit. Not worth
# the line in a cold message, so back here, and the "no load rating given"
# flag goes with it: a flag has to be answerable, and this is no longer a
# question anyone can fail to answer. Still recorded when a supplier
# volunteers one, which is most of the time; only the asking stopped.
#
# size_chart is the same shape, having made the same round trip one commit
# earlier for the same reason.
EXTRACTED_ONLY = ("size_chart", "weight_kg", "load_rating_kg",
                  # Derived from forging_process by the auto-confirm below, and
                  # from their own words about flow forming. Asking "is it
                  # really forged" invites a yes from someone who has already
                  # described spinning a cast centre.
                  "forged_confirmed",
                  # Which construction the stored price is actually for. Not a
                  # question: it qualifies the price. Without it a 2-piece
                  # quote and a 3-piece quote sit in the same column looking
                  # comparable, and ask() treats the 2-piece one as answered
                  # and never asks again.
                  "quote_construction",
                  # WHICH certifications they name, as a set of tags.
                  # cert_document is the numbered question and stays the
                  # denominator; this is the other half of the same answer,
                  # so it is kept rather than asked. Two axes, like forged
                  # and piece_count: holding DOT and having sent the
                  # certificate are different facts. Weiluo wrote "our
                  # products are DOT & VIA certified" and the sheet showed
                  # only "claimed without document", throwing away the part
                  # of the answer the question actually asked for.
                  "certs_claimed",
                  # An all-in price: wheels plus shipping plus duties, quoted
                  # as one number the supplier will not break down. Same split
                  # as forged vs piece_count, one column over: "what do the
                  # wheels cost" and "what does the whole thing cost delivered"
                  # are different questions, and a landed number dropped into
                  # price_per_wheel_4set is compared against everyone else's
                  # ex-freight figure.
                  #
                  # Not a numbered question. The RFQ already asks for the price
                  # and the shipping separately; this is where the answer goes
                  # when a supplier declines to separate them, the same way
                  # off_basis_quotes keeps a price we cannot use in the column
                  # it was quoted for. Extracted rather than asked, so it never
                  # counts against a supplier who did give the breakdown.
                  "landed_total_usd")

# What gets persisted. ALL_FIELDS stays the *asked* set, so it remains the
# denominator for questions_answered: a supplier has not failed to answer
# something nobody put to them.
STORED_FIELDS = tuple(ALL_FIELDS) + EXTRACTED_ONLY

# What each stored field is called in the interface. The sheet's spec grid
# already had these names hand-written into the markup while the GUI derived
# its own from the key, so the same value was "defect policy" in one place and
# "Damage policy" in the other, and "freight cif usd" against "Est. shipping
# cost". Named once here so the two cannot drift.
#
# Certification is two fields, kept apart for the reason on certs_claimed: the
# names and whether a certificate exists are different answers. The sheet
# prints them in one cell; the GUI has to edit them separately.
FIELD_LABELS = {
    "price_per_wheel_1set": "Per wheel",
    "price_per_wheel_4set": "Set of 4",
    "forging_process":      "Process stated",
    "piece_count":          "Pieces",
    "certs_claimed":        "Certification",
    "cert_document":        "Certificate on file",
    "freight_cif_usd":      "Est. shipping cost",
    "manufacturing_time":   "Manufacturing time",
    "shipping_time":        "Shipping time",
    "is_manufacturer":      "Manufacturer",
    "cnc_inhouse":          "CNC",
    "heat_treat":           "Heat treat",
    "payment_terms":        "Payment terms",
    "defect_policy":        "Damage/Defect policy",
    "weight_kg":            "Weight (kg)",
    "load_rating_kg":       "Load rating (kg)",
    "forged_confirmed":     "Forged",
    "size_chart":           "Size chart",
    "quote_construction":   "Quoted construction",
    "landed_total_usd":     "Total delivered",
}


# What the GUI puts under each of the kept-but-not-asked fields. ALL_FIELDS
# carries the question text and doubles as the tooltip there; these have no
# question to show, and without a line each they were simply absent from the
# GUI. That left certs_claimed invisible in the only tool for correcting it,
# while it was being published on the sheet.
EXTRACTED_DESC = {
    "size_chart":        "The sizes or chart they offered, summarised. Not a numbered question, so never flagged when blank.",
    "weight_kg":         "Weight per wheel in kg. Not a numbered question, so never flagged when blank.",
    "load_rating_kg":    "Load rating per wheel in kg. Not a numbered question, so never flagged when blank.",
    "forged_confirmed":  "true or false. Drives the Forged column and the forging flags. Flow formed is not forged.",
    "quote_construction": f"Which construction the stored price is for. Anything but {QUOTE_CONSTRUCTION} means the price was set aside.",
    "certs_claimed":     "Certifications they name, comma separated. DOT, JWL, VIA, TUV, ECE, SFI, ISO 9001, IATF 16949, CE.",
    "landed_total_usd":  "All-in price for a set of 4 delivered: wheels plus shipping plus duties, when they quote one number and will not split it. Leave the price and shipping fields blank in that case.",
}

# EXTRACTED_DESC is what makes an EXTRACTED_ONLY field visible in the GUI.
# size_chart went back to EXTRACTED_ONLY without going back into this dict,
# which stored it correctly and made it impossible to see or hand-correct:
# the exact gap the extracted/asked split exists to close. Checked at import
# so the two cannot drift apart silently again.
if set(EXTRACTED_DESC) != set(EXTRACTED_ONLY):
    raise RuntimeError(
        f"EXTRACTED_DESC / EXTRACTED_ONLY mismatch: "
        f"{set(EXTRACTED_ONLY) ^ set(EXTRACTED_DESC)}")


def parse_set_field(k, raw):
    """Turn a typed-in, comma separated value into the stored tag list.

    The GUI edits every field as a text box, so without this a hand-corrected
    piece_count is stored as the string "monoblock, 2-piece" where the rest of
    the code expects a list, and a hand-typed certification bypasses the
    whitelist that keeps sales lines out. Same normalisers the parser uses.
    """
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if k == "piece_count":
        return merge_constructions(None, parts)
    if k == "certs_claimed":
        return cert_names(parts) or None
    return raw

# Constructions, in the order they are displayed. Also the whitelist: a reply
# saying "all options" or "any style" is not a capability, it is a sales line.
CONSTRUCTIONS = ("monoblock", "2-piece", "3-piece")

_CONSTRUCTION_ALIASES = {
    "single-piece": "monoblock", "single piece": "monoblock",
    "1-piece": "monoblock", "one-piece": "monoblock", "1 piece": "monoblock",
    "two-piece": "2-piece", "2 piece": "2-piece",
    "three-piece": "3-piece", "3 piece": "3-piece",
}


# The price fields, which are only meaningful for QUOTE_CONSTRUCTION.
# landed_total_usd is one of them: an all-in figure for a 3-piece is no more
# comparable than a 3-piece unit price, and leaving it out would let the guard
# be walked straight around by a supplier who quotes delivered rather than ex
# works.
PRICE_FIELDS = ("price_per_wheel_1set", "price_per_wheel_4set", "freight_cif_usd",
                "landed_total_usd")


def take_quote(got, rec):
    """Drop a price quoted for the wrong construction, keeping a record of it.

    Enforced here rather than in the prompt. Asking the model to withhold a
    number it can plainly see is the least reliable instruction you can give
    it, and a 3-piece price silently landing in the 2-piece column is exactly
    the failure this whole split exists to prevent. The model's job is to say
    what the price is and which construction it is for; deciding whether to
    keep it is the code's.

    Mutates `got` in place and returns the construction that was rejected, or
    None. The rejected figures go to meta.off_basis_quotes: a 3-piece quote is
    a real answer from the supplier and publishing the nulls does not mean
    destroying it, it just cannot sit in a column that compares 2-piece.
    """
    qc = got.get("quote_construction")
    if not qc or qc == QUOTE_CONSTRUCTION:
        return None
    if not any(got.get(k) not in (None, "") for k in PRICE_FIELDS):
        return None

    stash = {"construction": qc, "seen": TODAY}
    for k in PRICE_FIELDS:
        if got.get(k) not in (None, ""):
            stash[k] = got.pop(k)
    got["quote_construction"] = None

    off = rec.setdefault("meta", {}).setdefault("off_basis_quotes", [])
    if not any(o.get("construction") == qc and
               o.get("price_per_wheel_1set") == stash.get("price_per_wheel_1set")
               for o in off):
        off.append(stash)
    return qc


def answered_keys(Q):
    """Which asked questions this record has an answer for.

    A set of ALL_FIELDS keys, so the count can never exceed the denominator
    however the fields are populated.

    An all-in total counts as the set-of-4 price, because that is what it is:
    a price for four wheels, inclusive of shipping. Counting it as nothing
    took a point off Maxtera for answering "1500 USD including wheels,
    shipping and duties" instead of quoting ex works, which is principle 3
    read in the mirror, the same way the piece_count overwrite once cost
    Hantuo their ranking. It does NOT count as the shipping question: that
    answer is inside the total and cannot be recovered from it, so ask()
    rightly keeps asking for the split.
    """
    got = {k for k in ALL_FIELDS if Q.get(k) not in (None, "")}
    if Q.get("landed_total_usd") not in (None, ""):
        got.add("price_per_wheel_4set")
    return got


# Fields where a changed answer is a real contradiction worth flagging.
# Deliberately only numbers and booleans. The prose fields are the model's
# summary of what a supplier wrote, and re-reading the same reply rewords it:
# Gushi's defect_policy went from "compensation provided if damage occurs" to
# "compensation provided for damage" and earned a "changed answer between
# rounds" flag for a sentence the supplier never revised.
REVISION_FIELDS = (
    "price_per_wheel_1set", "price_per_wheel_4set", "freight_cif_usd",
    "landed_total_usd",
    "weight_kg", "load_rating_kg", "forged_confirmed", "is_manufacturer",
    "cnc_inhouse",
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


# Certifications, in display order. A whitelist for the same reason
# CONSTRUCTIONS is one: "fully certified", "meets international standards"
# and "export quality" are sales lines, not certifications, and printing them
# in the answer column pays a vendor for saying nothing.
#
# Each row is (canonical name, pattern matched against a claimed token,
# spellings to look for in the raw reply). Patterns run against every token,
# so one token can yield two answers ("JWL/VIA") or one ("VIA A-351" is a VIA
# registration number, not a second certification). Normalising here is also
# what collapses the listing side's "IATF 16949" and "IATF16949" into one tag.
_CERT_TABLE = (
    ("DOT",        r"\bdot\b",                    ("dot",)),
    ("JWL",        r"\bjwl\b",                    ("jwl",)),
    ("VIA",        r"\bvia\b",                    ("via",)),
    ("TUV",        r"\bt[uü]v\b",                 ("tuv", "tüv")),
    ("ECE",        r"\bece\b|\be/?e?[\s-]?mark\b", ("ece", "e-mark", "emark", "e/e-mark")),
    ("SFI",        r"\bsfi\b",                    ("sfi",)),
    ("SAE",        r"\bsae\b",                    ("sae",)),
    ("FMVSS",      r"\bfmvss",                    ("fmvss",)),
    ("CCC",        r"\bccc\b",                    ("ccc",)),
    # No trailing \b: it cannot match between "iatf" and the digit in
    # "IATF16949", so the unspaced spelling was silently dropped. Same reason
    # FMVSS is written open-ended, for "FMVSS110/120".
    ("IATF 16949", r"\biatf",                     ("iatf",)),
    # TS 16949 was renamed IATF 16949 in 2016, but it keeps its own tag. A
    # supplier still naming the withdrawn standard has not told us they hold
    # the current one, and quietly upgrading the answer would be inventing a
    # certification on their behalf.
    ("TS 16949",   r"\bts\s*16949\b",             ("ts 16949", "ts16949")),
    ("ISO 9001",   r"\biso\s*9001\b",             ("iso 9001", "iso9001")),
    ("CE",         r"\bce\b",                     ("ce",)),
)
CERTS = tuple(c for c, _, _ in _CERT_TABLE)
_CERT_TERMS = {c: t for c, _, t in _CERT_TABLE}


def cert_names(v):
    """Canonical certification names found in one claimed value or list."""
    got = []
    for item in ([] if v in (None, "") else (v if isinstance(v, list) else [v])):
        s = str(item).lower()
        got += [name for name, pat, _ in _CERT_TABLE if re.search(pat, s)]
    return [c for c in CERTS if c in set(got)]


def unasserted_certs(new, text):
    """Names the model returned that the reply does not actually assert."""
    low = (text or "").lower()
    return [n for n in cert_names(new) if not _cert_asserted(low, _CERT_TERMS[n])]


def merge_certs(prev, new, text=None):
    """Union of the certifications a supplier claims.

    Accumulates like piece_count and for the same reason: naming DOT in round
    one and VIA in round two means they hold both, so the second answer must
    not erase the first. Never a revision either.

    `text` is the raw reply, and a name only survives if it appears in it
    un-negated. That gate is in code rather than in the prompt because both
    ways it can fail have already cost this file real money.

    The pasted thread contains OUR question, and our question lists DOT, JWL,
    VIA and TUV as examples. A model reading the whole conversation will hand
    those back as the supplier's answer, which credits a vendor with four
    certifications it never claimed: silence paying, which is principle 3.
    And because we name the four, suppliers answer in the negative constantly
    ("DOT yes, no TUV"), which is the exact shape that once turned a bare
    substring test into a false accusation against Taizhou Hantuo.

    Both are caught by requiring the supplier's own text to assert the name.
    """
    keep = set(cert_names(prev))
    low = (text or "").lower()
    for name in cert_names(new):
        if text is None or _cert_asserted(low, _CERT_TERMS[name]):
            keep.add(name)
    return [c for c in CERTS if c in keep] or None


# Fields holding a SET the supplier adds to over time rather than a single
# answer that replaces the last one. A later answer is never a contradiction
# of an earlier one here: learning that a supplier also builds 3-piece, or
# also holds VIA, is new information. Recording either as a revision put
# "changed answer between rounds" on Hantuo for answering what we had just
# asked, so these skip revision tracking by construction.
SET_FIELDS = ("piece_count", "certs_claimed")


def merge_field(k, prev, value, text=None):
    """Fold one extracted value into what is already stored.

    The one place that knows a field's merge rule. Ingest, the GUI paste
    handler and backfill all come through here, because three hand-copied
    versions of this branch is exactly how the GUI and the CLI end up
    disagreeing about what a record should say.
    """
    if k == "piece_count":
        return merge_constructions(prev, value)
    if k == "certs_claimed":
        return merge_certs(prev, value, text)
    return value


# Round two unlocks once these are known. Not the whole of round one: a
# supplier who answered everything except the size chart has clearly engaged,
# and holding the detail questions back over one soft field wastes a round.
ROUND1_CORE = ("price_per_wheel_1set", "forged_confirmed", "piece_count",
               "freight_cif_usd")

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

            # Canonicalise before the merge below unions the lists, or the
            # same certification lands twice under two spellings: the stored
            # records already hold "VIA" beside "VIA A-351" and "IATF 16949"
            # beside "IATF16949". No assertion gate here, because a listing
            # page has no supplier text to assert anything; it stays a
            # listing claim and the sheet renders it as one.
            data["certs_claimed"] = cert_names(data.get("certs_claimed"))

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
                # setdefault, never assign: re-pulling a listing must not wipe
                # an affiliate link already set for this supplier.
                rec.setdefault("affiliate_url", None)
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
                    # Ours, not theirs, so it sits at the top level rather than
                    # in from_quote and survives a reset. Null until set in the
                    # GUI; the sheet falls back to listings[0] meanwhile.
                    "affiliate_url": None,
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
def ask(wheel, size="18x9.5J", offset="ET+22", pcd="5x114.3"):
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
            # Nothing left to ask, so nothing left to send. Remove whatever
            # outbox file is still sitting there rather than leaving it: the
            # GUI's rfq endpoint globs every .txt in outbox/ after calling
            # ask(), not just the ones this run touched, so a file this old
            # loop had no reason to rewrite was displayed under "Message to
            # send" indefinitely, quoting the price question by its wording
            # from whenever it was last generated. A supplier who has
            # answered everything gets no message, not a stale one.
            (out / f"{rec['id']}.txt").unlink(missing_ok=True)
            continue

        # Only ask what the listing could not answer. Shorter asks get
        # better response rates and signal you actually read their page.
        # A field with no question text of its own is asked inside another
        # question, so a missing one has to pull in the question that carries
        # it. Without this the secondary can never be asked again once the
        # primary is answered: Ruitou gave a weight and no load rating, the
        # weight question therefore stopped being sent, and they kept a "no
        # load rating given" flag for something we had quietly stopped asking.
        want = []
        for k in missing:
            if k in EXTRAS:
                continue
            k = PAIRED_WITH.get(k, k)
            if pool.get(k) and k not in want:
                want.append(k)
        numbered = [k for k in pool if k in want]
        extras = [k for k in missing if k in EXTRAS and pool.get(k)]

        qs = "\n".join(f"  - {pool[k]}" for k in numbered)

        ex = [pool[k] for k in extras]
        ex_block = "\n".join(f"  - {e}" for e in ex) if ex else ""

        body = f"""Hello,

I'm looking at your wheels for my own car and would like a quote.

Reference spec for all pricing:
  {size} \u00b7 {offset} \u00b7 {pcd}

Could you confirm:

{qs}{f"{chr(10)}{chr(10)}An extra if you can:{chr(10)}{chr(10)}{ex_block}" if ex else ""}

Thank you.""".strip() + "\n"
        (out / f"{rec['id']}.txt").write_text(body, encoding="utf-8")
        # rfq_sent is the latest ask, which is what reply_days measures from.
        # Keep the first ask separately so re-running this does not erase when
        # contact actually started.
        rec["meta"].setdefault("rfq_first_sent", TODAY)
        rec["meta"]["rfq_sent"] = TODAY
        rec["meta"]["rfq_round"] = 2 if round2 else 1
        rec["meta"]["ref_spec"] = f"{size} {offset} {pcd} {QUOTE_CONSTRUCTION}"
        f.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"  {rec['id']}: round {2 if round2 else 1}, asking {len(missing)} questions")

    print(f"-> {out}  (paste these into Alibaba chat)")


# --------------------------------------------------------------------------
# 3. ingest
# --------------------------------------------------------------------------
REPLY_SCHEMA = """
This is a supplier's reply to a sourcing enquiry, often in broken English.
Return one JSON object. Extract ONLY what the supplier actually stated.

  price_per_wheel_1set   number or null (USD). If they quote more than one
                         construction, take the 2-PIECE figure. Report the
                         price they actually gave and say which construction
                         it is for in quote_construction; do not convert or
                         adjust between constructions.
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
                         ("the three-piece style is 550 USD"), use that. Watch
                         for the question it answers: a price following "how
                         much for two piece" is a 2-piece price even if the
                         reply itself does not repeat the word. If they give
                         one price and never say, null.
  cert_document          "provided" | "promised" | "claimed without document"
                         | "not available" | null. Use "not available" when
                         they say they cannot or do not supply one. That is a
                         disclosure, not a claim.
  certs_claimed          ARRAY of certification names the SUPPLIER attaches to
                         their own wheels. Include a name however they invoke
                         it: "our products are DOT & VIA certified" is
                         ["DOT","VIA"], and so is "we build under JWL/VIA
                         standards". [] if they name none.
                         Do NOT weigh how strong the claim is or whether they
                         can evidence it. That is cert_document's job, and the
                         sheet prints the two side by side, so "JWL, VIA" next
                         to "none available" is a complete and honest answer.
                         Dropping the name because the claim looked weak just
                         loses the half we asked for.
  size_chart             string or null. If they listed sizes or attached a
                         chart, summarise the range, e.g. "17-20in, 8-12J,
                         ET-20 to +45, chart attached". Null if not addressed.
  freight_cif_usd        number or null. The shipping cost BY ITSELF, not
                         counting the wheels. If their figure covers the
                         wheels too, it is not this field, see below.
  landed_total_usd       number or null. One all-in price for a set of 4
                         delivered: wheels plus shipping plus duties, stated
                         as a single number. "The total price is 1500 USD,
                         including wheels, shipping fees, and customs duties"
                         is 1500 here.
  manufacturing_time     string or null. How long to BUILD the wheels, in
                         their own words, e.g. "30-40 days". Also called
                         production cycle or lead time. Keep the range as a
                         range, do not average it to one number.
  shipping_time          string or null. How long to SHIP them once built,
                         e.g. "25-30 days". Also called transportation or
                         transit. This is the second half of "production
                         cycle 30-40 days, transportation 25-30 days": the
                         two are different figures and must not be merged, or
                         a 40 day build reads as a 40 day delivery.
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
- "Claimed without document" requires a claim. A supplier saying they cannot
  provide a certificate, or that one would need a test run first, has told us
  something useful and claimed nothing: that is "not available".
- certs_claimed is only what the SUPPLIER claims. The pasted thread contains
  our side of the conversation too, and our question lists DOT, JWL, VIA and
  TUV as examples. Those are our words. Never return a certification that
  only appears in a message we sent, and never one they denied ("no TUV").
- If a price is given without stating quantity, put it in
  price_per_wheel_1set and say so in notes.
- An all-in figure goes in landed_total_usd ALONE. Leave
  price_per_wheel_1set, price_per_wheel_4set and freight_cif_usd null, and do
  not divide it by 4 or subtract a guessed shipping cost to fill them. Only
  the supplier knows the split, and a number we invented for them would be
  compared against figures every other supplier actually quoted.
- The reverse also holds: a price that excludes shipping is NOT
  landed_total_usd, however it is worded. "Total for 4 wheels, shipping
  extra" is price_per_wheel_4set.
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
        rejected = take_quote(got, rec)
        if rejected:
            print(f"  {sid}: {rejected} quote set aside, not a {QUOTE_CONSTRUCTION} price")

        # Store everything the schema extracted, count only what was asked.
        filled = 0
        for k in STORED_FIELDS:
            if got.get(k) not in (None, ""):
                if k == "certs_claimed":
                    dropped = unasserted_certs(got[k], text)
                    if dropped:
                        print(f"  {sid}: reply does not assert {', '.join(dropped)}, not stored")
                rec["from_quote"][k] = merge_field(
                    k, rec["from_quote"].get(k), got[k], text)
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
        total_filled = len(answered_keys(rec["from_quote"]))

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


def backfill(wheel, field, workers=6):
    """Rebuild ONE field from the stored replies, leaving everything else alone.

    Adding a field to REPLY_SCHEMA leaves it null on every record parsed
    before it existed, and the documented cure is `ingest --force`. That
    re-reads every reply and rewrites every extracted value, and the model is
    not deterministic: heat_treat has drifted "6061-T6" to "T6", a factory
    address changed city, and a "not flow-formed" qualifier vanished from a
    forging claim. Paying that across the whole dataset to fill one column is
    a bad trade, and it is why --force carries a warning.

    So this reads the same replies with the same schema and writes only the
    field it was asked for. Everything else in the record, including
    meta.parsed_replies, is untouched: those hashes say these replies were
    read, and that stays true.

    The field is cleared first, so this rebuilds the column rather than
    unioning onto whatever a previous run left behind.
    """
    if field not in STORED_FIELDS:
        raise SystemExit(f"unknown field '{field}'.\n  one of: {', '.join(STORED_FIELDS)}")

    rep = wdir(wheel) / "replies"
    todo = []
    for f in sorted(rep.glob("*.txt")):
        sid = f.stem.split("__")[0]
        target = sdir(wheel) / f"{sid}.yaml"
        if target.exists():
            todo.append((f, sid, target, f.read_text(encoding="utf-8")))
    if not todo:
        print("  no replies to read.")
        return

    print(f"  reading {len(todo)} reply(s) for '{field}' only")
    parsed = {}
    if len(todo) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
            futs = {pool.submit(_ask_model, t[3], REPLY_SCHEMA): t[0] for t in todo}
            for fut in as_completed(futs):
                parsed[futs[fut]] = fut.result()
    else:
        for t in todo:
            parsed[t[0]] = _ask_model(t[3], REPLY_SCHEMA)

    # Clear first, so a scalar field is rebuilt rather than left holding a
    # value no reply supports any more.
    #
    # Except for the set-valued fields, which accumulate by definition and are
    # never cleared by ingest either. Rebuilding those from scratch hands a
    # single weak extraction the power to delete a claim the supplier
    # definitely made: one run dropped Raylan's certifications despite "we
    # hold TUV/JWL/VIA certifications" sitting in their reply, and with the
    # field already blanked there was nothing to fall back on. Union instead,
    # matching merge_field. To genuinely retract one of these, reset the
    # record.
    before = {}
    for target in {t[2] for t in todo}:
        rec = yaml.safe_load(target.read_text())
        before[target] = rec["from_quote"].get(field)
        if field not in SET_FIELDS:
            rec["from_quote"][field] = None
            target.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # Sequential and in sorted order, like ingest: one supplier's replies fold
    # into the same record cumulatively.
    for f, sid, target, text in todo:
        got = parsed[f]
        if got.get(field) in (None, ""):
            continue
        if field == "certs_claimed":
            dropped = unasserted_certs(got[field], text)
            if dropped:
                print(f"  {sid}: reply does not assert {', '.join(dropped)}, not stored")
        rec = yaml.safe_load(target.read_text())
        rec["from_quote"][field] = merge_field(
            field, rec["from_quote"].get(field), got[field], text)
        target.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # questions_answered is the count of ALL_FIELDS that are populated, so
    # filling one of them leaves the stored fraction wrong until it is redone.
    # Derived from the record, not from what this run happened to change.
    if field in ALL_FIELDS:
        for target in before:
            rec = yaml.safe_load(target.read_text())
            n = len(answered_keys(rec["from_quote"]))
            rec["meta"]["questions_answered"] = f"{n}/{len(ALL_FIELDS)}"
            target.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")

    print()
    for target in sorted(before):
        rec = yaml.safe_load(target.read_text())
        now, was = rec["from_quote"].get(field), before[target]
        mark = " " if now == was else "*"
        print(f"  {mark} {target.stem[:44]:<46} {was or '-'}  ->  {now or '-'}")
    print("\n  Flags are not recomputed here. Run recompute.py if this field feeds one.")


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


# Certifications get denied in a verb phrase, not adjacent: "we do not have
# TUV", "no JWL yet". _NEGATOR wants the negator flush against the keyword and
# reads straight past all of those. It is deliberately not widened, because
# the forging flag depends on its narrowness: in "not forged, this is flow
# formed" a looser reach would find "not" before "flow formed" and swallow the
# one flag principle 5 exists to raise.
#
# So the gap is allowed here only, and only as far as a clause runs. It stops
# at a sentence end and at a contrastive conjunction, or "we don't have TUV
# but we do have JWL" would deny the JWL as well.
_CERT_NEGATOR = re.compile(
    r"\b(?:not|no|never|non|isn'?t|aren'?t|doesn'?t|don'?t|cannot|can'?t|"
    r"without|lack(?:s|ing)?|waiting for|applying for)\b"
    r"(?:(?!\b(?:but|however|though|yet|although)\b)[^.;!?]){0,24}$"
)


def _asserted(text, keywords, window=30, negator=None):
    """True if a keyword appears in text without a negation just before it."""
    neg = negator or _NEGATOR
    for k in keywords:
        for m in re.finditer(re.escape(k), text):
            if not neg.search(text[max(0, m.start() - window):m.start()]):
                return True
    return False


def _cert_asserted(text, terms):
    """_asserted with the wider reach certification denials need."""
    return _asserted(text, terms, window=44, negator=_CERT_NEGATOR)


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
    # A claim is only unbacked if something was claimed. certs_claimed is the
    # discriminator the enum alone could not give: Ningbo answered "we
    # currently cannot provide certificates, you would need 20 samples first",
    # which is a straight and useful disclosure, and the sheet accused them of
    # claiming a certification they could not evidence. That is principle 5's
    # false accusation in a different column. The prompt now has "not
    # available" for exactly this, and the guard stays anyway, because a
    # mis-bucketed enum value should not be able to produce the accusation.
    if Q.get("cert_document") == "claimed without document" and Q.get("certs_claimed"):
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
    # No flag for a missing load_rating_kg: it went back to EXTRACTED_ONLY, so
    # it is no longer a question anyone can fail to answer.
    # Falsy covers the empty list merge_constructions can no longer produce,
    # but would otherwise slip past an `in (None, "")` test.
    if not Q.get("piece_count"):
        fl.append("constructions offered not confirmed")

    # They answered with a price, just not for the construction the sheet
    # compares. Worth surfacing: it is not a silence, it is an outstanding
    # question, and without this the record looks like they never quoted.
    off = {o.get("construction") for o in (meta.get("off_basis_quotes") or [])}
    if off and not p:
        fl.append(f"quoted {', '.join(sorted(x for x in off if x))}, "
                  f"no {QUOTE_CONSTRUCTION} price")
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
    # No flag for a missing size_chart: it went back to EXTRACTED_ONLY, so it
    # is no longer a question anyone can fail to answer.
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
    for name in ("pull", "ask", "ingest", "check", "mark-parsed", "backfill"):
        p = sub.add_parser(name); p.add_argument("--wheel", required=True)
        if name == "ask":
            p.add_argument("--size", default="18x9.5J"); p.add_argument("--offset", default="ET+22")
            p.add_argument("--pcd", default="5x114.3")
        if name == "ingest":
            p.add_argument("--force", action="store_true",
                           help="re-read every reply, not just changed ones. Needed after adding a field to ALL_FIELDS.")
            p.add_argument("--workers", type=int, default=6)
        if name == "backfill":
            p.add_argument("--field", required=True,
                           help="the one field to rebuild from the stored replies. Everything else is left alone.")
            p.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    if a.cmd == "pull": pull(a.wheel)
    elif a.cmd == "ask": ask(a.wheel, a.size, a.offset, a.pcd)
    elif a.cmd == "ingest": ingest(a.wheel, a.force, a.workers)
    elif a.cmd == "mark-parsed": mark_parsed(a.wheel)
    elif a.cmd == "backfill": backfill(a.wheel, a.field, a.workers)
    elif a.cmd == "check": check(a.wheel)
