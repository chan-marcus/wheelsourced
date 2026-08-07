// Reads the same YAML files pipeline.py writes. No database, no API.
// Runs at build time only, so the browser gets a plain JSON blob.

import fs from "node:fs";
import path from "node:path";
import yaml from "js-yaml";

const WHEELS_DIR = path.resolve("../wheels");
const PUBLIC_DIR = path.resolve("public");

const IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"];

// The uploaded image lives in wheels/<slug>/ next to the YAML, so it is one
// more thing git versions alongside the record. Astro will not serve anything
// outside site/public, though, so the <img> 404s unless we mirror it in.
// Copy at load time rather than making the GUI write to two places: wheels/
// stays the single source of truth and public/wheels is derived output.
function publishImage(slug) {
  const src = IMAGE_EXTS
    .map((ext) => path.join(WHEELS_DIR, slug, `image${ext}`))
    .find((p) => fs.existsSync(p));
  if (!src) return { hasImage: false, imageExt: "" };

  const ext = path.extname(src);
  const destDir = path.join(PUBLIC_DIR, "wheels", slug);
  const dest = path.join(destDir, `image${ext}`);

  // A wheel has exactly one image. Clear stale copies first or an older
  // upload in a different format keeps winning: the extension search is
  // ordered, so a leftover image.png shadows a freshly uploaded image.jpg.
  fs.mkdirSync(destDir, { recursive: true });
  for (const f of fs.readdirSync(destDir)) {
    if (f.startsWith("image") && f !== `image${ext}`) fs.rmSync(path.join(destDir, f));
  }

  const from = fs.statSync(src);
  const to = fs.existsSync(dest) ? fs.statSync(dest) : null;
  if (!to || to.size !== from.size || to.mtimeMs < from.mtimeMs) fs.copyFileSync(src, dest);

  return { hasImage: true, imageExt: ext };
}

const num = (v) => (typeof v === "number" && !Number.isNaN(v) ? v : null);
const hours = (v) => { const m = String(v ?? "").match(/(\d+(?:\.\d+)?)\s*h/i); return m ? +m[1] : null; };
const pct = (v) => { const m = String(v ?? "").match(/(\d+(?:\.\d+)?)\s*%/); return m ? +m[1] : null; };
const transitDays = (v) => { const m = String(v ?? "").match(/(\d+(?:\.\d+)?)\s*d/i); return m ? +m[1] : null; };

export function loadWheels() {
  if (!fs.existsSync(WHEELS_DIR)) return [];

  return fs
    .readdirSync(WHEELS_DIR)
    .filter((d) => fs.statSync(path.join(WHEELS_DIR, d)).isDirectory())
    .map((slug) => {
      const dir = path.join(WHEELS_DIR, slug, "suppliers");
      if (!fs.existsSync(dir)) return null;

      const suppliers = fs
        .readdirSync(dir)
        .filter((f) => f.endsWith(".yaml"))
        .map((f) => normalize(yaml.load(fs.readFileSync(path.join(dir, f), "utf8"))))
        .filter(Boolean)
        // Cheapest-first is wrong on its own: the vendor lying about forging
        // is always the cheapest, so a naive price sort puts the fake at the
        // top of the page. Rank in tiers — clean and quoted, then flagged,
        // then never quoted — and sort by landed cost inside each tier.
        .sort((a, b) => {
          const tier = (s) =>
            s.landed && !s.flags.length ? 0 :   // fully quoted, nothing wrong
            s.landed ? 1 :                      // fully quoted, flagged
            s.setPrice ? 2 :                    // priced but freight unknown
            3;                                  // never quoted
          const t = tier(a) - tier(b);
          if (t) return t;
          const av = a.landed ?? a.setPrice, bv = b.landed ?? b.setPrice;
          if (av && bv) return av - bv;
          return b.answered - a.answered;
        });

      return {
        slug,
        title: titleize(slug),
        suppliers: scoreAll(suppliers),
        ...publishImage(slug),
      };
    })
    .filter((w) => w && w.suppliers.length);
}

