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

**4b. Capability and quote are different questions.**
`piece_count` is the SET of constructions a supplier can build, rendered as
tags. It accumulates through `merge_constructions()` and is never overwritten
by whichever one they last quoted. Every price, meanwhile, is for
`QUOTE_CONSTRUCTION` (2-piece) so the figures compare, and
`quote_construction` records what a stored price is actually for.

A price for any other construction is DISCARDED by `take_quote()`, never
converted and never stored. It goes to `meta.off_basis_quotes` so the answer
survives, and the supplier is flagged as still owing a comparable price.
Enforced in code, not in the prompt: telling the model to withhold a number
it can plainly see is the least reliable instruction available, and one
3-piece figure landing in the 2-piece column defeats the entire split.

`QUOTE_CONSTRUCTION` is defined above `QUOTE_ONLY` because the question text
is built from it with an f-string. Written out separately they drifted, and
the failure is silent and total: the constant said 2-piece while the RFQ
asked for a 3-PIECE price, so every reply answered a question whose answer
the guard then discarded, and `ask()` re-sent it forever.

Conflating the two cost a supplier their ranking. Hantuo quoted 2-piece, was
asked for 3-piece, and the second quote overwrote the first: their
`piece_count` collapsed from a capability to a single value, the price rise
logged as a revision, they were flagged "changed answer between rounds", and
they fell from first to second. They had done nothing but answer the
question. A vendor must never be penalised for disclosing more, which is
principle 3 read in the mirror.

Construction is NOT a scoring input and must not become one.

**5. Flow forming is not forging.**
"Rotary forged" and "flow formed" mean a CAST centre with a spun barrel.
The reply parser is explicitly told not to normalise those to "forged".
Conversely, "machined from a forged blank" IS normal forging and must not
be flagged. We got this wrong once and it produced a false accusation
against an honest supplier.

Then we got it wrong a second way. The flag was a bare substring test, so
"fully forged from 6061-T6 billets, **not** flow-formed" matched on
`flow-form` and accused Taizhou Hantuo of the one process it had explicitly
ruled out. Match through `_asserted()`, which ignores a keyword preceded by a
negation. Any new claim-detection flag must go through it too: suppliers
answer this question in the negative constantly, because we ask them to.

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

**Astro reads ../wheels at build time.** The GUI reads YAML per request; the
site reads it once and bakes it into `getStaticPaths`. Vite only watches its own
root, so for a long time nothing written to `wheels/` reached a running dev
server and the sheet quietly served whatever it had at startup. That does not
present as a stale page, it presents as the write having failed: reset a
supplier, watch the GUI clear, then see the old figures still on the sheet.

The `watch-wheels` plugin in `astro.config.mjs` now watches `../wheels` and
restarts the server on change, debounced so a pull does not restart a dozen
times. It restarts rather than invalidating the module because `getStaticPaths`
caches per route and has already handed the supplier data to the props, so
dropping `data.js` out of the module graph would not reach it.

Nothing watches during `astro build`, which is correct: a build reads once.

**Changing the question set needs a migration.** `ask()` iterates the field
definitions (not the record) and calls setdefault, so new keys backfill
automatically and stale keys are ignored. But old keys linger in the YAML
until cleaned, and backfilling *values* for a newly added field means
`ingest --wheel <w> --force` over the stored replies in `replies/`. Plain
`ingest` will not do it: it now skips replies it has already read.

**Asked and stored are different sets, and the copy loops use the stored
one.** `ALL_FIELDS` is what the RFQ asks and is the denominator for
`questions_answered`. `STORED_FIELDS` adds `EXTRACTED_ONLY` (weight_kg,
load_rating_kg, forged_confirmed, size_chart): things `REPLY_SCHEMA` pulls
out but that were never numbered questions.

Trimming the question set once removed those four from the asked set while
leaving them in `ROUND1_CORE` and `_flags()`. The copy loops iterated
`ALL_FIELDS`, so the model extracted the values and the code silently threw
them away, while the flag logic marked suppliers down for not providing them.
Gushi replied "Weight is about 11 kg, load capacity is 850 kg" and both
fields stayed null with a "no load rating given" flag attached. Older records
only held values because those fields were asked before the trim.

If you add a field to `REPLY_SCHEMA`, add it to `QUOTE_ONLY`/`ROUND2` if it
should be asked, or to `EXTRACTED_ONLY` if it should only be kept. A field in
the schema and in neither is a field you parse and discard.

**`PHOTO_ASK` is dead.** It is defined and never referenced, so nothing asks
for the scale photo. Weight arrives only when a supplier volunteers it.

**Ingest is incremental, and a re-parse is not free.** Each reply's content
hash is stored in `meta.parsed_replies`; unchanged files are skipped and the
rest are parsed concurrently (`--workers`, default 6). It used to re-read the
whole archive every run, so one pasted reply cost 23 model calls and about a
minute, growing with the archive.

The cost that mattered was not the minute. The model is not deterministic, so
re-reading a reply rewrites its extracted values with slightly different
wording every time: `heat_treat` drifted `6061-T6` -> `T6`, a factory address
changed city, and a `not flow-formed` qualifier vanished from a forging
claim. Every ingest run was quietly churning the whole dataset. Skipping
unchanged replies is what stops that, and it is the real reason for the
cache.

