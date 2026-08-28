// Records a click on an "Open listing" button. Sibling of suggest-supplier.js
// and request-wheel.js in shape, but not in purpose: those two save something a
// person typed, this one counts something they did.
//
// WHY A BEACON AND NOT A REDIRECT. The obvious way to count outbound clicks is
// to point the button at /go/<supplier> and 302 from there, which catches every
// click including the JS-disabled ones. It is not the way this site can do it.
// The href IS the disclosure: a reader hovering an affiliate link sees the
// alibaba.com URL it actually goes to, and principle 1 is that the reader can
// always tell where a figure came from. Swapping that for an opaque first-party
// redirect buys a few percent more coverage by hiding the destination on the
// one site whose entire product is not hiding things. So the href stays real
// and the click is reported alongside it with sendBeacon(). Clicks from readers
// running no JS are not counted. That is the accepted cost, and it is the same
// trade the rest of the analytics makes.
//
// WRITES. One KV write per click, on the free plan's 1,000/day ACCOUNT-WIDE
// budget that _middleware.js and both forms already draw on -- see the long
// note there. Clicks are a fraction of pageviews so this is a small addition,
// but it is not free, and it is why there is no second rollup key: the per-
// supplier totals on /stats are counted from these key NAMES, which list()
// returns without a read, so one write covers both the feed and the counts.
//
// PRIVACY. Same posture as _middleware.js: no raw IP is stored. The visitor id
// comes from the wsvid cookie that middleware already set, which is a salted
// hash and arrives here on its own because the beacon is same-origin.
// Cloudflare's own geo and network fields are recorded (see GEO below) and are
// a step past what a pageview stores; they are what makes a click legible as a
// prospect rather than a tally mark, and none of them is an address.

const SLUG = /^[a-z0-9][a-z0-9-]{0,79}$/;   // wheel slugs and supplier ids both
const TTL = 60 * 60 * 24 * 90;              // 90d, matching the pageview feed

// A click is one row, so the same inverted-timestamp trick as pv: keys applies:
// KV lists lexicographically, so newest-first ordering is free and the feed is
// one bounded list() with no paging. The wheel and supplier ride IN THE KEY on
// purpose -- list() hands back key names without reading a single value, so
// /stats totals every click per supplier for zero reads and only fetches the
// values for the handful of rows it actually prints.
const key = (wheel, supplier) =>
  `clk:${String(1e15 - Date.now()).padStart(16, "0")}:${wheel}:${supplier}:${crypto.randomUUID().slice(0, 8)}`;

// Coarse, on purpose. "Safari on iPhone" is the useful part of a user agent for
// judging a prospect; the build numbers are the part that makes it a
// fingerprint. Order matters twice: Edge and Chrome both say "Chrome", and
// every iPad since iPadOS 13 says "Macintosh" unless the touch hint is checked
// first.
function device(ua) {
  const s = ua || "";
  const os = /iPhone/.test(s) ? "iPhone"
    : /iPad|Macintosh(?=.*Mobile)/.test(s) ? "iPad"
    : /Android/.test(s) ? "Android"
    : /Macintosh|Mac OS X/.test(s) ? "Mac"
    : /Windows/.test(s) ? "Windows"
    : /Linux/.test(s) ? "Linux" : null;
  const browser = /Edg\//.test(s) ? "Edge"
    : /OPR\/|Opera/.test(s) ? "Opera"
    : /Firefox\//.test(s) ? "Firefox"
    : /Chrome\//.test(s) ? "Chrome"
    : /Safari\//.test(s) ? "Safari" : null;
  return [browser, os].filter(Boolean).join(" / ") || null;
}

function getCookie(request, name) {
  const header = request.headers.get("Cookie") || "";
  const m = header.match(new RegExp(`(?:^|;\\s*)${name}=([A-Za-z0-9_-]+)`));
  return m ? m[1] : null;
}

