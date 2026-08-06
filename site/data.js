// Reads the same YAML files pipeline.py writes. No database, no API.
// Runs at build time only, so the browser gets a plain JSON blob.

import fs from "node:fs";
import path from "node:path";
import yaml from "js-yaml";

const WHEELS_DIR = path.resolve("../wheels");

const num = (v) => (typeof v === "number" && !Number.isNaN(v) ? v : null);

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

      return { slug, title: titleize(slug), suppliers };
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
    listed_price: num(L.listed_price_low),
    price,
    price4: num(Q.price_per_wheel_4set),
    freight,
    freight_estimated: price && freight === null,
    landed,
    setPrice,
    weight: num(Q.weight_kg),
    load_rating: num(Q.load_rating_kg),
    forging: Q.forging_process || null,
    cert: Q.cert_document || null,
    size_chart: Q.size_chart || null,
    is_manufacturer: Q.is_manufacturer ?? null,
    flags: r.flags || [],
    answered,
    total: Object.keys(Q).length,
    verified_on: (r.meta && (r.meta.reply_received || r.meta.listing_seen)) || null,
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
