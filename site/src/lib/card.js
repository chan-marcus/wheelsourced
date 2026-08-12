// The supplier card template, shared by the build and the browser.
//
// This used to live inside the page's inline <script>, which meant the entire
// sourcing dataset -- every supplier, price, score and spec, the only content
// on this site nobody else has -- reached the browser as a JSON blob and was
// drawn by JS. Stripped of scripts, which is what a crawler reads before it
// decides whether to spend rendering budget on you, a wheel sheet was 546
// words of boilerplate and not one supplier name. Google runs JS on a second,
// deferred pass; Bing and the AI crawlers largely do not run it at all.
//
// So the rows are rendered at build time now. The same functions still run in
// the browser to re-sort, which is the reason this is a module rather than a
// second copy of the template: two templates in two languages drift, and the
// drift would show up as the page changing under you on first sort.
//
// Locales are pinned. toLocaleString() with no argument follows the host's
// locale, so a build machine set to de-DE would emit "1.456" into the HTML
// and the browser would rewrite it to "1,456" on the first sort.
const money = (n) => "$" + Math.round(n).toLocaleString("en-US");

// Escape before interpolating into an HTML attribute. The outbound link is now
// an affiliate URL typed into a form rather than a scraped listing, and a
// quote in it would close the href early and let the rest parse as markup.
const attr = (v) => String(v ?? "")
  .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
  .replace(/</g, "&lt;").replace(/>/g, "&gt;");

// "4 pieces" -> "4". The Min. order label already says what is being counted.
// Only piece/pieces: a "2 sets" MOQ means something different from "2" and
// keeps its unit. Display only, because s.moq is compared as a whole string
// to decide whether a per-wheel figure is meaningful.
const moqCount = (v) =>
  typeof v === "string" ? v.replace(/\s*pieces?\b\.?/i, "").trim() || v : v;

// Whether the certificate itself ever arrived, shown grey beside the names
// because it qualifies them rather than answering for them. "DOT, VIA" with
// nothing attached is a claim, and the sheet has to keep saying so.
//
// Short forms only when they follow the names, where "claimed" would just
// repeat the names being right there. Standing alone the stored phrase is
// left long, because it has to describe itself: a bare "no document" would
// not say that a certification was claimed at all.
const CERT_DOC = {
  "provided": "on file",
  "promised": "promised",
  "claimed without document": "no document",
  "not available": "none available",
};
const certDoc = (v) =>
  v ? ` <span class="grey">${CERT_DOC[v] || v}</span>` : "";

// Standing alone the stored phrase is usually self-describing, so it is used
// as it is. "not available" is the exception: printed raw it sits one word
// away from the "not disclosed" that means the supplier never answered, and a
// supplier who told us they have no certificate must not read like one who
// ignored the question.
const CERT_DOC_ALONE = { "not available": "none available" };
const certAlone = (v) => (v ? CERT_DOC_ALONE[v] || v : null);

const or = (v, unit = "") =>
  v === null || v === undefined || v === ""
    ? `<span class="none">not disclosed</span>` : `${v}${unit}`;

// Sorted copy. Never sorts in place: the build renders one order and the
// browser re-sorts the same array repeatedly, and a shared mutable array
// would make the second sort depend on the first.
export function orderSuppliers(suppliers, sortMode) {
  const l = suppliers.slice();
  const by = (get, dir) => l.sort((a, b) => {
    const av = get(a), bv = get(b);
    if (av == null && bv == null) return b.answered - a.answered;
    if (av == null) return 1;
    if (bv == null) return -1;
    return dir * (av - bv);
  });
  switch (sortMode) {
    case "price":    return by((s) => s.landed ?? s.setPrice, 1);
    case "rating":   return by((s) => s.store_rating, -1);
    case "age":      return by((s) => s.years, -1);
    case "reorder":  return by((s) => s.reorderPct, -1);
    default:         return by((s) => s.vetScore, -1);
  }
}

