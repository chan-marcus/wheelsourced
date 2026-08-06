# WheelSourced

A curated sourcing site for aftermarket wheels made by Chinese factories.
Not a link aggregator. The product is the vetting.

---

## Non-negotiable principles

These were decided deliberately. Do not "improve" them without asking.

**1. Provenance is everything.**
Every field is tagged by origin. `from_listing` is what the platform claims.
`from_quote` is what a human told us. The site renders listing figures grey
and quoted figures black. Never merge the two, never let a listing value
stand in for a quoted one.

**2. Publish the nulls.**
When a supplier ignores a question twice, the sheet says "not disclosed".
That is a feature, not a gap. Never invent, estimate, or infer a missing
value to make a row look complete.

**3. A missing value must never advantage a vendor.**
This has caused two real bugs already:
- `landed = price*4 + (freight ?? 0)` made the vendor who withheld freight
  look cheapest. Landed cost is now null unless BOTH parts were quoted.
- A naive price sort put the vendor lying about forging at the top, since
  the liar is always cheapest.
Any time you add a default for a missing field, check whether it rewards
non-disclosure. It usually does.

**4. Forged and piece count are independent axes.**
`forged_confirmed` (bool) and `piece_count` (monoblock / 2-piece / 3-piece)
are separate questions. A cast 3-piece is a real, honest product. Never let
one field fall back to the other.

**5. Flow forming is not forging.**
"Rotary forged" and "flow formed" mean a CAST centre with a spun barrel.
The reply parser is explicitly told not to normalise those to "forged".
Conversely, "machined from a forged blank" IS normal forging and must not
be flagged. We got this wrong once and it produced a false accusation
against an honest supplier.

**6. Name wheels by geometry, never by another maker's product.**
"Mesh 5-spoke, 3-piece" not "OZ Futura replica". This keeps the site out of
the takedown pipeline and is more accurate anyway.

---

## Layout

    pipeline.py     scrape listings -> YAML, generate RFQs, parse replies
    app.py          local GUI on :8420, reads/writes the same YAML
    recompute.py    re-run flag logic over stored records
    wheels/<slug>/
      picks.txt     hand-picked Alibaba listing URLs
      suppliers/    one YAML per supplier, THIS IS THE DATABASE
      outbox/       generated RFQ messages
      replies/      raw pasted vendor replies, never edited
    site/           Astro static site, reads ../wheels at BUILD time

---

## Workflow

Discovery is manual by design. Marcus photo-searches on alibaba.com and
s.1688.com, hand-picks listings, and pastes URLs into the GUI. Style
matching is a human judgement; do not try to automate it.

1. `pull`   listing + supplier profile -> YAML (via a vision model, not
            CSS selectors: Alibaba markup churns constantly)
2. `ask`    generates an RFQ asking ONLY what the listing failed to answer
3. paste replies into the GUI -> parsed, flagged
4. `site`   builds the public sheet from the YAML

---

## Gotchas that have already cost hours

**Flags are stored, not derived at render.** After ANY change to `_flags()`
in pipeline.py, run `python recompute.py` or the old flags persist in the
YAML and show on the site.

**Astro reads ../wheels at build time.** `npm run dev` does not watch files
outside its own directory, so after a pull you must restart the dev server.
The GUI reads YAML live; the site does not. They will disagree until you
restart Astro.

**Changing the question set needs a migration.** `ask()` iterates
`ALL_FIELDS` (not the record) and calls setdefault, so new keys backfill
automatically and stale keys are ignored. But old keys linger in the YAML
until cleaned, and backfilling *values* for a newly added field means
re-running `ingest` over the stored replies in `replies/`.

**`FORGED_K = 0.055`** in pipeline.py is an estimate, not a measurement. It
drives the "weight high for forged" flag. Calibrate it against real quotes.
If honest suppliers keep tripping it, K is too low.

**Two-round questions.** `ROUND2` (cnc_inhouse, heat_treat, payment_terms,
defect_policy) only sends once `ROUND1_CORE` is answered: price, weight, load
rating, forged, piece count, freight. Not all of round one, because a
supplier who withheld only the size chart has still engaged. Sending all
fourteen at first contact tanks the response rate.

**Flags are derived, never appended by the caller.** `"changed answer between
rounds"` used to be tacked on by app.py after `_flags()` returned, so every
`recompute.py` run wiped it. It now derives from `meta.revisions`. Any new
flag must be computable from the record alone or it will not survive.

---

## Scoring

`scoreAll()` in `site/src/lib/data.js` computes the "best vetted" number:
min-max normalises price, store rating, store age, response time and reorder
rate across the set, then subtracts 6 per flag. A vendor missing a dimension
is scored on the rest rather than pushed to last place.

Scaled by coverage: the weighted average is multiplied by
`0.55 + 0.45 * coverage`, where coverage is the share of the five dimensions
the vendor actually has. Before this, a vendor with two populated dimensions
could outrank one with five on a lucky average, which is the same failure
mode as defaulting a missing freight cost to zero. The floor of 0.55 means a
thin record is discounted, not erased.

---

## Things NOT to build

- Anything that automates sending messages on Alibaba. The account matters
  more than the automation, and that is how accounts get banned.
- A backend or database. The dataset is ~300 rows changing quarterly. YAML
  in git gives us price history for free.
- A page per wheel × size × bolt-pattern combination. That is the doorway
  page pattern and Google penalises it.
- Fitment advice ("this fits your E36 lowered"). Vehicle data is limited to
  permanent manufacturer facts. The judgement layer is a liability boundary,
  not a scope decision.

---

## Style

- No em dashes anywhere in generated copy or UI text.
- Design language is a technical sourcing document, not a car marketing
  site: spec-sheet typography, revision dates, visible verification stamps.
  Barlow Condensed for headings, IBM Plex Mono for data.
- The wordmark uses Neue Haas Grotesk Display Pro (licensed, self-hosted
  from site/public/fonts/, falls back to Helvetica Neue).
