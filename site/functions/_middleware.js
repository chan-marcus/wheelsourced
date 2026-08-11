// Visitor tracking for /stats. Runs on every request to the site, so the
// first rule is that it can never break a page: everything below is wrapped,
// the response is fetched and returned regardless, and the KV writes happen
// after the response is already on its way out via waitUntil().
//
// PRIVACY: no raw IP is ever stored. The visitor id is
// SHA-256(ip + user-agent + ADMIN_KEY) truncated to 16 hex chars, and the
// secret salt means it cannot be recomputed from a guessed IP by anyone who
// does not already have admin access.
//
// The id is deliberately STABLE across days rather than rotating nightly.
// That is a real tradeoff and it is the second version of this file: rotating
// the id daily is more private, but it makes "new vs returning" unanswerable
// by construction, since yesterday's visitor is unrecognisable today. Showing
// which visitors are new was the point of the page, so the id persists. It is
// still pseudonymous and still not reversible to a person, but it can follow
// one browser across visits, which the rotating version could not.
//
// THREE WRITES per pageview, each answering a different question cheaply:
//   pv:<invTs>:<rand>  one row in the event feed. The timestamp is inverted
//                      so KV's lexicographic key order puts newest first and
//                      the feed is a single bounded list() with no paging.
//   uv:<date>:<vid>    one key per visitor per day. Counting keys gives daily
//                      uniques without a hot counter, which KV rate-limits to
//                      about one write a second and would be hit every view.
//   seen:<vid>         first-ever sighting. Its absence is what makes a
//                      visitor "New", so it is written only on a first visit.

const TTL_EVENTS = 60 * 60 * 24 * 90;   // 90d of feed + daily rollups
const TTL_SEEN = 60 * 60 * 24 * 400;    // identity outlives the events it explains

// Only count real page reads. Without this every favicon, font, stylesheet
// and wheel image would land in the numbers, and "page views" would mostly be
// a measure of how many assets the page happens to load.
function isPageView(request, url) {
  if (request.method !== "GET") return false;
  if (url.pathname.startsWith("/api/")) return false;
  if (url.pathname === "/stats") return false;         // admin, not an audience
  if (/\.[a-z0-9]+$/i.test(url.pathname)) return false; // .png/.css/.js/.ico/.txt
  return (request.headers.get("Accept") || "").includes("text/html");
}

async function visitorId(request, salt) {
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const ua = request.headers.get("User-Agent") || "";
  const data = new TextEncoder().encode(`${ip}|${ua}|${salt}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].slice(0, 8)
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Own-domain referrers are navigation, not traffic sources, and would drown
// out the ones worth seeing. Everything else is kept as its origin only:
// the full URL of the page someone came from is more than is needed to know
// where traffic comes from.
function referrerOf(request, url) {
  const raw = request.headers.get("Referer");
  if (!raw) return null;
  try {
    const r = new URL(raw);
    if (r.hostname === url.hostname) return null;
    return r.origin;
  } catch {
    return null;
  }
}

async function record(request, env, url) {
  const now = new Date();
  const iso = now.toISOString();
  const date = iso.slice(0, 10);
  const vid = await visitorId(request, env.ADMIN_KEY || "unsalted");

  // Is this the first time we have ever seen this browser?
  const seenKey = `seen:${vid}`;
  const seen = await env.SITE_STATS.get(seenKey);
  const isNew = !seen;

  // Inverted timestamp: KV lists keys in lexicographic order, so subtracting
  // from a fixed ceiling makes the newest event sort first. Without this the
  // feed would need every key read and reversed just to show the last 100.
  const inv = String(1e15 - now.getTime()).padStart(16, "0");
  const rand = crypto.randomUUID().slice(0, 8);

  const writes = [
    env.SITE_STATS.put(`pv:${inv}:${rand}`, JSON.stringify({
      vid,
      path: url.pathname,
      ts: iso,
      ref: referrerOf(request, url),
      country: request.headers.get("CF-IPCountry") || null,
      isNew,
    }), { expirationTtl: TTL_EVENTS }),

    env.SITE_STATS.put(`uv:${date}:${vid}`, "1", { expirationTtl: TTL_EVENTS }),
  ];
  if (isNew) {
    writes.push(env.SITE_STATS.put(seenKey, iso, { expirationTtl: TTL_SEEN }));
  }
  await Promise.all(writes);
}

export async function onRequest(context) {
  const { request, env, next, waitUntil } = context;
  const response = await next();

  try {
    const url = new URL(request.url);
    if (env.SITE_STATS && isPageView(request, url)) {
      // Fire and forget. The visitor is not kept waiting on KV round trips,
      // and a KV failure cannot turn a working page into an error.
      waitUntil(record(request, env, url).catch(() => {}));
    }
  } catch {
    // Counting is never worth a broken page.
  }

  return response;
}