export function supplierCard(s, { sortMode = "vetting", i = 0, open = new Set() } = {}) {
  return `
    <article class="v ${sortMode === "vetting" && i === 0 && s.clean ? "top" : ""}">
      <div class="vh">
        <div class="vinfo">
          <div class="vn">${s.name}</div>
          <div class="vm" style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px;">
            ${s.verified ? `<span class="tag good">verified supplier</span>` : ""}
            ${s.business_type ? `<span class="tag">${s.business_type}</span>` : ""}
            <span class="tag">${s.forged === true ? "forged" : s.forged === false ? "not forged" : "forging unconfirmed"}</span>
            ${Array.isArray(s.pieces) ? s.pieces.map(p => `<span class="tag">${p}</span>`).join("") : `<span class="tag">${s.pieces || "piece count unknown"}</span>`}
          </div>
        </div>
        <div class="vp">
          ${s.landed
            ? `<b>${money(s.landed)}</b><small>est. landed / set of 4</small>`
            : s.setPrice
              ? `<b>${money(s.setPrice)}</b><small>set of 4, before freight</small>
                 <small class="warnsm">freight not quoted</small>`
              : `<b class="grey">${s.listed_price ? money(s.listed_price * 4) : "—"}</b>
                 <small>listed only, not quoted</small>`}
        </div>
      </div>

      <div class="headline" data-toggle="${s.id}" style="cursor: pointer;">
        <div class="${sortMode === "rating" ? "hl on" : "hl"}">
          <dt>Store rating</dt><dd>${s.store_rating ? s.store_rating + "/5" : "—"}
          ${s.review_count ? `<em>(${s.review_count})</em>` : ""}</dd></div>
        <div class="${sortMode === "age" ? "hl on" : "hl"}">
          <dt>Store age</dt><dd>${s.years ? s.years + (s.years === 1 ? " yr" : " yrs") : "—"}</dd></div>
        <div class="${sortMode === "reorder" ? "hl on" : "hl"}">
          <dt>Reorder rate</dt><dd>${s.reorder ?? "—"}</dd></div>
        <div class="hl">
          <dt>Shipping time</dt><dd>${
            s.ship_time ? s.ship_time
            : s.transit ? `${s.transit.replace(/^Est\.\s*/, "")} <em>listed</em>`
            : "—"}</dd></div>
        <div class="${sortMode === "vetting" ? "hl on" : "hl"}">
          <dt>Vetting score</dt><dd>${s.vetScore ?? "—"}<em>/100</em></dd></div>
      </div>

      <div style="margin-top: 12px;">
        <button class="btn ghost" data-toggle="${s.id}" data-toggle-btn
          aria-controls="specs-${s.id}" aria-expanded="${open.has(s.id)}"
          >${open.has(s.id) ? "Hide details" : "Show details"}</button>
        <dl class="specs" id="specs-${s.id}"
          style="margin-top:12px;max-height:${open.has(s.id) ? "999px" : "0"};opacity:${open.has(s.id) ? "1" : "0"};overflow:hidden;transition:max-height 250ms cubic-bezier(0.4,0,0.2,1), opacity 250ms cubic-bezier(0.4,0,0.2,1);">
          <div><dt>Per wheel</dt><dd>${
            s.price ? money(s.price)
            : s.listed_price ? `<span class="grey">${money(s.listed_price)} listed</span>`
            : or(null)}</dd></div>
          <div><dt>Set of 4</dt><dd>${
            s.setPrice ? money(s.setPrice) : or(null)}</dd></div>
          <div><dt>Est. Shipping Cost</dt><dd>${s.freight ? money(s.freight) : or(null)}</dd></div>
          <div><dt>Total delivered</dt><dd>${
            s.landed
              ? money(s.landed) + (s.landed_quoted ? ` <em>quoted all-in</em>` : "")
              : or(null)}</dd></div>
          <div><dt>Manufacturing time</dt><dd>${or(s.lead_time)}</dd></div>
          <div><dt>Min. order</dt><dd>${or(moqCount(s.moq))}</dd></div>
          <div><dt>Forged</dt><dd>${s.forged === true ? "confirmed"
            : s.forged === false ? "no" : or(null)}</dd></div>
          <div><dt>Process stated</dt><dd>${or(s.forging_process)}</dd></div>
          <div><dt>Pieces</dt><dd>${or(s.pieces)}</dd></div>
          <div><dt>Certification</dt><dd>${
            s.certs ? s.certs.join(", ") + certDoc(s.cert) : or(certAlone(s.cert))}</dd></div>
          <div><dt>Heat treat</dt><dd>${or(s.heat_treat)}</dd></div>
          <div><dt>CNC</dt><dd>${s.cnc === true ? "in house"
            : s.cnc === false ? "outsourced" : or(null)}</dd></div>
          <div class="prose"><dt>Payment terms</dt><dd>${or(s.payment)}</dd></div>
          <div class="prose"><dt>Damage/Defect policy</dt><dd>${or(s.defects)}</dd></div>
        </dl>
      </div>

      <div class="vf" style="margin-top:12px;">
        <button class="btn" data-rfq="${s.id}">Write quote request</button>
        ${s.url ? `<a class="btn ghost" href="${attr(s.url)}" rel="nofollow noopener" target="_blank">Open listing</a>` : ""}
        <span class="seen">${s.verified_on ? "last verified " + s.verified_on : ""}</span>
      </div>
    </article>`;
}

export function renderRows(suppliers, { sortMode = "vetting", open = new Set() } = {}) {
  return orderSuppliers(suppliers, sortMode)
    .map((s, i) => supplierCard(s, { sortMode, i, open }))
    .join("");
}