So do not reach for `--force` casually. It re-reads everything and will move
values that nobody asked it to move. `mark-parsed` is the safe way to adopt
the cache on an existing archive: it records the hashes without calling the
model, meaning "what is stored is what these replies said".

Writes stay sequential on purpose. One supplier can have a dozen replies
folded into the same YAML cumulatively, so only the model calls fan out; the
results are applied in sorted order exactly as before.

**`FORGED_K = 0.055`** in pipeline.py is an estimate, not a measurement. It
drives the "weight high for forged" flag. Calibrate it against real quotes.
If honest suppliers keep tripping it, K is too low.

**Two-round questions.** `ROUND2` (cnc_inhouse, heat_treat, payment_terms,
defect_policy) is held back from first contact: sending all fourteen cold
tanks the response rate. It unlocks once the supplier has engaged, meaning
`meta.reply_received` is set or every `ROUND1_CORE` field is answered.

The gate used to require all of `ROUND1_CORE` and nothing else. That was too
strict in practice: `piece_count` is the field suppliers dodge most, and one
dodged answer pinned a supplier in round one permanently. Raylan had replied
four times and still could not be asked about payment terms. The gate is
there to keep the first message short, not to withhold questions from a
supplier who is already talking to you.

**Flags are derived, never appended by the caller.** `"changed answer between
rounds"` used to be tacked on by app.py after `_flags()` returned, so every
`recompute.py` run wiped it. It now derives from `meta.revisions`. Any new
flag must be computable from the record alone or it will not survive.

**Only numbers and booleans count as a changed answer.** `REVISION_FIELDS`
gates what goes into `meta.revisions`. The prose fields are the model's
summary of a reply, and re-reading the same text rewords it: Gushi's
`defect_policy` shifted from "compensation provided if damage occurs" to
"compensation provided for damage" and earned a "changed answer between
rounds" flag for a sentence the supplier never revised. A reworded summary is
not a contradiction.

---

## Scoring

`scoreAll()` in `site/src/lib/data.js` computes the "best vetted" number over
seven dimensions. A vendor missing a dimension is scored on the rest rather
than pushed to last place. Flags are computed and stored but do not affect
the vetting score.

**The score is out of 100 against fixed anchors, and only price is relative.**
Landed cost is min-maxed because it genuinely is comparative: these are
like-for-like quotes at one reference spec, so the cheapest really is the best
price on offer. Everything else is scored against a constant.

Min-max scored the worst vendor in the set at zero and everyone else on a
curve, so a one-year-old shop only looked bad because a peer happened to be
five. When every vendor ties on a dimension min-max gives them all full marks,
which reads as everyone excelling at something none of them has.

Reorder rate was the worst case: min-maxed while carrying the heaviest weight
of all, a quarter of the score. Exactly one supplier could ever score full
marks on it and the rest were dragged toward zero however good their actual
rate was. That alone flattened the whole field into 37-58.

The anchors (TRUSTED_YEARS, TRUSTED_REVIEWS, RATING_FLOOR/CEIL,
STRONG_REORDER_PCT, the response and transit bounds) are set to excellence a
real supplier can reach, not to perfection. Pitched at the theoretical best
case they made the best available supplier read like a bare pass. Never set
them from the current set's max: that is min-max in a different hat.

Expect real scores in the 40s to 60s. 100 means excellent on every axis at
once and nothing real clears it.

Two things still move when the set changes, both deliberate: the landed
min-max, and the rating prior, which is the set mean. A fixed prior of 4.7
costs about a point per vendor if that drift ever becomes a problem.

**Ratings are credibility-weighted before scoring.** A rating is shrunk toward
the set mean in proportion to how few reviews back it (`RATING_PRIOR_WEIGHT`,
25). 5.0 from two buyers is not a better rating than 4.8 from eighty-seven, it
is a less certain one. Scoring rating and review count as independent
dimensions let a vendor bank full marks for a figure its own sample could not
support: the "silence must not pay" rule in a different costume.

**Only a fully quoted landed cost is scored.** The price dimension used
`landed ?? setPrice`, which compared one vendor's pre-freight number against
everyone else's post-freight one, so withholding freight made you look cheaper
than the vendors who disclosed it. Same bug as defaulting freight to zero.

Scaled by coverage: the weighted average is multiplied by
`0.55 + 0.45 * coverage`, where coverage is the share of the seven dimensions
the vendor actually has. Before this, a vendor with two populated dimensions
could outrank one with five on a lucky average, which is the same failure
mode as defaulting a missing freight cost to zero. The floor of 0.55 means a
thin record is discounted, not erased.

Weights: reorder 2.0, rating 1.5, store age 1.5, review count 1.3, landed
cost 1.0, response 0.3, transit 0.3. Adding a dimension changes every score,
so check the whole table afterwards rather than the one vendor you had in
mind: adding transit at 0.3 raised a thin vendor's score because it happened
to be fastest, which is the opposite of what was intended.

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
