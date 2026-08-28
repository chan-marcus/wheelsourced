// Cloudflare Pages Function serving /stats: traffic, plus the admin view of
// what has come in through the two public forms. Password-gated, because the
// KV stores it reads hold real people's email addresses, and the local
// equivalent (app.py's /requests) was only ever safe to leave open because
// nothing outside this machine could reach it.
//
// Visitor numbers come from the keys _middleware.js writes, one per visitor
// per day. See that file for why the shape is what it is and what is and is
// not stored about a visitor.
//
// Stateless session: the cookie is not a token looked up in storage, it is
// HMAC-SHA256(ADMIN_KEY, a fixed string), recomputed and compared on every
// request. Knowing the cookie value requires having typed the real password
// at least once; there is nothing to steal from a session store because
// there is no session store.
//
// Setup: `wrangler pages secret put ADMIN_KEY --project-name=wheelsourced`.
// Without it every login attempt fails closed (500, not a silent bypass).

const SESSION_STRING = "wheelsourced-admin-session";
const COOKIE = "admin_auth";
const MAX_AGE = 60 * 60 * 24 * 180; // 180 days

async function hmac(key, message) {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    "raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Constant-time-ish compare. The realistic threat here is someone guessing
// the password, not shaving milliseconds off a hex compare over the network,
// but it costs nothing to not leak length-by-timing either.
function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function getCookie(request, name) {
  const header = request.headers.get("Cookie") || "";
  const match = header.match(new RegExp(`(?:^|;\\s*)${name}=([^;]+)`));
  return match ? match[1] : null;
}

async function isAuthed(request, env) {
  const cookie = getCookie(request, COOKIE);
  if (!cookie || !env.ADMIN_KEY) return false;
  const expected = await hmac(env.ADMIN_KEY, SESSION_STRING);
  return safeEqual(cookie, expected);
}

const esc = (v) =>
  String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

async function readAll(kv, prefix) {
  if (!kv) return [];
  const { keys } = await kv.list({ prefix });
  const vals = await Promise.all(keys.map((k) => kv.get(k.name)));
  return vals.filter(Boolean).map((v) => JSON.parse(v))
    // Newest first. Keys are prefix:timestamp:uuid, so this also works
    // without parsing the date field, but sorting on the stored date keeps
    // it correct even if the key format ever changes.
    .sort((a, b) => (b.date || "").localeCompare(a.date || ""));
}

// Traffic for the page: the recent event feed, plus daily rollups.
//
// The feed is a single bounded list(). _middleware.js writes pv: keys with an
// inverted timestamp precisely so KV's lexicographic order returns newest
// first, which turns "the last 100 views" into one call instead of reading
// every key in the namespace and sorting.
async function readFeed(kv, limit = 100) {
  const { keys } = await kv.list({ prefix: "pv:", limit });
  const vals = await Promise.all(keys.map((k) => kv.get(k.name, "json")));
  return vals.filter(Boolean);
}

// Daily uniques come from the uv: keys, where one key IS one visitor for one
// day, so counting keys is the whole calculation. Cursor-paginated rather
// than assuming the 1000-key page is everything: silently reporting low is
// the worst way for an analytics number to be wrong.
async function readDaily(kv, days = 30) {
  const cutoff = new Date(Date.now() - days * 864e5).toISOString().slice(0, 10);
  const byDay = new Map();
  let cursor;
  do {
    const res = await kv.list({ prefix: "uv:", cursor, limit: 1000 });
    for (const k of res.keys) {
      const day = k.name.split(":")[1];
      if (day >= cutoff) byDay.set(day, (byDay.get(day) || 0) + 1);
    }
    cursor = res.list_complete ? null : res.cursor;
  } while (cursor);
  return [...byDay.entries()].sort((a, b) => b[0].localeCompare(a[0]));
}

// Clicks on the "Open listing" buttons, written by functions/api/click.js.
//
// Two questions, answered two different ways on purpose. TOTALS come from key
// NAMES: the endpoint writes the wheel and the supplier into the key itself,
// and list() returns names without reading a single value, so every click ever
// recorded is counted per supplier for zero reads. DETAIL comes from values,
// and only for the newest few rows that actually get printed. Reading all of
// them to show forty would be the same waste readFeed exists to avoid.
//
// Cursor-paginated rather than trusting one 1000-key page, same as readDaily
// and for the same reason: a total that silently reads low is worse than one
// that is missing, because it looks like an answer.
async function readClicks(kv, detail = 40) {
  const bySupplier = new Map();
  const byDay = new Map();
  const newest = [];
  let total = 0;
  let cursor;

  do {
    const res = await kv.list({ prefix: "clk:", cursor, limit: 1000 });
    for (const k of res.keys) {
      // clk:<invertedTimestamp>:<wheel>:<supplier>:<rand>. Both slugs are
      // matched against /^[a-z0-9-]+$/ before they are ever written, so
      // splitting on ":" cannot be thrown off by a value containing one.
      const [, inv, wheel, supplier] = k.name.split(":");
      if (!supplier) continue;
      total++;
      const pair = `${wheel}/${supplier}`;
      bySupplier.set(pair, (bySupplier.get(pair) || 0) + 1);
      const day = new Date(1e15 - Number(inv)).toISOString().slice(0, 10);
      byDay.set(day, (byDay.get(day) || 0) + 1);
      // The inverted timestamp means lexicographic order IS newest-first, so
      // the first keys seen are the newest ones and no sort is needed.
      if (newest.length < detail) newest.push(k.name);
    }
    cursor = res.list_complete ? null : res.cursor;
  } while (cursor);

  const rows = (await Promise.all(newest.map((n) => kv.get(n, "json")))).filter(Boolean);

  return {
    total,
    rows,
    today: byDay.get(new Date().toISOString().slice(0, 10)) || 0,
    people: new Set(rows.map((r) => r.vid).filter(Boolean)).size,
    bySupplier: [...bySupplier.entries()].sort((a, b) => b[1] - a[1]),
  };
}

// What is known about each visitor from the pageview feed, keyed by visitor id.
// This is what turns a click from a tally mark into a person: the click record
// itself knows where they were and what they took, and this knows what they
// read on the way there and where they arrived from.
//
// Built from the same 100 rows readFeed already fetched, so it costs nothing
// extra -- and it is bounded by that, which is a real limit worth knowing:
// a click from someone whose pageviews have already scrolled out of the
// window shows its own fields and an empty session, rather than a wrong one.
function visitorIndex(feed) {
  const m = new Map();
  // Walked oldest-first. The feed arrives newest-first because that is the
  // order the page prints it in, but a session is a journey and every field
  // built here is about its ORDER: the path list should read the way they
  // browsed it, and the referrer worth keeping is the one they arrived on,
  // not whichever off-site link they touched most recently. Reversing once
  // here is what makes both of those the plain reading of the loop instead of
  // two separate off-by-one arguments in comments.
  for (const v of [...feed].reverse()) {
    let e = m.get(v.vid);
    if (!e) m.set(v.vid, (e = { views: 0, paths: [], isNew: false, ref: null }));
    e.views++;
    if (!e.paths.includes(v.path)) e.paths.push(v.path);
    if (v.isNew) e.isNew = true;
    if (v.ref && !e.ref) e.ref = v.ref;
  }
  return m;
}

async function readTraffic(kv) {
  if (!kv) return null;
  const [feed, daily, clicks] = await Promise.all([
    readFeed(kv), readDaily(kv), readClicks(kv),
  ]);
  const today = new Date().toISOString().slice(0, 10);
  return {
    feed,
    daily,
    clicks,
    visitors: visitorIndex(feed),
    uniquesToday: daily.find(([d]) => d === today)?.[1] || 0,
    uniques30: daily.reduce((t, [, n]) => t + n, 0),
    // Distinct people in the feed window, which the stable visitor id makes
    // answerable at all: the rotating id this replaced could not tell one
    // person on three days from three people.
    people: new Set(feed.map((v) => v.vid)).size,
    newPeople: feed.filter((v) => v.isNew).length,
  };
}

function page({ body, title = "WheelSourced - Stats" }) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>${title}</title>
<link rel="icon" type="image/png" href="/favicon-32.png" sizes="32x32">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* Kept in step with site/src/styles/global.css by hand: this page is a
   standalone Cloudflare Function and cannot import that stylesheet. Dark
   theme: --ink is the light text/border color, --stock the dark page bg,
   --stock2 a lighter (elevated) surface, same as the main site now. */
:root{color-scheme:dark;--stock:#424242;--stock2:#4F4F4F;--ink:#EDEFEB;--soft:#BBC0B5;--rule:#53594A;--chop:#EBA498;--bronze:#D6B271}
*{box-sizing:border-box}
body{margin:0;background:var(--stock);color:var(--ink);font:15px/1.5 'IBM Plex Mono',monospace;padding:0 20px 80px}
.sheet{max-width:1060px;margin:0 auto}
header{border-bottom:2px solid var(--ink);padding:26px 0 12px;margin-bottom:30px;display:flex;justify-content:space-between;align-items:baseline}
.logo{font-family:'Barlow Condensed';font-weight:700;font-size:26px;letter-spacing:-.05em;text-decoration:none;color:var(--ink)}
.logo em{font-style:normal;color:var(--bronze)}
h1{font-family:'Barlow Condensed';font-size:32px;font-weight:700;text-transform:uppercase;margin:36px 0 16px}
table{width:100%;border-collapse:collapse;margin-bottom:20px}
th{text-align:left;padding:8px 10px;border-bottom:2px solid var(--ink);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--soft)}
td{padding:8px 10px;border-bottom:1px solid var(--rule);font-size:12.5px;vertical-align:top;word-break:break-word}
tr:hover td{background:var(--stock2)}
.empty{color:var(--soft);font-style:italic;padding:20px 10px}
a{color:var(--bronze)}
form.login{max-width:340px;margin:60px auto 0;text-align:center}
input[type=password]{width:100%;font-family:'IBM Plex Mono';font-size:14px;padding:10px;border:1px solid var(--ink);background:transparent;color:var(--ink);margin-bottom:12px}
button{font-family:'IBM Plex Mono';font-size:11.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;padding:10px 16px;border:1px solid var(--ink);background:var(--ink);color:var(--stock);cursor:pointer;width:100%}
.err{color:var(--chop);font-size:12px;margin-bottom:12px}
.logout{font-size:11px;color:var(--soft);text-decoration:none}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px 20px;margin-bottom:8px}
.tile dt{font-size:9.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--soft);margin-bottom:4px}
.tile dd{margin:0;font-family:'Barlow Condensed';font-weight:700;font-size:34px;line-height:1}
/* Unique visitors is the number actually being asked for, so it is the one
   thing on the page in the accent colour. Everything else is context for it. */
