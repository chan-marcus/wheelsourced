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

**5b. Which certifications, and whether the certificate arrived, are two
answers.**

`certs_claimed` is the SET of certifications a supplier names, a whitelisted
tag list that accumulates through `merge_certs()`. `cert_document` is whether
a certificate was actually sent. Same split as forged vs piece_count, for the
same reason: naming DOT and having emailed the PDF are different facts, and
merging them let a vendor bank the hard one by answering the easy one. Weiluo
wrote "our products are DOT & VIA certified" and the sheet printed only
"claimed without document", throwing away the half of the answer the question
asked for.

`cert_document` stays the numbered question, so it stays the denominator.
`certs_claimed` is `EXTRACTED_ONLY`: the RFQ already asks for both.

The whitelist is there for the CONSTRUCTIONS reason. "Fully certified" and
"export quality" are sales lines, not certifications. It also canonicalises,
which is what collapses the listing side's "VIA" beside "VIA A-351" and its
three spellings of IATF 16949. TS 16949 keeps its own tag rather than being
upgraded to IATF: a supplier naming the withdrawn standard has not told us
they hold the current one.

**A cert only counts if the supplier's own text asserts it.** Enforced in
`merge_certs()`, not asked for in the prompt, because our own RFQ question
lists DOT, JWL, VIA and TUV as examples and the pasted thread contains our
side of the conversation. A model reading the whole thread will hand those
four straight back as the supplier's answer, which is principle 3 inverted:
four free certifications for saying nothing. Verified: fed the real Weiluo
thread and a model output of all four, the gate keeps DOT and VIA and drops
JWL and TUV.

