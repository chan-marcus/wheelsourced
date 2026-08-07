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
  await env.WHEEL_REQUESTS.put(key, JSON.stringify(entry));

  return json({ ok: true }, 200);
}

// A GET here is someone poking at the endpoint, not a browser doing anything
// useful. Say so rather than falling through to the SPA 404.
export const onRequestGet = () =>
  json({ ok: false, error: "POST a wheel request to this endpoint." }, 405);