.tile.key dd{color:var(--bronze)}
.tile dd small{font-family:'IBM Plex Mono';font-size:11px;font-weight:400;color:var(--soft)}
.bars{margin:10px 0 24px}
.bar{display:grid;grid-template-columns:92px 1fr 88px;align-items:center;gap:10px;padding:2px 0}
.bar span{font-size:11.5px;color:var(--soft)}
.bar .track{background:var(--stock2);height:14px;position:relative}
.bar .fill{background:var(--bronze);height:100%}
.bar .n{font-size:11.5px;text-align:right}
.bar .n b{font-weight:600}
.vid{font-size:12px;letter-spacing:.02em}
/* The whole point of the feed: a first-ever visit is called out, everything
   else is quiet. Bronze because it is already the accent everywhere else. */
.badge{display:inline-block;margin-left:8px;font-size:9px;font-weight:600;
  letter-spacing:.1em;text-transform:uppercase;color:var(--bronze);
  border:1px solid var(--bronze);border-radius:9px;padding:1px 7px;vertical-align:1px}
tr.new td{background:rgba(138,106,47,.07)}
tr.new:hover td{background:rgba(138,106,47,.13)}
td.dim{color:var(--soft)}
.when{white-space:nowrap}
tr.group-toggle{cursor:pointer}
tr.group-toggle .arrow{display:inline-block;font-size:9px;width:10px;transition:transform .12s;color:var(--soft)}
tr.group-toggle.open .arrow{transform:rotate(90deg)}
tr.group-toggle .count{margin-left:8px;font-size:10.5px;color:var(--soft)}
tr.child td{background:var(--stock2)}
tr.child.hidden{display:none}
.tabs{display:flex;gap:4px;border-bottom:2px solid var(--ink);margin-bottom:30px}
.tab{font-family:'IBM Plex Mono';font-size:11.5px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;text-decoration:none;color:var(--soft);padding:10px 16px;
  border:1px solid var(--rule);border-bottom:none;background:var(--stock2)}