function normalize(r) {
  if (!r || !r.id) return null;
  const L = r.from_listing || {};
  const Q = r.from_quote || {};

  const price = num(Q.price_per_wheel_1set);
  const price4quoted = num(Q.price_per_wheel_4set);
  const freight = num(Q.freight_cif_usd);
  // Set of 4 price, preferring vendor's actual quote over calculated
  const setPrice = price4quoted ?? (price ? price * 4 : null);
  // The number that actually matters. Null unless BOTH parts are quoted:
  // a landed cost built on a listing price is a made-up number.
  // Only a real landed cost when BOTH parts were quoted. Adding zero for an
  // undisclosed freight cost makes the least forthcoming vendor look like the
  // cheapest, which is exactly backwards.
  const landed = setPrice && freight !== null ? setPrice + freight : null;

  const fields = Object.values(Q);
  const answered = fields.filter((v) => v !== null && v !== undefined && v !== "").length;

  return {
    id: r.id,
    name: titleize(r.id),
    url: (r.listings && r.listings[0]) || r.listing_url || null,
    bolt_patterns: (L.bolt_patterns || []).map(String),
    sizes: (L.sizes_offered || []).map(String),
    construction: L.construction || null,
    business_type: L.business_type || null,
    years: num(L.years_on_platform),
    verified: !!L.verified_supplier,
    // Platform performance metrics. These are Alibaba's own numbers, not the
    // vendor's claims, which makes them a different class of evidence from
    // anything in from_quote.
    store_rating: num(L.store_rating),
    review_count: num(L.review_count),
    response_time: L.response_time || null,
    on_time: L.on_time_dispatch || null,
    reorder: L.reorder_rate || null,
    transit: L.transit_time || null,
    moq: L.moq || null,
    // Units sold is the only figure on a listing that reflects actual
    // transactions rather than a claim or a self-reported stat.
    sold: num(L.units_sold),
    // Sortable forms. "<=2h" -> 2, "18%" -> 18. Lower is better for response,
    // higher for everything else.
    respHours: hours(L.response_time),
    reorderPct: pct(L.reorder_rate),
    onTimePct: pct(L.on_time_dispatch),
    listed_price: num(L.listed_price_low),
    price,
    price4: num(Q.price_per_wheel_4set),
    freight,
    freight_estimated: price && freight === null,
    landed,
    setPrice,
    weight: num(Q.weight_kg),
    load_rating: num(Q.load_rating_kg),
    forged: Q.forged_confirmed ?? null,
    // The verbatim claim behind `forged`. Shown alongside it, never merged
    // into it: "rotary forged" reads as a confirmation until you print it.
    forging_process: Q.forging_process || null,
    pieces: Q.piece_count || null,
    cnc: Q.cnc_inhouse ?? null,
    heat_treat: Q.heat_treat || null,
    payment: Q.payment_terms || null,
    defects: Q.defect_policy || null,
    // One question, two answers, kept apart: which certifications they name,
    // and whether the certificate ever arrived. Merging them let a supplier
    // bank the harder one by answering the easier one.
    //
    // Quoted only, with no fallback to L.certs_claimed. The listing side is
    // marketing boilerplate and it sprays: one listing claims eight
    // certifications where the supplier, asked directly, named two. Printing
    // the long unvetted list next to the short honest one rewards the vendor
    // whose listing overclaims, and the product here is the vetting. A
    // supplier who has not answered gets "not disclosed", same as any other
    // field.
    certs: Q.certs_claimed?.length ? Q.certs_claimed : null,
    cert: Q.cert_document || null,
    size_chart: Q.size_chart || null,
    is_manufacturer: Q.is_manufacturer ?? null,
    flags: r.flags || [],
    answered,
    total: Object.keys(Q).length,
    verified_on: (r.meta && (r.meta.reply_received || r.meta.listing_seen)) || null,
    ref_spec: (r.meta && r.meta.ref_spec) || null,
    replies: (r.meta && r.meta.reply_count) || 0,
  };
}

