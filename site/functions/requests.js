// Cloudflare Pages Function serving /requests: the admin view of what has
// come in through the two public forms. Password-gated, because the two KV
// stores it reads hold real people's email addresses, and the local
// equivalent (app.py's /requests) was only ever safe to leave open because
// nothing outside this machine could reach it.
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

function page({ body, title = "WheelSourced - Requests" }) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title>
<link rel="icon" type="image/png" href="/favicon-32.png" sizes="32x32">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--stock:#E2E5DE;--stock2:#D7DBD3;--ink:#1B211C;--soft:#5A6259;--rule:#A9AFA5;--chop:#B0301F;--bronze:#8A6A2F}
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
</style></head><body><div class="sheet">
<header><a class="logo" href="/">Wheel<em>Sourced</em></a>${body.showLogout ? '<a class="logout" href="/requests?logout=1">Log out</a>' : ""}</header>
${body.html}
</div></body></html>`;
}

function loginForm(error) {
  return `<form class="login" method="POST">
    <h1 style="margin-top:0">Requests</h1>
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
      headers: { Location: "/requests", "Set-Cookie": `${COOKIE}=; Path=/; Max-Age=0` },
    });
  }

  if (!(await isAuthed(request, env))) {
    return new Response(page({ body: { html: loginForm(), showLogout: false } }),
      { headers: { "content-type": "text/html;charset=utf-8" } });
  }

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

  const html = `
    <h1>Wheel requests</h1>
    <table><thead><tr><th>Wheel name</th><th>Date</th><th>Email</th></tr></thead>
    <tbody>${reqRows || '<tr><td colspan="3" class="empty">No requests yet</td></tr>'}</tbody></table>

    <h1>Supplier suggestions</h1>
    <table><thead><tr><th>Listing</th><th>Wheel</th><th>Date</th><th>Email</th></tr></thead>
    <tbody>${sugRows || '<tr><td colspan="4" class="empty">No suggestions yet</td></tr>'}</tbody></table>
  `;

  return new Response(page({ body: { html, showLogout: true } }),
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
      Location: "/requests",
      "Set-Cookie": `${COOKIE}=${token}; Path=/; Max-Age=${MAX_AGE}; HttpOnly; Secure; SameSite=Lax`,
    },
  });
}
