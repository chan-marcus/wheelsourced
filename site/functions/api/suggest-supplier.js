// Cloudflare Pages Function for the "suggest a supplier" form at the foot of
// each sourcing sheet. Sibling of request-wheel.js and deliberately the same
// shape: same origin as the site, appends to KV, and is not a general backend.
// The sourcing dataset stays where it belongs, in YAML in git.
//
// Setup: bind the same KV namespace as SUPPLIER_SUGGESTIONS on the Pages
// project. Without the binding this returns 503 rather than accepting a
// submission it cannot store, because a form that silently drops suggestions
// is worse than one that admits it is down.

const MAX_URL = 600;
const MAX_EMAIL = 254;
const MAX_WHEEL = 80;

// Discovery is alibaba.com and s.1688.com, so a listing from anywhere else is
// not something the workflow can act on. This endpoint is public and
// unauthenticated, and an open "post us any link" box is a spam target, so the
// host check is the filter. Keep in step with _listing_host_ok in app.py,
// which serves this same route in dev.
const HOSTS = ["alibaba.com", "1688.com"];

const json = (body, status) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

function listingHostOk(raw) {
  let u;
  try {
    u = new URL(raw);
  } catch {
    return false;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return false;
  const host = u.hostname.toLowerCase();
  // Match the registrable domain rather than a substring: the hostname
  // "alibaba.com.evil.tld" contains "alibaba.com" and is not Alibaba.
  return HOSTS.some((d) => host === d || host.endsWith("." + d));
}

export async function onRequestPost({ request, env }) {
  if (!env.SUPPLIER_SUGGESTIONS) {
    return json({ ok: false, error: "Suggestion storage is not configured." }, 503);
  }

  let data;
  try {
    data = await request.json();
  } catch {
    return json({ ok: false, error: "Expected JSON." }, 400);
  }

  // Trim and cap before storing. Unauthenticated public endpoint, so treat
  // everything in the body as hostile length-wise.
  const url = String(data?.url ?? "").trim().slice(0, MAX_URL);
  if (!url) return json({ ok: false, error: "A listing URL is required." }, 400);
  if (!listingHostOk(url)) {
    return json(
      { ok: false, error: "Only alibaba.com and 1688.com listings can be vetted." },
      400,
    );
  }

  const wheel = String(data?.wheel ?? "").trim().slice(0, MAX_WHEEL) || null;

  const rawEmail = String(data?.email ?? "").trim().slice(0, MAX_EMAIL);
  // Only keep something that actually looks like an address. A junk value is
  // worse than none: it reads as a contactable lead and is not one.
  const email = /^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(rawEmail) ? rawEmail : null;

  const entry = {
    url,
    ...(wheel ? { wheel } : {}),
    date: new Date().toISOString().slice(0, 10),
    ...(email ? { email } : {}),
  };

  // One key per submission. A read-modify-write of a single JSON array would
  // lose entries whenever two people submit at once, since KV has no
  // compare-and-set. List them with the "sug:" prefix to read them back.
  const key = `sug:${Date.now()}:${crypto.randomUUID()}`;

  // The write is the whole endpoint, so a failed write has to be reported as
  // one. Left unguarded, a KV error escaped the handler and Pages turned it
  // into a bare 500: the visitor read "Server returned 500", had no way to
  // tell whether the fault was theirs or ours, and the submission existed
  // nowhere. The realistic cause is the free tier's 1,000 writes a day, which
  // is an account-wide budget shared with the visitor counter in
  // _middleware.js, so it runs out exactly when traffic is heaviest and a
  // lead is worth the most.
  //
  // Same reasoning as the 503 above, one failure mode over: a form that
  // cannot store a submission has to say so rather than accept it into
  // nothing. The message says nothing was recorded because that is what makes
  // retrying the right move instead of a risk of sending twice.
  //
  // console.error is the only trace a dropped submission leaves. Without it
  // the failure is invisible from this end and reads identically to nobody
  // having submitted at all.
  try {
    await env.SUPPLIER_SUGGESTIONS.put(key, JSON.stringify(entry));
  } catch (e) {
    console.error("SUPPLIER_SUGGESTIONS put failed", key, e);
    return json({
      ok: false,
      error: "Could not save that right now. Nothing was recorded, so please try again later.",
    }, 503);
  }

  return json({ ok: true }, 200);
}

// A GET here is someone poking at the endpoint, not a browser doing anything
// useful. Say so rather than falling through to the SPA 404.
export const onRequestGet = () =>
  json({ ok: false, error: "POST a supplier suggestion to this endpoint." }, 405);