function titleize(s) {
  return String(s).replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function loadVehicles() {
  const f = path.resolve("src/data/vehicles.yaml");
  if (!fs.existsSync(f)) return [];
  return yaml.load(fs.readFileSync(f, "utf8")) || [];
}


// How many reviews a rating needs before it mostly speaks for itself.
const RATING_PRIOR_WEIGHT = 25;

// The absolute anchors. 100 means "excellent on every axis", and these are set
// to excellence a real Alibaba supplier can actually reach, not perfection.
// They were previously pitched at the theoretical best case (a 5.0 credible
// rating, six years, best-in-set on every price axis), which nothing real
// clears, so the whole field compressed into 37-58 and the best supplier
// available read like a bare pass.
//
// Do NOT set these from the current set's max. That is min-max wearing a
// different hat: it pins the worst vendor at zero and the best at full marks
// however good either actually is, and every score then moves whenever a
// supplier is added. These are fixed judgements about the trade.
const TRUSTED_YEARS = 5;
const TRUSTED_REVIEWS = 120;
// Ratings below this are indistinguishable on Alibaba, where almost nobody
// sits under 4.3. Measuring from 4.0 spent a third of the scale on a band no
// supplier occupies and made a shaky 4.4 look close to a solid 4.8.
const RATING_FLOOR = 4.3;
const RATING_CEIL = 4.9;
// A reorder rate this high is a strong signal on its own terms.
const STRONG_REORDER_PCT = 35;
// Response and transit, in hours and days: the point where each stops
// counting against a supplier, and the point where it stops earning credit.
const SLOW_RESPONSE_H = 5;
const FAST_RESPONSE_H = 1;
const SLOW_TRANSIT_D = 32;
const FAST_TRANSIT_D = 15;

const clamp01 = (n) => Math.max(0, Math.min(1, n));

// Composite "best vetted", scored out of 100 against fixed anchors.
//
// Only landed cost is relative now. It is the one figure that genuinely only
// means something against the peers: these are like-for-like quotes for the
// same wheel at the same reference spec, so the cheapest really is the best
// price on offer.
//
// Everything else is absolute, measured against the anchors above rather than
// against whoever happens to be in the set. Min-max hid that: it scores the
// worst vendor at zero and everyone else on a curve, so a one-year-old shop
// only looked bad because a peer happened to be five. Worse, when every
// vendor ties, min-max hands them all full marks, so a set where nobody has a
// track record would read as though everybody did.
//
// Reorder rate in particular was min-maxed while carrying the heaviest weight
// of all, a quarter of the score. That forced the field flat: exactly one
// supplier could ever score full marks on it and everyone else was dragged
// toward zero however good their actual reorder rate was. A 20% reorder rate
// is a real, respectable number and now scores like one.
//
// The rating is credibility-weighted before it is scored. 5.0 from two
// reviews is not a better rating than 4.8 from eighty-seven, it is a less
// certain one. Scoring rating and review count as independent dimensions let
// a vendor bank full marks for a figure its own sample cannot support, which
// is the "silence must not pay" rule in a different costume.
function scoreAll(list) {
  const rated = list.filter((s) => typeof s.store_rating === "number");
  const prior = rated.length
    ? rated.reduce((t, s) => t + s.store_rating, 0) / rated.length
    : null;

  // Shrink each rating toward the set mean in proportion to how few reviews
  // stand behind it. Two reviews barely move off the prior, three hundred
  // nearly replace it.
  const credibleRating = (s) => {
    if (prior === null || typeof s.store_rating !== "number") return null;
    const n = s.review_count ?? 0;
    return (n * s.store_rating + RATING_PRIOR_WEIGHT * prior) / (n + RATING_PRIOR_WEIGHT);
  };

  const dims = [
    // Landed only. `landed ?? setPrice` compared a vendor's pre-freight
    // number against everyone else's post-freight one, so withholding freight
    // made you look cheaper than the vendors who disclosed it. That is the
    // same bug as defaulting a missing freight cost to zero, wearing a hat.
    { k: (s) => s.landed,               lowerBetter: true,  w: 1.0 },
    // Minor factor: many fast responders still disappoint.
    { k: (s) => s.respHours, w: 0.3,
      abs: (h) => clamp01((SLOW_RESPONSE_H - h) / (SLOW_RESPONSE_H - FAST_RESPONSE_H)) },
    // Most telling: the share of buyers who came back.
    { k: (s) => s.reorderPct, w: 2.0, abs: (v) => clamp01(v / STRONG_REORDER_PCT) },
    { k: (s) => transitDays(s.transit), w: 0.3,
      abs: (d) => clamp01((SLOW_TRANSIT_D - d) / (SLOW_TRANSIT_D - FAST_TRANSIT_D)) },
    { k: credibleRating, w: 1.5,
      abs: (v) => clamp01((v - RATING_FLOOR) / (RATING_CEIL - RATING_FLOOR)) },
    { k: (s) => s.years,     w: 1.5, abs: (v) => clamp01(v / TRUSTED_YEARS) },
    { k: (s) => s.review_count, w: 1.3, abs: (v) => clamp01(v / TRUSTED_REVIEWS) },
  ];

  const ranges = dims.map((d) => {
    if (d.abs) return null;
    const vals = list.map(d.k).filter((v) => typeof v === "number");
    return vals.length ? [Math.min(...vals), Math.max(...vals)] : null;
  });

  list.forEach((s) => {
    let sum = 0, wt = 0;
    dims.forEach((d, i) => {
      const v = d.k(s);
      if (typeof v !== "number") return;
      let n;
      if (d.abs) {
        n = d.abs(v);
      } else {
        const r = ranges[i];
        if (!r) return;
        const [lo, hi] = r;
        // No spread means the dimension separates nobody. Award the midpoint
        // rather than full marks: an uninformative dimension should not read
        // as everyone excelling at it.
        n = hi === lo ? 0.5 : (v - lo) / (hi - lo);
        if (d.lowerBetter) n = 1 - n;
      }
      sum += n * d.w;
      wt += d.w;
    });
    // Scale by coverage. Averaging over "whatever data exists" let a vendor
    // with two populated dimensions beat one with five, because the missing
    // three were skipped rather than counted against them. That is the same
    // failure as defaulting a missing freight cost to zero: silence came out
    // ahead. Weight the average by the share of dimensions actually present,
    // floored so a thin record is discounted, not erased.
    const coverage = wt / dims.reduce((t, d) => t + d.w, 0);
    const base = wt ? (sum / wt) * (0.55 + 0.45 * coverage) : 0;
    s.coverage = Math.round(coverage * 100);
    s.vetScore = Math.round(Math.max(0, base * 100));
  });

  return list.sort((a, b) => b.vetScore - a.vetScore);
}
