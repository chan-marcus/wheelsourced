// Cloudflare Pages Function. Same origin as the site, so the request form
// works on the live domain: the page used to post to http://localhost:8420,
// which is unreachable from a visitor's machine and blocked as mixed content
// from an https page anyway.
//
// Deliberately not a general backend. It appends to a KV list and nothing
// else. The sourcing dataset stays where it belongs, in YAML in git.
//
// Setup: create a KV namespace and bind it as WHEEL_REQUESTS on the Pages
// project. Without the binding this returns 503 rather than accepting a
// submission it cannot store, because a form that silently drops requests is
// worse than one that admits it is down.

const MAX_NAME = 120;
const MAX_EMAIL = 254;

const json = (body, status) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

export async function onRequestPost({ request, env }) {
  if (!env.WHEEL_REQUESTS) {
    return json({ ok: false, error: "Request storage is not configured." }, 503);
  }

  let data;
  try {
    data = await request.json();
  } catch {
    return json({ ok: false, error: "Expected JSON." }, 400);
  }

  // Trim and cap before storing. This is an unauthenticated public endpoint,
  // so treat everything in the body as hostile length-wise.
  const name = String(data?.name ?? "").trim().slice(0, MAX_NAME);
  if (!name) return json({ ok: false, error: "Wheel name is required." }, 400);

  const rawEmail = String(data?.email ?? "").trim().slice(0, MAX_EMAIL);
  // Only keep something that actually looks like an address. A junk value is
  // worse than none: it reads as a contactable lead and is not one.
  const email = /^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(rawEmail) ? rawEmail : null;

  const entry = {
    name,
    date: new Date().toISOString().slice(0, 10),
    ...(email ? { email } : {}),
  };

  // One key per submission. A read-modify-write of a single JSON array would
  // lose entries whenever two people submit at once, since KV has no
  // compare-and-set. List them with the "req:" prefix to read them back.
  const key = `req:${Date.now()}:${crypto.randomUUID()}`;

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
    await env.WHEEL_REQUESTS.put(key, JSON.stringify(entry));
  } catch (e) {
    console.error("WHEEL_REQUESTS put failed", key, e);
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
  json({ ok: false, error: "POST a wheel request to this endpoint." }, 405);