**There are two negators now, and the narrow one is load-bearing.** `_NEGATOR`
requires the negator flush against the keyword ("not flow-formed"). Do not
widen it: in "not forged, this is flow formed" a longer reach finds "not"
before "flow formed" and swallows the one flag principle 5 exists to raise.
Certifications get denied in a verb phrase instead ("we do not have TUV", "no
JWL yet"), so `_CERT_NEGATOR` allows a gap, bounded to one clause. It stops at
a sentence end and at a contrastive conjunction, or "we don't have TUV but we
do have JWL" denies the JWL too. `_asserted()` takes the negator as an
argument; both paths share the scanning loop and neither can drift.

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
      functions/    Cloudflare Pages Functions, one per public form

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

**The GUI reloads on save, but only for app.py and pipeline.py.** It serves
its entire page from a string inside app.py, so a stale process serves old
HTML forever and it reads as the edit not having worked: the Finish field sat
on the form for three hours after being deleted from the source. `uvicorn.run`
now takes `"app:app"` with `reload=True`.

The watch is scoped on purpose. Unscoped it would take in `.venv`, and every
paste writes YAML into `wheels/`, which would restart the server mid-request.
Scoping needs `watchfiles` installed: without it uvicorn ignores the include
and exclude lists and warns that it has, so treat that warning as "reinstall
watchfiles", not as noise.

Data still is not watched, and does not need to be. The GUI reads YAML per
request.

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

**The restart is serialised, and the port is pinned.** A restart starting
before the previous one finished raced it for the port: the new server found
4321 held by the old, took 4322, and the old left a socket that listened and
answered nothing. The result is a dead 4321, a live 4322 and no hint that the
sheet moved. `restarting` guards against overlap, `strictPort` means a clash
fails at startup instead of drifting somewhere else, and the server binds
127.0.0.1 explicitly because Node resolves localhost to ::1 first on macOS, so
the default bind served `localhost:4321` but not `127.0.0.1:4321`.

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

**The two public forms post to a different place in dev than in a build.**
"Request a wheel" on the index and "Suggest a supplier" at the foot of each
sheet both resolve their endpoint the same way: `PUBLIC_*_ENDPOINT` if set,
else `localhost:8420` under `astro dev`, else same-origin. Hardcoding the
localhost URL is how the request form used to fail on the live domain, and it
failed silently: the host is unreachable from a visitor's machine, and a
browser blocks http from an https page as mixed content anyway.

In production each is a Pages Function in `site/functions/api/`, appending to
Cloudflare KV. Deliberately not a backend: the sourcing dataset stays in YAML
in git, and these only ever append to a list. **Each needs its KV namespace
bound** (`WHEEL_REQUESTS`, `SUPPLIER_SUGGESTIONS`) or the endpoint returns 503,
which is on purpose. A form that silently drops submissions is worse than one
that says it is down.

Suggestions are restricted to alibaba.com and 1688.com hosts, matched on the
registrable domain rather than as a substring, or `alibaba.com.evil.tld` gets
in. That allowlist is written twice, in `_listing_host_ok` in app.py and
`listingHostOk` in the Function, because the two serve the same route in dev
and in production. Keep them in step.

Locally the GUI writes `supplier_suggestions.json`, which is **gitignored**: it
holds email addresses typed into a public form by people who are not us.
`wheel_requests.json` predates that decision and is tracked, and it already has
an address in it.

**`backfill` fills one field from the archive without churning the rest.**
Adding a field to `REPLY_SCHEMA` leaves it null on every record parsed before
it existed, and `ingest --force` is a bad way to fix that: it re-reads every
reply and rewrites every extracted value, which is the non-determinism problem
above. `backfill --wheel ccw --field certs_claimed` reads the same replies with
the same schema and writes only the named field, clearing it first so it
rebuilds the column rather than unioning onto a previous run.

It leaves `meta.parsed_replies` alone, because those hashes say these replies
were read and that stays true. It does redo `questions_answered` when the field
is in `ALL_FIELDS`, since that fraction counts populated fields and would
otherwise be stale. It does NOT recompute flags: run `recompute.py` after.

**"Claimed without document" requires a claim.** The enum had no way to say a
supplier had told us they have no certificate, so the model put that in the
same bucket as an unbacked boast. Ningbo answered "we currently cannot provide
certificates, you would need 20 samples first", which is straight and useful,
and both the flag and the public Certification cell accused them of claiming
something. Principle 5's false accusation, one column over.

`cert_document` now has `"not available"` for a decline, and the flag
additionally requires `certs_claimed` to be non-empty. Both, deliberately: the
enum value is the honest record, and the `certs_claimed` gate means a
mis-bucketed value cannot produce the accusation on its own. Weiluo still
trips it, because they did claim DOT and VIA and did not send anything.

On the sheet a decline reads "none available", not the raw stored phrase,
which would otherwise sit one word from the "not disclosed" that means the
supplier never answered.

**The Certification column is quoted-only, with no listing fallback.** The
listing side sprays: one listing claims eight certifications where the
supplier, asked directly, named two. Rendering the long unvetted list beside
the short honest one rewards the vendor whose listing overclaims, which is
principle 3, and the product here is the vetting. A supplier who has not
answered reads "not disclosed" like any other field. `from_listing.certs_claimed`
is still stored and still canonicalised, it just does not reach the sheet.

**An all-in price is a third answer, not a set price.** `landed_total_usd`
holds one number covering wheels, shipping and duties, quoted by a supplier
who will not break it down. Same split as forged vs piece_count, one column
over: "what do the wheels cost" and "what does the whole thing cost delivered"
are different questions.

The old freight question asked "Total cost to deliver to Los Angeles ...
Include shipping, duty, port fees and tariffs." To a second-language reader
that is the grand total, and Maxtera answered it that way three times: "the
total price is 1500 USD, including wheels, shipping fees, and customs duties."
With nowhere else to put it, the parser filed a landed figure under
`price_per_wheel_4set`, where it sat against everyone else's ex-freight
numbers, while `freight_cif_usd` stayed null so `landed` came out null too.
The supplier who gave the most complete price answer on the sheet was the only
one with no landed cost and no place in the "cheapest" sort. That is principle
3 read in the mirror, the same shape as the piece_count overwrite that cost
Hantuo their ranking.

So `landed` prefers the quoted total over the computed one, and the parts stay
null: only the supplier knows the split, and inventing it would put a number
we made up beside figures everyone else actually quoted. The sheet prints
"quoted all-in" next to it, because "not disclosed" in the other three price
cells means something different when one number already covers them.

`answered_keys()` counts an all-in total as the set-of-4 price, or moving the
figure out of that field would have docked the supplier a point for answering
more fully. It deliberately does not count as the shipping question: that
answer is inside the total and cannot be recovered, so `ask()` keeps asking
for the split. The question now asks for shipping "by itself", but suppliers
will still answer all-in, which is why the field exists rather than a better
prompt: same reasoning as `off_basis_quotes`.

`landed_total_usd` is in `PRICE_FIELDS`, so `take_quote()` discards it for the
wrong construction like any other price. Left out, it would be the one way
around that guard.

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

**The RFQ is written for a second-language reader.** Sales staff answer it one
number at a time, so each question is a single line of short sentences, one
thing per sentence, plain word over precise word ("factory" not
"manufacturer", "how is it made" not "what process"). A question that has to
be re-read is a question that gets skipped, and a skipped question is a null
on the sheet. Three sentences that must survive any rewording: the price
question naming `QUOTE_CONSTRUCTION` twice, "list all, not only this wheel" on
`piece_count`, and "can you send the certificate?" on `cert_document`. Each is
load-bearing for a guard downstream, and the reasons are on the fields.

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
six dimensions. A vendor missing a dimension is scored on the rest rather
than pushed to last place. Flags are computed and stored but do not affect
the vetting score.

**Price is not one of the six, on purpose.** It was originally the one
min-maxed, relative dimension, on the reasoning that these are like-for-like
quotes at one reference spec, so the cheapest genuinely is the best price on
offer. That reasoning is still true, and is exactly why it does not belong in
this particular number: "best price" and "most trustworthy" are different
questions, and scoring them together meant a vendor's *trustworthiness* score
moved when a competitor happened to quote low, which has nothing to do with
whether that vendor is trustworthy. Price still drives the "est. landed"
figure shown on every card, computed exactly as before, and the "cheapest"
sort option; it simply does not feed the 0-100 number. The min-max / relative
branch stays in `scoreAll()` as generic machinery (`lowerBetter`, in the code),
since a genuinely comparative dimension could exist again someday, but nothing
currently uses it, so every live dimension is scored against a fixed anchor.

**The score is out of 100 against fixed anchors.** Everything is scored
against a constant rather than against whoever happens to be in the set,
because a min-maxed dimension has two failure modes and this project has hit
both. Min-max scored the worst vendor at zero and everyone else on a curve, so
a one-year-old shop only looked bad because a peer happened to be five. When
every vendor ties on a dimension min-max hands them all full marks, which
reads as everyone excelling at something none of them has.

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

One thing still moves when the set changes, deliberately: the rating prior,
which is the set mean. A fixed prior of 4.7 costs about a point per vendor if
that drift ever becomes a problem.

**Ratings are credibility-weighted before scoring.** A rating is shrunk toward
the set mean in proportion to how few reviews back it (`RATING_PRIOR_WEIGHT`,
25). 5.0 from two buyers is not a better rating than 4.8 from eighty-seven, it
is a less certain one. Scoring rating and review count as independent
dimensions let a vendor bank full marks for a figure its own sample could not
support: the "silence must not pay" rule in a different costume.

**Only a fully quoted landed cost is ever shown as one.** This is no longer a
scoring rule, since price is not scored, but it still governs the "est.
landed" figure and the "cheapest" sort, and the reasoning is the same one
that used to protect the score: `landed ?? setPrice` would have compared one
vendor's pre-freight number against everyone else's post-freight one, so
withholding freight made a vendor look cheaper than the ones who disclosed
it. Same bug as defaulting freight to zero, just relocated from the score to
the display. `landed` stays null in `normalize()` unless both the set price
and freight were quoted, full stop.

Scaled by coverage: the weighted average is multiplied by
`0.55 + 0.45 * coverage`, where coverage is the share of the six dimensions
the vendor actually has. Before this, a vendor with two populated dimensions
could outrank one with five on a lucky average, which is the same failure
mode as defaulting a missing freight cost to zero. The floor of 0.55 means a
thin record is discounted, not erased.

Weights: reorder 2.0, rating 1.5, store age 1.5, review count 1.3, response
0.3, transit 0.3. Adding a dimension changes every score, so check the whole
table afterwards rather than the one vendor you had in mind: adding transit at
0.3 raised a thin vendor's score because it happened to be fastest, which is
the opposite of what was intended.

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
