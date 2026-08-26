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
// THE KEYS, and how often each is written. That last part matters: the free
// plan allows 1,000 KV writes a day ACROSS THE ACCOUNT, which is the same
// allowance the wheel request and supplier suggestion forms draw on. Counting
// a pageview and saving a lead come out of one budget, so anything written
// here more often than it has to be is spent twice.
//
//   pv:<invTs>:<rand>  one row in the event feed, written on every view --
//                      genuinely one key per view. The timestamp is inverted
//                      so KV's lexicographic key order puts newest first and
//                      the feed is a single bounded list() with no paging.
//   uv:<date>:<vid>    one key per visitor per day. Counting keys gives daily
//                      uniques without a hot counter, which KV rate-limits to
//                      about one write a second and would be hit every view.
//                      Written only on the day's FIRST view, decided by the
//                      wsday cookie -- see record(). Writing it on every view
//                      rewrote an identical key all day for no added
//                      information and was the bulk of the daily spend.
//   seen:<vid>         first-ever sighting. Its absence is what makes a
//                      visitor "New", so it is written only on a first visit.
//
// Bots are dropped before any of it (see isBot). They keep no cookies, so
// every hit was a cookie-less first visit paying the full read-plus-writes
// price, to count something that is not an audience.

const TTL_EVENTS = 60 * 60 * 24 * 90;   // 90d of feed + daily rollups
const TTL_SEEN = 60 * 60 * 24 * 400;    // identity outlives the events it explains
const VID_COOKIE = "wsvid";
const VID_MAX_AGE = 60 * 60 * 24 * 400;
// Marks "this browser has already been counted in today's uniques". Holds the
// UTC date it was set for, so it self-invalidates at the day boundary: the
// value stops matching tomorrow regardless of when the cookie itself expires,
// which is why the expiry below can be a loose two days instead of something
// that has to land exactly on midnight.
const DAY_COOKIE = "wsday";
const DAY_MAX_AGE = 60 * 60 * 48;

// Automated traffic, dropped before it costs anything. Two reasons, and the
// second is what forced this: crawlers are not the audience this page exists
// to describe, and they are also the most expensive visitors there are. A bot
// keeps no cookies, so every single hit looks like a cookie-less first visit
// -- a seen: read, sometimes a seen: write, and the pv:/uv: pair on top. On
// the free plan that is a 1,000 writes/day budget being spent to count robots,
// and it is the same budget the wheel request form needs to save a real lead.
//
// User-Agent matching only, deliberately. Bot Management scores are not
// available on this plan, and a substring list that misses a few obscure
// crawlers is fine here: a false negative costs one counted pageview and a
// false positive costs one uncounted human, so neither direction is worth
// being clever about.
const BOT_UA = /bot|crawl|spider|slurp|scrape|curl|wget|python-requests|httpx|aiohttp|scrapy|go-http-client|okhttp|java\/|libwww|axios|node-fetch|headless|phantomjs|puppeteer|playwright|lighthouse|pagespeed|facebookexternalhit|whatsapp|telegram|discord|slackbot|embedly|preview|monitor|uptime|pingdom|statuscake|feedfetcher|archiver|ahrefs|semrush|mj12|dotbot|petalbot|bytespider|dataprovider|serpstat|screaming frog/i;

function isBot(request) {
  const ua = request.headers.get("User-Agent") || "";
  // No UA at all is not a browser; real ones always send something.
  return ua === "" || BOT_UA.test(ua);
}

// Only count real page reads. Without this every favicon, font, stylesheet
// and wheel image would land in the numbers, and "page views" would mostly be
// a measure of how many assets the page happens to load.
function isPageView(request, url) {
  if (request.method !== "GET") return false;
  if (isBot(request)) return false;
  if (url.pathname.startsWith("/api/")) return false;
  if (url.pathname === "/stats") return false;         // admin, not an audience
  if (/\.[a-z0-9]+$/i.test(url.pathname)) return false; // .png/.css/.js/.ico/.txt
  return (request.headers.get("Accept") || "").includes("text/html");
}