.tab.on{color:var(--ink);background:var(--stock);border-color:var(--ink);
  position:relative;top:2px}
/* Supplier names are long slugs, so the click chart needs a far wider label
   column than the date labels the day chart was built for. */
.bars.wide .bar{grid-template-columns:minmax(0,330px) 1fr 60px}
.bars.wide .bar span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bars.wide .bar span em{font-style:normal;color:var(--bronze)}
/* The expanded half of a click row: everything that did not fit in six
   columns, which is most of what makes the click worth reading. */
.detail{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));
  gap:11px 26px;margin:0;padding:4px 0 10px}
.detail dt{font-size:9.5px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--soft);margin-bottom:3px}
.detail dd{margin:0;font-size:12.5px;word-break:break-word}
.detail dd.q{color:var(--soft)}
.hot{color:var(--bronze)}
/* td sets word-break:break-word so a long referrer URL cannot widen the
   table. A slug is not a URL and must not be broken mid-word: "lmgt4" wrapped
   to "lmgt" over "4" and read as a different wheel. */
td.slug{white-space:nowrap}
</style></head><body><div class="sheet">
<header><a class="logo" href="/">Wheel<em>Sourced</em></a>${body.showLogout ? '<a class="logout" href="/stats?logout=1">Log out</a>' : ""}</header>
${body.tabs || ""}
${body.html}
</div></body></html>`;
}

function loginForm(error) {
  return `<form class="login" method="POST">
    <h1 style="margin-top:0">Stats</h1>
    ${error ? `<div class="err">${esc(error)}</div>` : ""}
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Log in</button>
  </form>`;
}

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  if (url.searchParams.get("logout")) {
    return new Response(null, {
      status: 303,
      headers: { Location: "/stats", "Set-Cookie": `${COOKIE}=; Path=/; Max-Age=0` },
    });
  }

  if (!(await isAuthed(request, env))) {
    return new Response(page({ body: { html: loginForm(), showLogout: false } }),
      { headers: { "content-type": "text/html;charset=utf-8" } });
  }

  // Which tab, and only fetch what that tab needs: the two KV namespaces
  // behind Requests and the one behind Traffic have nothing in common, and
  // Traffic already costs a cursor-paginated scan (readDaily), so reading it
  // on every load of a page someone opened to see Requests was pure waste.
  const tab = url.searchParams.get("tab") === "traffic" ? "traffic" : "requests";

  const tabsHtml = `
    <div class="tabs">
      <a class="tab ${tab === "requests" ? "on" : ""}" href="/stats?tab=requests">Requests</a>
      <a class="tab ${tab === "traffic" ? "on" : ""}" href="/stats?tab=traffic">Traffic</a>
    </div>`;

  const bar = (label, n, max) => `
    <div class="bar"><span>${esc(label)}</span>
      <div class="track"><div class="fill" style="width:${max ? Math.round((n / max) * 100) : 0}%"></div></div>
      <div class="n"><b>${n}</b></div></div>`;

  let html;

  if (tab === "requests") {
    const [reqs, sugs] = await Promise.all([
      readAll(env.WHEEL_REQUESTS, "req:"),
      readAll(env.SUPPLIER_SUGGESTIONS, "sug:"),
    ]);

    const reqRows = reqs.map((r) =>
      `<tr><td>${esc(r.name)}</td><td>${esc(r.date)}</td><td>${esc(r.email || "")}</td></tr>`
    ).join("");
    const sugRows = sugs.map((s) =>
      `<tr><td><a href="${esc(s.url)}" rel="nofollow noopener" target="_blank">${esc(s.url)}</a></td>` +
      `<td>${esc(s.wheel || "")}</td><td>${esc(s.date)}</td><td>${esc(s.email || "")}</td></tr>`
    ).join("");

    html = `
      <h1 style="margin-top:0">Wheel requests</h1>
      <table><thead><tr><th>Wheel name</th><th>Date</th><th>Email</th></tr></thead>
      <tbody>${reqRows || '<tr><td colspan="3" class="empty">No requests yet</td></tr>'}</tbody></table>

      <h1>Supplier suggestions</h1>
      <table><thead><tr><th>Listing</th><th>Wheel</th><th>Date</th><th>Email</th></tr></thead>
      <tbody>${sugRows || '<tr><td colspan="4" class="empty">No suggestions yet</td></tr>'}</tbody></table>
    `;

    return new Response(page({ body: { html, tabs: tabsHtml, showLogout: true } }),
      { headers: { "content-type": "text/html;charset=utf-8" } });
  }

  const traffic = await readTraffic(env.SITE_STATS, 30);

  let trafficHtml;
  if (!traffic) {
    trafficHtml = `<p class="empty">Visitor tracking is not configured (SITE_STATS not bound).</p>`;
  } else if (!traffic.feed.length) {
    trafficHtml = `<p class="empty">No visits recorded yet. Counting starts from the next page view.</p>`;
  } else {
    const maxU = Math.max(...traffic.daily.map(([, n]) => n), 1);
    // The feed is one row per view, so the same visitor loading three pages
    // in a row printed the same hash three times in a column meant to help
    // you spot repeat visitors, not bury them in their own noise. Group
    // consecutive same-visitor rows under one summary row (id + view count),
    // hide the individual views, and reveal them on click. Only ever
    // compares to the row directly above, so two separate visits by the same
    // person with anyone else's view between them still get their own group
    // -- "grouped" means "consecutive", not "this hash already appeared
    // somewhere above".
    // One timestamp formatter for both tables. Pinned to Pacific because the
    // person reading this page is in it; the stored value is UTC.
    const when = (v) => esc(new Date(v.ts).toLocaleString("en-US", {
      timeZone: "America/Los_Angeles", dateStyle: "short", timeStyle: "medium" }));

    const ref = (v) => v.ref ? `<a href="${esc(v.ref)}" rel="noopener nofollow">${esc(v.ref)}</a>` : "Direct";

    const rowsHtml = [];
    let i = 0, gid = 0;
    while (i < traffic.feed.length) {
      let j = i + 1;
      while (j < traffic.feed.length && traffic.feed[j].vid === traffic.feed[i].vid) j++;
      const group = traffic.feed.slice(i, j);
      const head = group[0];
      const rest = group.slice(1);
      const isGroup = rest.length > 0;
      const groupIsNew = group.some((v) => v.isNew);
      const g = `g${gid++}`;

      rowsHtml.push(`
      <tr class="${groupIsNew ? "new" : ""} ${isGroup ? "group-toggle" : ""}" ${isGroup ? `data-group="${g}"` : ""}>
        <td class="vid">${isGroup ? '<span class="arrow">&#9656;</span>' : ""}${esc(head.vid)}${isGroup ? `<span class="count">${group.length} views</span>` : ""}${groupIsNew ? '<span class="badge">New</span>' : ""}</td>
        <td>${esc(head.path)}</td>
        <td class="when dim">${when(head)}</td>
        <td class="dim">${ref(head)}</td>
        <td class="dim">${esc(head.country || "")}</td>
      </tr>`);

      for (const v of rest) {
        rowsHtml.push(`
      <tr class="child hidden ${v.isNew ? "new" : ""}" data-group="${g}">
        <td class="vid"></td>
        <td>${esc(v.path)}</td>
        <td class="when dim">${when(v)}</td>
        <td class="dim">${ref(v)}</td>
        <td class="dim">${esc(v.country || "")}</td>
      </tr>`);
      }
      i = j;
    }
    const feedRows = rowsHtml.join("");

    // ---- Open listing clicks -------------------------------------------
    //
    // The section that answers "who", not just "how many". A click on its own
    // is a tally mark; a click next to how long they had been reading, which
    // sort they had chosen, which row they took under it, whether they had the
    // spec sheet open, and what else they read that session is a prospect with
    // a shape. All of it comes from two places already paid for: the click
    // record itself, and the pageview feed above joined on the visitor id.
    const C = traffic.clicks;

    // Seconds to something readable. Not a duration library: the only two
    // cases are "under a minute" and "over one".
    const dur = (n) => n == null ? null
      : n < 60 ? `${n}s` : `${Math.floor(n / 60)}m ${String(n % 60).padStart(2, "0")}s`;
    const place = (c) => [c.city, c.region, c.country].filter(Boolean).join(", ") || null;
    // Sort keys as the reader saw them, not as the <select> stores them.
    const SORTS = { vetting: "best vetted", price: "cheapest", rating: "store rating",
                    age: "store age", reorder: "reorder rate" };
    const dim = (v) => v ? esc(v) : '<span style="color:var(--soft)">not recorded</span>';

    let clicksHtml;
    if (!C.total) {
      clicksHtml = `
      <h1>Open listing clicks</h1>
      <p class="empty">No listing clicks recorded yet. Counting starts from the
        next click on an Open listing button.</p>`;
    } else {
      const maxC = Math.max(...C.bySupplier.map(([, n]) => n), 1);
      const supplierBars = C.bySupplier.slice(0, 12).map(([pair, n]) => {
        const [wheel, supplier] = pair.split("/");
        return `
        <div class="bar"><span title="${esc(pair)}"><em>${esc(wheel)}</em> ${esc(supplier)}</span>
          <div class="track"><div class="fill" style="width:${Math.round((n / maxC) * 100)}%"></div></div>
          <div class="n"><b>${n}</b></div></div>`;
      }).join("");

      const clickRows = C.rows.map((c, i) => {
        const who = c.vid ? traffic.visitors.get(c.vid) : null;
        const g = `c${i}`;
        // A visitor is called New off the pageview feed rather than off the
        // click, because the click endpoint deliberately does not do the KV
        // read that would answer it: _middleware.js already did, one request
        // earlier, for this same person.
        const isNew = who?.isNew;
        // The two signals worth surfacing without expanding the row. Opening
        // the spec sheet is the one that says the vetting was actually read,
        // which is the entire premise of the site, so it gets the accent.
        const signal = [
          c.opened ? '<span class="hot">read specs</span>' : null,
          dur(c.dwell) ? `${esc(dur(c.dwell))} on page` : null,
        ].filter(Boolean).join(", ") || "not recorded";

        return `
      <tr class="group-toggle ${isNew ? "new" : ""}" data-group="${g}">
        <td class="vid"><span class="arrow">&#9656;</span>${c.vid ? esc(c.vid) : "unknown"}${
          isNew ? '<span class="badge">New</span>' : ""}</td>
        <td>${esc(c.supplier)}</td>
        <td class="dim slug">${esc(c.wheel)}</td>
        <td class="when dim">${when(c)}</td>
        <td class="dim">${signal}</td>
        <td class="dim">${dim(place(c))}</td>
      </tr>
      <tr class="child hidden" data-group="${g}">
        <td colspan="6">
          <dl class="detail">
            <div><dt>Row taken</dt><dd>${
              c.rank ? `#${c.rank} of the list` : '<span class="q">not recorded</span>'}${
              c.sort ? `, sorted by ${esc(SORTS[c.sort] || c.sort)}` : ""}</dd></div>
            <div><dt>Spec sheet</dt><dd>${
              c.opened ? "expanded before clicking" : '<span class="q">not opened</span>'}</dd></div>
            <div><dt>Time on page</dt><dd>${dim(dur(c.dwell))}</dd></div>
            <div><dt>Device</dt><dd>${dim(c.device)}</dd></div>
            <div><dt>Network</dt><dd>${dim(c.network)}</dd></div>
            <div><dt>Arrived from</dt><dd>${
              who?.ref ? `<a href="${esc(who.ref)}" rel="noopener nofollow">${esc(who.ref)}</a>`
                       : '<span class="q">Direct, or outside the feed window</span>'}</dd></div>
            <div style="grid-column:1/-1"><dt>Pages read this session</dt><dd>${
              who?.paths?.length
                ? `${who.views} view${who.views === 1 ? "" : "s"}: ${esc(who.paths.join(" \u2192 "))}`
                : '<span class="q">Their pageviews have scrolled out of the feed window</span>'}</dd></div>
          </dl>
        </td>
      </tr>`;
      }).join("");

      clicksHtml = `
      <h1>Open listing clicks</h1>
      <dl class="tiles">
        <div class="tile key"><dt>Clicks today</dt><dd>${C.today}</dd></div>
        <div class="tile key"><dt>Clicks <small>(90d)</small></dt><dd>${C.total}</dd></div>
        <div class="tile"><dt>Visitors who clicked <small>(last ${C.rows.length})</small></dt><dd>${C.people}</dd></div>
        <div class="tile"><dt>Suppliers clicked</dt><dd>${C.bySupplier.length}</dd></div>
      </dl>
      <p style="font-size:11.5px;color:var(--soft);margin:0 0 22px">
        A click is one press of an Open listing button, counted by the page
        rather than by a redirect, so the button still goes straight to the
        listing it names. Clicks from readers running no JavaScript are not
        counted. <b style="color:var(--bronze)">read specs</b> means the
        supplier's spec sheet was expanded at the moment they clicked.</p>

      <h1>Clicks by supplier</h1>
      <div class="bars wide">${supplierBars}</div>

      <h1>Recent clicks</h1>
      <p style="font-size:11.5px;color:var(--soft);margin:-6px 0 14px">
        Click a row for what the visitor did before taking the link.</p>
      <table><thead><tr><th>Visitor ID</th><th>Supplier</th><th>Wheel</th>
        <th>Clicked at</th><th>Engagement</th><th>Where</th></tr></thead>
      <tbody>${clickRows}</tbody></table>`;
    }

    trafficHtml = `
      <dl class="tiles">
        <div class="tile key"><dt>Unique visitors today</dt><dd>${traffic.uniquesToday}</dd></div>
        <div class="tile key"><dt>New visitors <small>(last ${traffic.feed.length} views)</small></dt><dd>${traffic.newPeople}</dd></div>
        <div class="tile"><dt>Distinct visitors <small>(same window)</small></dt><dd>${traffic.people}</dd></div>
        <div class="tile"><dt>Visitor-days / 30d</dt><dd>${traffic.uniques30}</dd></div>
      </dl>
      <p style="font-size:11.5px;color:var(--soft);margin:0 0 22px">
        A visitor is one browser, identified by a salted hash of IP and user agent;
        no IP is stored. <b style="color:var(--bronze)">New</b> means never seen
        before, not just new today. Visitor-days counts each visitor once per day,
        so someone who came back three times is three.</p>

      <h1>Unique visitors by day</h1>
      <div class="bars">${traffic.daily.map(([day, n]) => bar(day, n, maxU)).join("")}</div>

      ${clicksHtml}

      <h1>Recent page views</h1>
      <table id="feed"><thead><tr><th>Visitor ID</th><th>Path</th><th>Visited at</th><th>Referrer</th><th>Country</th></tr></thead>
      <tbody>${feedRows}</tbody></table>
      <script>
      // Delegated on document rather than on #feed, because there are two
      // expandable tables now: grouped pageviews and the click rows above.
      // Group ids are unique per table (g0.. and c0..) so one handler cannot
      // open the wrong table's children.
      document.addEventListener('click', (e) => {
        const tr = e.target.closest('tr.group-toggle');
        if (!tr) return;
        const g = tr.dataset.group;
        const open = tr.classList.toggle('open');
        document.querySelectorAll('tr.child[data-group="' + g + '"]').forEach((r) => {
          r.classList.toggle('hidden', !open);
        });
      });
      </script>
    `;
  }

  html = `
    <h1 style="margin-top:0">Traffic</h1>
    ${trafficHtml}
  `;

  return new Response(page({ body: { html, tabs: tabsHtml, showLogout: true } }),
    { headers: { "content-type": "text/html;charset=utf-8" } });
}

export async function onRequestPost({ request, env }) {
  if (!env.ADMIN_KEY) {
    return new Response(page({ body: { html: loginForm("Not configured."), showLogout: false } }),
      { status: 500, headers: { "content-type": "text/html;charset=utf-8" } });
  }

  const form = await request.formData();
  const password = String(form.get("password") || "");

  if (!safeEqual(password, env.ADMIN_KEY)) {
    return new Response(page({ body: { html: loginForm("Wrong password."), showLogout: false } }),
      { status: 401, headers: { "content-type": "text/html;charset=utf-8" } });
  }

  const token = await hmac(env.ADMIN_KEY, SESSION_STRING);
  return new Response(null, {
    status: 303,
    headers: {
      Location: "/stats",
      "Set-Cookie": `${COOKIE}=${token}; Path=/; Max-Age=${MAX_AGE}; HttpOnly; Secure; SameSite=Lax`,
    },
  });
}
