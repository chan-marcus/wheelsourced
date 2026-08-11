// Visitor counting for /stats. Runs on every request to the site, so the
// first rule is that it can never break a page: everything below is wrapped,
// the response is fetched and returned regardless, and the KV write happens
// after the response is already on its way out via waitUntil().
//
// PRIVACY: no raw IP is ever stored. The visitor id is
// SHA-256(ip + user-agent + date + ADMIN_KEY) truncated to 16 hex chars.
// Including the date means the id rotates at midnight UTC, so it identifies
// "a visitor today" and cannot be used to follow someone across days.
// Including ADMIN_KEY as a salt means the hash cannot be recomputed from a
// guessed IP by anyone who does not already have the admin secret. Nothing
// stored is reversible to a person.
//
// STORAGE SHAPE: one key per visitor per day, `uv:<date>:<id>`, holding a
// small JSON blob. That single shape answers both questions cheaply:
//   unique visitors that day = number of keys with that date prefix
//   pageviews that day       = sum of .views across them
// A per-day counter key would have been simpler but KV rate-limits writes to
// the same key to roughly one a second, and a hot key on every pageview is
// exactly the thing that limit exists to stop. Keying by visitor spreads the
// writes out; the only contention left is one person loading two pages in the
// same second, where the cost is an undercounted view, not an error.
//
// Keys expire after 90 days so the namespace cannot grow without bound.

const TTL_DAYS = 90;

// Only count real page reads. Without this every favicon, font, stylesheet
// and wheel image would land in the numbers, and "pageviews" would mostly be
// a measure of how many assets the page happens to load.
function isPageView(request, url) {
  if (request.method !== "GET") return false;
  if (url.pathname.startsWith("/api/")) return false;
  if (url.pathname === "/stats") return false;         // admin, not an audience
  if (/\.[a-z0-9]+$/i.test(url.pathname)) return false; // .png/.css/.js/.ico/.txt
  const accept = request.headers.get("Accept") || "";
  return accept.includes("text/html");
}

async function visitorId(request, salt, date) {
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const ua = request.headers.get("User-Agent") || "";
  const data = new TextEncoder().encode(`${ip}|${ua}|${date}|${salt}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].slice(0, 8)
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function record(request, env, url) {
  const date = new Date().toISOString().slice(0, 10);
  // ADMIN_KEY doubles as the hash salt. It already exists, is already secret,
  // and rotating it only costs a discontinuity in visitor ids, not data loss.
  const id = await visitorId(request, env.ADMIN_KEY || "unsalted", date);
  const key = `uv:${date}:${id}`;

  const existing = await env.SITE_STATS.get(key, "json");
  const paths = new Set(existing?.paths || []);
  paths.add(url.pathname);

  await env.SITE_STATS.put(key, JSON.stringify({
    views: (existing?.views || 0) + 1,
    // Capped: a crawler walking every page should not turn one key into an
    // unbounded list, and 12 distinct paths is already more than this site has.
    paths: [...paths].slice(0, 12),
    country: request.headers.get("CF-IPCountry") || null,
    first: existing?.first || new Date().toISOString(),
    last: new Date().toISOString(),
  }), { expirationTtl: 60 * 60 * 24 * TTL_DAYS });
}

export async function onRequest(context) {
  const { request, env, next, waitUntil } = context;
  const response = await next();

  try {
    const url = new URL(request.url);
    if (env.SITE_STATS && isPageView(request, url)) {
      // Fire and forget. The visitor is not kept waiting on a KV round trip,
      // and a KV failure cannot turn a working page into an error.
      waitUntil(record(request, env, url).catch(() => {}));
    }
  } catch {
    // Counting is never worth a broken page.
  }

  return response;
}
