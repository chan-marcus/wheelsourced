// Reads the same YAML files pipeline.py writes. No database, no API.
// Runs at build time only, so the browser gets a plain JSON blob.

import fs from "node:fs";
import path from "node:path";
import yaml from "js-yaml";

const WHEELS_DIR = path.resolve("../wheels");

const num = (v) => (typeof v === "number" && !Number.isNaN(v) ? v : null);
const hours = (v) => { const m = String(v ?? "").match(/(\d+(?:\.\d+)?)\s*h/i); return m ? +m[1] : null; };
const pct = (v) => { const m = String(v ?? "").match(/(\d+(?:\.\d+)?)\s*%/); return m ? +m[1] : null; };

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

      return { slug, title: titleize(slug), suppliers: scoreAll(suppliers) };
    })
    .filter((w) => w && w.suppliers.length);
}

function normalize(r) {
  if (!r || !r.id) return null;
  const L = r.from_listing || {};
  const Q = r.from_quote || {};

  const price = num(Q.price_per_wheel_1set);
  const freight = num(Q.freight_cif_usd);
  // The number that actually matters. Null unless BOTH parts are quoted:
  // a landed cost built on a listing price is a made-up number.
  // Only a real landed cost when BOTH parts were quoted. Adding zero for an
  // undisclosed freight cost makes the least forthcoming vendor look like the
  // cheapest, which is exactly backwards.
  const landed = price && freight !== null ? price * 4 + freight : null;
  const setPrice = price ? price * 4 : null;

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


// Composite "best vetted": cheap, well rated, long established, fast to reply,
// high reorder. Each dimension is min-max normalised across the set so no
// single unit dominates, then averaged over whatever data actually exists.
// A vendor missing a dimension is scored on the rest rather than penalised
// into last place, but flags subtract directly.
function scoreAll(list) {
  const dims = [
    { k: (s) => s.landed ?? s.setPrice, lowerBetter: true,  w: 1.0 },
    { k: (s) => s.store_rating,         lowerBetter: false, w: 1.0 },
    { k: (s) => s.years,                lowerBetter: false, w: 0.8 },
    { k: (s) => s.respHours,            lowerBetter: true,  w: 0.6 },
    { k: (s) => s.reorderPct,           lowerBetter: false, w: 1.0 },
  ];

  const ranges = dims.map((d) => {
    const vals = list.map(d.k).filter((v) => typeof v === "number");
    return vals.length ? [Math.min(...vals), Math.max(...vals)] : null;
  });

  list.forEach((s) => {
    let sum = 0, wt = 0;
    dims.forEach((d, i) => {
      const r = ranges[i], v = d.k(s);
      if (!r || typeof v !== "number") return;
      const [lo, hi] = r;
      let n = hi === lo ? 1 : (v - lo) / (hi - lo);
      if (d.lowerBetter) n = 1 - n;
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
    s.vetScore = Math.round(Math.max(0, base * 100 - s.flags.length * 6));
  });

  return list.sort((a, b) => b.vetScore - a.vetScore);
}