function getCookie(request, name) {
  const header = request.headers.get("Cookie") || "";
  const m = header.match(new RegExp(`(?:^|;\\s*)${name}=([A-Za-z0-9_-]+)`));
  return m ? m[1] : null;
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

async function record(request, env, url, countedToday) {
  const now = new Date();
  const iso = now.toISOString();
  const date = iso.slice(0, 10);

  // Identity, in order of preference:
  //
  // 1. The vid cookie, if the browser sent one back. This is what makes a
  //    visitor survive an IP change, and mobile IPs change constantly:
  //    switching wifi to cellular, moving between towers, and IPv6 privacy
  //    extensions rotating the interface identifier on a schedule all give
  //    the same person a different address. Hashing the IP alone made every
  //    one of those look like a brand new visitor, which is what made the
  //    feed read as nothing but New badges that never collapse.
  //
  // 2. Otherwise the IP+UA hash, as before. Crawlers do not keep cookies, so
  //    without this fallback every bot hit would be a new visitor forever.
  //
  // The cookie is SET TO the IP hash rather than to a fresh random value, on
  // purpose: the id is then identical whether or not the browser keeps the
  // cookie, so a visitor's very first view and their second do not appear
  // under two different ids the way they would if the cookie introduced a
  // new value on visit two.
  const ipHash = await visitorId(request, env.ADMIN_KEY || "unsalted");
  const cookieVid = getCookie(request, VID_COOKIE);
  const vid = cookieVid || ipHash;

  // A returned cookie proves we have met before, and it proves it without a
  // KV read, which also removes a real race: two views in the same second
  // both read seen: before either wrote it, so both were flagged New. The
  // browser holding a cookie cannot race with itself that way.
  let isNew = false;
  if (!cookieVid) {
    const seenKey = `seen:${ipHash}`;
    isNew = !(await env.SITE_STATS.get(seenKey));
    if (isNew) {
      await env.SITE_STATS.put(seenKey, iso, { expirationTtl: TTL_SEEN });
    }
  }

  // Inverted timestamp: KV lists keys in lexicographic order, so subtracting
  // from a fixed ceiling makes the newest event sort first. Without this the
  // feed would need every key read and reversed just to show the last 100.
  const inv = String(1e15 - now.getTime()).padStart(16, "0");
  const rand = crypto.randomUUID().slice(0, 8);

  // pv: is genuinely one key per view. uv: is not: it is one key per visitor
  // per day, so it only ever needed writing on that day's FIRST view. It used
  // to be written on every one, which meant someone reading ten pages rewrote
  // the identical key ten times -- nine wasted writes out of a 1,000/day
  // budget, and close enough to KV's one-write-per-second-per-key ceiling
  // that a fast click-through could have the write rejected anyway. The day
  // cookie set in onRequest is what makes the first view tellable from the
  // rest without a read.
  const writes = [
    env.SITE_STATS.put(`pv:${inv}:${rand}`, JSON.stringify({
      vid,
      path: url.pathname,
      ts: iso,
      ref: referrerOf(request, url),
      country: request.headers.get("CF-IPCountry") || null,
      isNew,
    }), { expirationTtl: TTL_EVENTS }),
  ];

  // A browser that refuses cookies can never report being counted, so it
  // still writes uv: on every view. That is the old behaviour, kept on
  // purpose: the alternative is undercounting a real visitor, and this is now
  // the rare case rather than the common one.
  if (!countedToday) {
    writes.push(
      env.SITE_STATS.put(`uv:${date}:${vid}`, "1", { expirationTtl: TTL_EVENTS }),
    );
  }

  await Promise.all(writes);

  return vid;
}

export async function onRequest(context) {
  const { request, env, next, waitUntil } = context;
  let response = await next();

  try {
    const url = new URL(request.url);
    if (env.SITE_STATS && isPageView(request, url)) {
      // Whether this browser is already in today's uniques. Decided here and
      // not in record(), because the answer is a cookie and only this side of
      // waitUntil() can still put a header on the response.
      const today = new Date().toISOString().slice(0, 10);
      const countedToday = getCookie(request, DAY_COOKIE) === today;

      // Fire and forget. The visitor is not kept waiting on KV round trips,
      // and a KV failure cannot turn a working page into an error.
      waitUntil(record(request, env, url, countedToday).catch(() => {}));

      const cookies = [];

      // Set the id cookie if they did not send one. Computed here rather
      // than read out of record(), because that runs after the response has
      // gone and cannot add a header to it any more.
      if (!getCookie(request, VID_COOKIE)) {
        const vid = await visitorId(request, env.ADMIN_KEY || "unsalted");
        cookies.push(
          `${VID_COOKIE}=${vid}; Path=/; Max-Age=${VID_MAX_AGE}; HttpOnly; Secure; SameSite=Lax`,
        );
      }

      // Stamp today's date so this browser's next view skips the uv: write.
      if (!countedToday) {
        cookies.push(
          `${DAY_COOKIE}=${today}; Path=/; Max-Age=${DAY_MAX_AGE}; HttpOnly; Secure; SameSite=Lax`,
        );
      }

      if (cookies.length) {
        // next() can hand back an immutable response for a static asset, so
        // rebuild it rather than mutating headers in place.
        response = new Response(response.body, response);
        for (const c of cookies) response.headers.append("Set-Cookie", c);
      }
    }
  } catch {
    // Counting is never worth a broken page.
  }

  return response;
}