// Same-origin only. This endpoint writes to KV without a password, so the thing
// worth blocking is a script somewhere else pointing at it and burning the
// day's write budget. A header check does not stop anyone determined -- curl
// sets any header it likes -- but it costs one comparison and it stops the
// entire casual case.
//
// Origin OR Referer, not Origin alone. Every browser is supposed to send Origin
// on a POST and sendBeacon is a POST, but the failure mode if one does not is
// that every click from that browser is silently dropped and the number just
// reads low forever, which is the worst way for an analytics figure to be
// wrong. Referer is checked as a second chance at the same question.
function sameOrigin(request, url) {
  const host = (raw) => {
    try {
      return new URL(raw).host;
    } catch {
      return null;
    }
  };
  const origin = request.headers.get("Origin");
  if (origin) return host(origin) === url.host;
  const referer = request.headers.get("Referer");
  return referer ? host(referer) === url.host : false;
}

export async function onRequestPost(context) {
  const { request, env } = context;
  const url = new URL(request.url);

  // 204 rather than an error for everything below. sendBeacon() discards the
  // response and the visitor is already on their way to Alibaba, so there is
  // nobody to tell. A rejected click must not look like a broken one either:
  // the button's job is to open the listing, and counting is never worth
  // interfering with that.
  const ok = () => new Response(null, { status: 204 });

  if (!env.SITE_STATS || !sameOrigin(request, url)) return ok();

  let body;
  try {
    body = await request.json();
  } catch {
    return ok();
  }

  // Both are interpolated into a KV key, so they are matched against a strict
  // slug pattern rather than escaped. Every real value is already a kebab slug
  // -- wheel directory names and the id field of a supplier YAML -- so anything
  // failing this is not a click on a button that exists.
  const wheel = String(body?.wheel ?? "");
  const supplier = String(body?.supplier ?? "");
  if (!SLUG.test(wheel) || !SLUG.test(supplier)) return ok();

  // GEO. Cloudflare fills request.cf on its own, so none of this is derived
  // from an address we store. asOrganization is the network the visitor is on,
  // which is the field that separates a buyer at home from a datacentre range
  // pretending to be one. All optional: cf is undefined in `wrangler dev`
  // without --remote, and individual fields go missing on some routes.
  const cf = request.cf || {};

  // What the reader had done before clicking, reported by the page. None of it
  // is checkable and none of it needs to be -- a forged dwell time buys nobody
  // anything -- but it is the difference between "someone clicked" and "someone
  // read the specs for ninety seconds, sorted by landed price, and clicked the
  // third row".
  const dwell = Number.isFinite(body?.dwell) ? Math.min(Math.round(body.dwell), 86400) : null;
  const rank = Number.isFinite(body?.rank) ? Math.min(Math.round(body.rank), 999) : null;
  const sort = /^[a-z]{1,12}$/.test(String(body?.sort ?? "")) ? String(body.sort) : null;

  const entry = {
    vid: getCookie(request, "wsvid"),   // set by _middleware.js on the pageview
    wheel,
    supplier,
    ts: new Date().toISOString(),
    country: request.headers.get("CF-IPCountry") || null,
    city: cf.city || null,
    region: cf.region || null,
    network: cf.asOrganization || null,
    device: device(request.headers.get("User-Agent")),
    dwell,
    rank,
    sort,
    // Whether they had the spec sheet open on this supplier at the moment they
    // clicked. The one field here that says whether the vetting was read or
    // scrolled past, which is the question this site exists to answer.
    opened: body?.opened === true,
  };

  try {
    await env.SITE_STATS.put(key(wheel, supplier), JSON.stringify(entry), {
      expirationTtl: TTL,
    });
  } catch (e) {
    // Same reasoning as the forms: a dropped write is invisible from this end
    // otherwise, and the realistic cause is the shared daily budget running out
    // exactly when traffic is heaviest. Unlike the forms there is no visitor to
    // apologise to, so the log is the whole remedy.
    console.error("SITE_STATS click put failed", wheel, supplier, e);
  }

  return ok();
}

// A GET is someone poking at the endpoint. Say so rather than falling through
// to the SPA 404, same as the other two functions in this directory.
export const onRequestGet = () =>
  new Response(JSON.stringify({ ok: false, error: "POST a listing click to this endpoint." }), {
    status: 405,
    headers: { "content-type": "application/json" },
  });
