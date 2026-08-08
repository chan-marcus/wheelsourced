#!/usr/bin/env python3
"""
app.py - local web GUI for the wheel sourcing pipeline.

    python app.py
    open http://localhost:8420

Reads and writes the SAME YAML files as pipeline.py, so the CLI keeps working
and git still sees every change as a normal commit. This is a front end, not a
replacement.

What it is actually for: pasting supplier replies and watching the record fill
in. Everything else is convenience.
"""

import os, re, threading
from pathlib import Path
from datetime import date
import base64

import yaml
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


import pipeline as P     # reuse QUOTE_ONLY, _flags, _ask_model, REPLY_SCHEMA

# Anthropic() constructs happily without a key and only raises when you
# actually send something, so a keyless server starts, serves every page, and
# then fails the moment you paste a reply. Check at startup instead: the
# failure belongs where it can be read, not four hours later behind a button.
# This bites when the GUI is launched from somewhere that strips the variable
# out of the environment rather than from a normal shell.
if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
    raise SystemExit(
        "ANTHROPIC_API_KEY is not set in this process.\n"
        "Reply parsing would fail on every paste, so refusing to start.\n"
        "Run app.py from a shell that has the key (it is exported in ~/.zshrc)."
    )

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static wheel file serving
@app.get("/wheels/{wheel:path}")
async def get_wheel_file(wheel: str):
    wheel_path = Path("wheels") / wheel
    if wheel_path.exists() and wheel_path.is_file():
        return FileResponse(wheel_path)
    return JSONResponse({"error": "not found"}, status_code=404)

LOG: list[str] = []
BUSY = {"running": False}


def log(msg):
    LOG.append(msg)
    print(msg)
    del LOG[:-400]


# --------------------------------------------------------------------------
# data helpers
# --------------------------------------------------------------------------
def wheels():
    root = Path("wheels")
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []


def load(wheel):
    d = P.sdir(wheel)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.yaml")):
        r = yaml.safe_load(f.read_text())
        # Iterate the field definitions, not the stored keys, so questions
        # added since this record was pulled still register as gaps.
        r["_gaps"] = [k for k in P.ALL_FIELDS
                      if r.get("from_quote", {}).get(k) in (None, "")]
        out.append(r)
    # Cheapest quoted first. Anyone who has not quoted a price has nothing to
    # sort on, so they sink to the bottom ordered by how much they did answer.
    def key(r):
        p = r["from_quote"].get("price_per_wheel_1set")
        return (0, p, 0) if isinstance(p, (int, float)) else (1, 0, len(r["_gaps"]))
    return sorted(out, key=key)


def save(wheel, rec):
    rec = {k: v for k, v in rec.items() if not k.startswith("_")}
    (P.sdir(wheel) / f"{rec['id']}.yaml").write_text(
        yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------
@app.get("/api/state")
def state(wheel: str = ""):
    ws = wheels()
    w = wheel or (ws[0] if ws else "")
    # `fields` stays the ASKED set and nothing else, because the GUI counts
    # answered against it and that fraction is questions_answered. `extracted`
    # is everything else that gets stored, sent separately so it can be shown
    # and corrected without being counted as a question nobody answered.
    return {"wheels": ws, "wheel": w, "suppliers": load(w) if w else [],
            "fields": P.ALL_FIELDS, "extracted": P.EXTRACTED_DESC,
            "labels": P.FIELD_LABELS,
            "busy": BUSY["running"], "log": LOG[-40:]}


@app.post("/api/wheel")
async def new_wheel(req: Request):
    name = re.sub(r"[^a-z0-9-]+", "-", (await req.json())["name"].lower()).strip("-")
    for sub in ("suppliers", "outbox", "replies"):
        (Path("wheels") / name / sub).mkdir(parents=True, exist_ok=True)
    (Path("wheels") / name / "picks.txt").touch()
    return {"ok": True, "wheel": name}


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")


@app.post("/api/wheel-image")
async def upload_wheel_image(wheel: str = Form(...), file: UploadFile = File(...)):
    # Multipart, not base64 in JSON. Hand-rolling base64 in the browser meant a
    # byte-at-a-time String.fromCharCode loop over the whole file, which is both
    # slow and easy to get subtly wrong on binary data. The browser already
    # knows how to send a file.
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_EXTS:
        return JSONResponse(
            {"ok": False, "error": f"unsupported file type {ext or '(none)'}"}, status_code=400)

    contents = await file.read()
    if not contents:
        return JSONResponse({"ok": False, "error": "empty file"}, status_code=400)

    wheel_dir = Path("wheels") / wheel
    wheel_dir.mkdir(parents=True, exist_ok=True)

    # One image per wheel. Without this an old image.png outlives a new
    # image.jpg and the site keeps rendering the stale one, because the
    # extension lookup is ordered rather than newest-wins.
    for stale in wheel_dir.glob("image.*"):
        if stale.suffix.lower() != ext:
            stale.unlink()

    (wheel_dir / f"image{ext}").write_bytes(contents)
    return {"ok": True, "path": f"wheels/{wheel}/image{ext}", "bytes": len(contents)}


@app.get("/api/picks")
def get_picks(wheel: str):
    f = P.wdir(wheel) / "picks.txt"
    return {"text": f.read_text() if f.exists() else ""}


@app.post("/api/picks")
async def set_picks(req: Request):
    b = await req.json()
    (P.wdir(b["wheel"]) / "picks.txt").write_text(b["text"])
    n = len([l for l in b["text"].split() if l.startswith("http")])
    return {"ok": True, "count": n}


def _bg(fn, *a):
    def run():
        BUSY["running"] = True
        try:
            fn(*a)
        except Exception as e:
            log(f"ERROR: {e}")
        finally:
            BUSY["running"] = False
            log("done")
    threading.Thread(target=run, daemon=True).start()


@app.post("/api/pull")
async def do_pull(req: Request):
    if BUSY["running"]:
        return {"ok": False, "error": "already running"}
    w = (await req.json())["wheel"]
    log(f"pulling listings for {w}, a browser window will open")
    _bg(P.pull, w)
    return {"ok": True}


@app.post("/api/rfq")
async def do_rfq(req: Request):
    b = await req.json()
    P.ask(b["wheel"], b.get("size", "18x9.5J"), b.get("offset", "ET+22"),
          b.get("pcd", "5x114.3"))
    out = {}
    for f in (P.wdir(b["wheel"]) / "outbox").glob("*.txt"):
        out[f.stem] = f.read_text()
    return {"ok": True, "rfqs": out}


@app.post("/api/reply")
async def do_reply(req: Request):
    """Paste a raw reply. Saved to disk first, then parsed."""
    b = await req.json()
    wheel, sid = b["wheel"], b["id"]
    when = b.get("date") or date.today().isoformat()

    rep = P.wdir(wheel) / "replies"
    rep.mkdir(parents=True, exist_ok=True)
    # Several replies can land on the same day. Never overwrite an earlier one.
    stem, n = f"{sid}__{when}", 1
    path = rep / f"{stem}.txt"
    while path.exists():
        n += 1
        path = rep / f"{stem}-{n}.txt"
    path.write_text(b["text"], encoding="utf-8")

    rec = yaml.safe_load((P.sdir(wheel) / f"{sid}.yaml").read_text())
    for k in P.STORED_FIELDS:
        rec["from_quote"].setdefault(k, None)
    # The raw reply is already on disk above, so a failure here loses nothing
    # and is safe to retry. Report it as JSON: an uncaught exception becomes a
    # bare 500 that the browser cannot read, which is how a parse failure
    # turned into a button stuck on "Parsing..." rather than an error message.
    try:
        got = P._ask_model(b["text"], P.REPLY_SCHEMA)
    except Exception as e:
        log(f"{sid}: parse FAILED, reply saved to {path.name} -- {type(e).__name__}: {e}")
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502)

    # A price for the wrong construction never enters the record. Same guard
    # ingest uses, called from the shared helper so the GUI and the CLI cannot
    # drift apart on which quotes count.
    rejected = P.take_quote(got, rec)
    if rejected:
        log(f"{sid}: {rejected} quote set aside, not a {P.QUOTE_CONSTRUCTION} price")

    new_this_round, changed = 0, []
    for k in P.STORED_FIELDS:
        v = got.get(k)
        if v in (None, ""):
            continue
        prev = rec["from_quote"].get(k)

        # The set-valued fields accumulate and are never a revision: learning
        # a supplier also does 3-piece, or also holds VIA, is new information
        # rather than a contradiction. Recording it as one put "changed answer
        # between rounds" on Hantuo for answering what we had just asked, so
        # they return before the revision tracking below ever sees them.
        if k in P.SET_FIELDS:
            if k == "certs_claimed":
                dropped = P.unasserted_certs(v, b["text"])
                if dropped:
                    log(f"{sid}: reply does not assert {', '.join(dropped)}, not stored")
            if prev in (None, "") and k in P.ALL_FIELDS:
                new_this_round += 1
            rec["from_quote"][k] = P.merge_field(k, prev, v, b["text"])
            continue

        if prev in (None, ""):
            if k in P.ALL_FIELDS:
                new_this_round += 1
        elif str(prev) != str(v) and k in P.REVISION_FIELDS:
            # They said something different the second time. Keep the newer
            # value but record the change: a price that moves between rounds
            # is worth knowing about. Restricted to numbers and booleans,
            # because a reworded prose summary is not a changed answer.
            changed.append(f"{k}: {prev} -> {v}")
        rec["from_quote"][k] = v

    # Auto-confirm forging if they described a forging process and didn't
    # explicitly say "not forged". "Made with forged blanks" = forged.
    if got.get("forging_process") and rec["from_quote"].get("forged_confirmed") is None:
        fp = str(got.get("forging_process", "")).lower()
        if not any(x in fp for x in ("flow form", "flow-form", "rotary forg", "spun", "spinning", "not forged", "cast")):
            if any(x in fp for x in ("forg", "press", "blank", "billet")):
                rec["from_quote"]["forged_confirmed"] = True

    # Cumulative, not per-reply. This is what the GUI shows.
    total_filled = sum(1 for k in P.ALL_FIELDS if rec["from_quote"].get(k) not in (None, ""))

    m = rec["meta"]
    m.setdefault("first_reply", when)
    m["reply_received"] = when
    m["reply_count"] = (m.get("reply_count") or 0) + 1
    m["questions_answered"] = f"{total_filled}/{len(P.ALL_FIELDS)}"
    if got.get("notes"):
        m.setdefault("reply_notes", []).append(f"{when}: {got['notes']}")
    if got.get("catalog_offered"):
        m["catalog_offered"] = True
    if got.get("factory_address"):
        m["factory_address"] = got["factory_address"]
    if changed:
        m.setdefault("revisions", []).extend(f"{when} {c}" for c in changed)

    # revisions is written above, and _flags() reads it, so the
    # "changed answer between rounds" flag now survives a recompute.
    rec["flags"] = P._flags(rec)
    save(wheel, rec)
    log(f"{sid}: reply {m['reply_count']}, +{new_this_round} new, {total_filled}/{len(P.ALL_FIELDS)} total"
        + (f", revised {len(changed)}" if changed else ""))
    return {"ok": True, "record": rec}


@app.post("/api/field")
async def edit_field(req: Request):
    """Manual override. You read the reply, the model missed something."""
    b = await req.json()
    rec = yaml.safe_load((P.sdir(b["wheel"]) / f"{b['id']}.yaml").read_text())
    v = b["value"]
    if v == "":
        v = None
    # The set-valued fields are stored as lists, and every box in this GUI is
    # a text box. Without this a corrected piece_count is saved as the string
    # "monoblock, 2-piece" where the rest of the code expects a list, and a
    # typed certification skips the whitelist that keeps sales lines out.
    elif b["key"] in P.SET_FIELDS:
        v = P.parse_set_field(b["key"], v)
    elif re.fullmatch(r"-?\d+(\.\d+)?", str(v)):
        v = float(v) if "." in str(v) else int(v)
    elif str(v).lower() in ("true", "false"):
        v = str(v).lower() == "true"
    rec["from_quote"][b["key"]] = v
    rec["flags"] = P._flags(rec)
    save(b["wheel"], rec)
    return {"ok": True, "record": rec}


# Meta written by the listing pull and by ask(), not by reading a reply. A
# reset throws away everything a supplier told us, so anything NOT in here goes.
# Kept as a keep-list rather than a drop-list on purpose: parse-derived keys get
# added over time and a drop-list silently misses the new ones, which is exactly
# how reply_count and questions_answered survived the first version of this.
RESET_KEEPS_META = ("listing_seen", "ref_spec", "rfq_sent", "rfq_round", "rfq_first_sent")


@app.post("/api/reset-supplier/{supplier_id:path}")
async def reset_supplier(supplier_id: str, req: Request):
    """Clear everything the replies told us, so the archive can be re-parsed.

    The raw replies in replies/ are never touched. This only drops what the
    model extracted from them, which is what makes a re-parse meaningful.
    """
    # The GUI knows which wheel it is looking at, so trust it. Scanning every
    # wheel for a matching filename resets the wrong record when the same
    # factory is tracked under two wheels, which is normal here.
    try:
        wheel = (await req.json()).get("wheel")
    except Exception:
        wheel = None

    roots = [Path("wheels") / wheel] if wheel else \
            [d for d in Path("wheels").iterdir() if d.is_dir()]

    for wheel_dir in roots:
        sup_file = wheel_dir / "suppliers" / f"{supplier_id}.yaml"
        if not sup_file.exists():
            continue
        rec = yaml.safe_load(sup_file.read_text())

        # Every quoted field back to null, including the extracted-only ones.
        rec["from_quote"] = {k: None for k in P.STORED_FIELDS}
        # Drop the parse cache so ingest re-reads the archive instead of
        # skipping it on an unchanged content hash.
        meta = {k: v for k, v in (rec.get("meta") or {}).items() if k in RESET_KEEPS_META}
        meta["parsed_replies"] = {}
        rec["meta"] = meta
        # Flags last, and only now: _flags() derives "changed answer between
        # rounds" from meta.revisions, so clearing revisions above is what stops
        # a blank record carrying an accusation about answers it no longer holds.
        rec["flags"] = P._flags(rec)

        sup_file.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        log(f"{supplier_id}: reset, {len(list((wheel_dir / 'replies').glob(supplier_id + '__*.txt')))} replies kept")
        # The GUI rebuilds the row from this, so returning it is not optional:
        # without it the caller reads r.record.from_quote off undefined and
        # reports a failure for a reset that actually succeeded.
        return {"ok": True, "record": rec}

    return JSONResponse({"ok": False, "error": f"supplier not found: {supplier_id}"}, status_code=404)


# --------------------------------------------------------------------------
# ui
# --------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WheelSourced - Sourcing</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--stock:#E2E5DE;--stock2:#D7DBD3;--ink:#1B211C;--soft:#5A6259;--rule:#A9AFA5;--chop:#B0301F;--bronze:#8A6A2F}
*{box-sizing:border-box}
body{margin:0;background:var(--stock);color:var(--ink);font:15px/1.5 'IBM Plex Sans',system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 90px}
header{border-bottom:2px solid var(--ink);padding:22px 0 12px;margin-bottom:26px;display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap}
h1{font-family:'Barlow Condensed';font-size:40px;line-height:.9;text-transform:uppercase;margin:0;letter-spacing:.01em}
.mono{font-family:'IBM Plex Mono',monospace}
.lbl{font-family:'IBM Plex Mono';font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--soft);display:block;margin-bottom:6px}
select,input,textarea{font-family:'IBM Plex Mono',monospace;font-size:13px;background:transparent;border:1px solid var(--rule);color:var(--ink);padding:8px 10px;width:100%}
textarea{resize:vertical;line-height:1.6}
button{font-family:'IBM Plex Mono';font-size:11.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;padding:9px 14px;border:1px solid var(--ink);background:var(--ink);color:var(--stock);cursor:pointer}
button.ghost{background:transparent;color:var(--ink)}
button:disabled{opacity:.4;cursor:not-allowed}
button:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid var(--bronze);outline-offset:2px}
.band{font-family:'IBM Plex Mono';font-size:11px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;border-bottom:1px solid var(--ink);padding-bottom:6px;margin:38px 0 16px;display:flex;justify-content:space-between;align-items:baseline}
.band span{font-weight:400;color:var(--soft);letter-spacing:.08em}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.row>div{flex:1;min-width:130px}
.sup{border:1px solid var(--rule);background:var(--stock2);padding:14px;margin-bottom:10px}
.sup.open{border:2px solid var(--ink)}
.sup-h{display:grid;grid-template-columns:1fr auto;gap:12px 18px;align-items:start;cursor:pointer}
.sup-p{text-align:right;white-space:nowrap;font-family:'IBM Plex Mono';font-size:12px;color:var(--soft);align-self:start}
.sup-p b{color:var(--ink)}
.sup-n{font-family:'Barlow Condensed';font-size:23px;text-transform:uppercase;line-height:1}
.sup-m{font-family:'IBM Plex Mono';font-size:11px;color:var(--soft);margin-top:4px}
.plat{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:9px;
  font-family:'IBM Plex Mono';font-size:11.5px;color:var(--ink)}
.plat span{white-space:nowrap}
.plat i{font-style:normal;color:var(--soft);font-size:9.5px;letter-spacing:.12em;
  text-transform:uppercase;margin-right:5px}
.pill{font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;border:1px solid var(--rule);padding:2px 6px;color:var(--soft);margin:0 3px 3px 0;display:inline-block}
.pill.bad{border-color:var(--chop);color:var(--chop)}
.pill.good{border-color:var(--bronze);color:var(--bronze)}
.body{display:none;margin-top:14px;padding-top:13px;border-top:1px solid var(--rule)}
.sup.open .body{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:10px 16px}
.f label{font-family:'IBM Plex Mono';font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--soft);display:block;margin-bottom:3px}
.f input{padding:6px 8px;font-size:12.5px}
.f input.empty{border-color:var(--chop);border-style:dashed}
#log{font-family:'IBM Plex Mono';font-size:11px;color:var(--soft);background:var(--stock2);border:1px solid var(--rule);padding:10px;height:120px;overflow:auto;white-space:pre-wrap;margin-top:10px}
.hint{font-family:'IBM Plex Mono';font-size:11px;color:var(--soft);margin-top:8px}

/* Native confirm() and alert() are OS chrome: wrong typeface, wrong palette,
   and blocking. They are also suppressed outright in some embedded browsers,
   where confirm() silently returns false and the button just does nothing. */
.scrim{position:fixed;inset:0;background:rgba(27,33,28,.55);display:flex;
  align-items:flex-start;justify-content:center;padding:14vh 18px;z-index:60}
.scrim[hidden]{display:none}
.dlg{background:var(--stock);border:2px solid var(--ink);max-width:460px;width:100%;padding:20px}
.dlg h3{font-family:'Barlow Condensed';font-weight:600;font-size:23px;text-transform:uppercase;
  margin:0 0 10px;letter-spacing:.01em}
.dlg p{font-family:'IBM Plex Mono';font-size:12.5px;line-height:1.65;color:var(--soft);margin:0 0 6px}
.dlg p b{color:var(--ink);font-weight:500}
.dlg .acts{display:flex;gap:9px;margin-top:18px}

#toasts{position:fixed;right:18px;bottom:18px;z-index:70;display:flex;
  flex-direction:column;gap:8px;align-items:flex-end}
.toast{font-family:'IBM Plex Mono';font-size:12px;line-height:1.5;max-width:380px;
  background:var(--ink);color:var(--stock);border:1px solid var(--ink);padding:11px 14px;
  animation:tin 180ms cubic-bezier(.4,0,.2,1)}
.toast.bad{background:var(--chop);border-color:var(--chop)}
.toast i{font-style:normal;display:block;font-size:9.5px;letter-spacing:.14em;
  text-transform:uppercase;opacity:.72;margin-bottom:3px}
@keyframes tin{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){.toast{animation:none}}
</style></head><body><div class="wrap">

<header>
  <h1>Wheel sourcing</h1>
  <div style="display:flex;gap:10px;align-items:flex-end">
    <div><span class="lbl">Wheel</span><select id="wheel" onchange="boot(this.value)"></select></div>
    <button class="ghost" onclick="newWheel()">New</button>
    <button class="ghost" onclick="window.location.href='http://localhost:4321'">View sourcing sheet</button>
  </div>
</header>

<div class="band">0 · Wheel image <span id="imagecount"></span></div>
<div class="row">
  <div style="flex:1;min-width:200px">
    <span class="lbl">Upload image for landing page</span>
    <input type="file" id="wheelImage" accept="image/*">
  </div>
  <button onclick="uploadImage()">Upload</button>
</div>
<div id="imagestatus" style="color:var(--soft);font-family:'IBM Plex Mono';font-size:11px;margin-top:8px"></div>

<div class="band">1 · Listings you picked <span id="pickcount"></span></div>
<textarea id="picks" rows="5" placeholder="One Alibaba listing URL per line. Find them by photo search first."></textarea>
<div class="row" style="margin-top:10px">
  <button class="ghost" onclick="savePicks()">Save list</button>
  <button id="pullbtn" onclick="pull()">Pull listing data</button>
</div>
<div id="log"></div>

<div class="band">2 · Quote requests <span>only asks what the listings did not answer</span></div>
<div class="row">
  <div><span class="lbl">Size</span><input id="size" value="18x9.5J"></div>
  <div><span class="lbl">Offset</span><input id="offset" value="ET+22"></div>
  <div><span class="lbl">Bolt pattern</span><input id="pcd" value="5x114.3"></div>
  <button onclick="genRfq()">Generate</button>
</div>
<div class="hint">Generated messages appear on each supplier below, with a copy button.</div>

<div class="band">3 · Suppliers <span id="supcount"></span></div>
<div id="sups"></div>

</div>

<div class="scrim" id="scrim" hidden>
  <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlgtitle">
    <h3 id="dlgtitle"></h3>
    <div id="dlgbody"></div>
    <div class="acts">
      <button id="dlgok"></button>
      <button class="ghost" id="dlgno">Cancel</button>
    </div>
  </div>
</div>
<div id="toasts" aria-live="polite"></div>

<script>
let S={wheel:"",suppliers:[],fields:{},extracted:{},labels:{},rfqs:{}}, OPEN=null;

async function boot(w){
  const r = await (await fetch("/api/state?wheel="+encodeURIComponent(w||""))).json();
  S.wheel=r.wheel; S.suppliers=r.suppliers; S.fields=r.fields; S.extracted=r.extracted||{};
  S.labels=r.labels||{};
  const sel=document.getElementById("wheel");
  sel.innerHTML=r.wheels.map(x=>`<option ${x===r.wheel?"selected":""}>${x}</option>`).join("");
  if(r.wheel){
    const p=await (await fetch("/api/picks?wheel="+r.wheel)).json();
    document.getElementById("picks").value=p.text;
    document.getElementById("pickcount").textContent=
      p.text.split(/\s+/).filter(l=>l.startsWith("http")).length+" urls";
  }
  document.getElementById("pullbtn").disabled=r.busy;
  document.getElementById("log").textContent=r.log.join("\n");
  render();
}
async function newWheel(){
  const n=prompt("Wheel slug, e.g. mesh-futura"); if(!n) return;
  await post("/api/wheel",{name:n}); boot(n.toLowerCase().replace(/[^a-z0-9-]+/g,"-"));
}
const post=(u,b)=>fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}).then(r=>r.json());

// Resolves true/false. Native confirm() blocks the event loop and is disabled
// outright in some embedded browsers, where it returns false and the button
// reads as broken.
function ask({title,body,ok="Confirm"}){
  const scrim=document.getElementById("scrim");
  document.getElementById("dlgtitle").textContent=title;
  document.getElementById("dlgbody").innerHTML=body;
  const okb=document.getElementById("dlgok"), nob=document.getElementById("dlgno");
  okb.textContent=ok;
  scrim.hidden=false; okb.focus();
  return new Promise(res=>{
    const done=v=>{
      scrim.hidden=true;
      okb.onclick=nob.onclick=scrim.onclick=null;
      document.removeEventListener("keydown",key);
      res(v);
    };
    const key=e=>{ if(e.key==="Escape") done(false); };
    okb.onclick=()=>done(true);
    nob.onclick=()=>done(false);
    scrim.onclick=e=>{ if(e.target===scrim) done(false); };
    document.addEventListener("keydown",key);
  });
}

// Which questions are still unanswered, recomputed from a record the server
// just returned. Was written out three times with the same filter; a reset that
// returns a record has to agree with a parse that returns one.
const withGaps=rec=>({...rec,_gaps:Object.keys(S.fields).filter(k=>{
  const v=rec.from_quote[k]; return !v && v!==false;})});

function toast(msg,kind){
  const t=document.createElement("div");
  t.className="toast"+(kind==="bad"?" bad":"");
  t.innerHTML=`<i>${kind==="bad"?"Failed":"Done"}</i>`;
  t.appendChild(document.createTextNode(msg));
  document.getElementById("toasts").appendChild(t);
  // Errors stay put long enough to read and copy; confirmations do not.
  setTimeout(()=>t.remove(),kind==="bad"?9000:4000);
}

async function savePicks(){
  const r=await post("/api/picks",{wheel:S.wheel,text:document.getElementById("picks").value});
  document.getElementById("pickcount").textContent=r.count+" urls";
}
async function pull(){
  await savePicks();
  await post("/api/pull",{wheel:S.wheel});
  document.getElementById("pullbtn").disabled=true;
  const t=setInterval(async()=>{
    const r=await (await fetch("/api/state?wheel="+S.wheel)).json();
    document.getElementById("log").textContent=r.log.join("\n");
    document.getElementById("log").scrollTop=9e9;
    if(!r.busy){clearInterval(t);document.getElementById("pullbtn").disabled=false;boot(S.wheel);}
  },2000);
}
async function uploadImage(){
  const wheelInput = document.getElementById("wheelImage");
  const imagestatus = document.getElementById("imagestatus");

  if(!S.wheel){ imagestatus.textContent = "Select a wheel first"; return; }
  if(!wheelInput.files || !wheelInput.files.length){
    imagestatus.textContent = "Choose an image file first"; return;
  }

  const file = wheelInput.files[0];
  imagestatus.textContent = "Uploading " + file.name + "...";

  // Let the browser encode the file. FormData sends the bytes as-is.
  const body = new FormData();
  body.append("wheel", S.wheel);
  body.append("file", file);

  try {
    const r = await fetch("/api/wheel-image", {method:"POST", body}).then(r=>r.json());
    if(r.ok){
      const kb = Math.round(r.bytes/1024);
      imagestatus.textContent = "Saved " + r.path + " (" + kb + " kB). "
        + "Restart the Astro dev server to see it on the site.";
      wheelInput.value = "";
    } else {
      imagestatus.textContent = "Error: " + (r.error || "upload failed");
    }
  } catch(err) {
    imagestatus.textContent = "Error: " + err.message;
  }
}

async function genRfq(){
  const r=await post("/api/rfq",{wheel:S.wheel,size:size.value,offset:offset.value,pcd:pcd.value});
  S.rfqs=r.rfqs; render();
}

function render(){
  document.getElementById("supcount").textContent=S.suppliers.length+" tracked";
  document.getElementById("sups").innerHTML=S.suppliers.map(s=>{
    const L=s.from_listing||{}, Q=s.from_quote||{}, gaps=s._gaps||[];
    const answered=Object.keys(S.fields).length-gaps.length;
    const pills=[
      L.business_type?`<span class="pill ${/manufactur/i.test(L.business_type)?'good':'bad'}">${L.business_type}</span>`:"",
      L.years_on_platform?`<span class="pill">${L.years_on_platform} yrs</span>`:"",
      (Q.piece_count||L.construction)?`<span class="pill">${Q.piece_count||L.construction}</span>`:"",
      Q.forged_confirmed===true?`<span class="pill good">forged confirmed</span>`:"",
      Q.forged_confirmed===false?`<span class="pill bad">not forged</span>`:"",
      ...(s.flags||[]).map(f=>`<span class="pill bad">${f}</span>`)
    ].join("");

    // Listing-side metrics. These were being pulled and stored but never
    // rendered, so the GUI looked like the pull had done nothing.
    const plat=[
      ["rating", L.store_rating?`${L.store_rating}/5${L.review_count?` (${L.review_count})`:""}`:null],
      ["response", L.response_time],
      ["reorder", L.reorder_rate],
      ["on time", L.on_time_dispatch],
      ["sold", L.units_sold],
      ["MOQ", L.moq],
      ["transit", L.transit_time],
    ].filter(([,v])=>v!==null&&v!==undefined&&v!=="");
    const platRow = plat.length
      ? `<div class="plat">${plat.map(([k,v])=>
          `<span><i>${k}</i>${v}</span>`).join("")}</div>` : "";
    // A tag list has to come back as "DOT, VIA" rather than the "DOT,VIA" a
    // bare array stringifies to, or every edit reformats what it read.
    const show=v=>Array.isArray(v)?v.join(", "):v;
    // `asked` fields dash when blank, because blank means a question went
    // unanswered. The extracted ones were never asked, so a blank there is
    // not a gap and must not be drawn like one.
    const cell=(k,desc,asked)=>{
      const v=Q[k]; const empty=(v===null||v===undefined||v==="");
      return `<div class="f"><label title="${desc}">${S.labels[k]||k.replace(/_/g," ")}</label>
        <input class="${empty&&asked?'empty':''}" value="${empty?'':show(v)}"
          onchange="setField('${s.id}','${k}',this.value)"></div>`;
    };
    const fields=Object.entries(S.fields).map(([k,d])=>cell(k,d,true)).join("");
    const extracted=Object.entries(S.extracted).map(([k,d])=>cell(k,d,false)).join("");

    // The same landed cost the sheet headlines, computed the same way so the
    // two never disagree: prefer their quoted set price over 4x the single,
    // and stay null unless BOTH the set and the freight were quoted. Falling
    // back to a bare price here would show the vendor who withheld freight a
    // smaller number than the one who disclosed it.
    const n=v=>(typeof v==="number"&&isFinite(v))?v:null;
    const setPrice=n(Q.price_per_wheel_4set) ?? (n(Q.price_per_wheel_1set)?n(Q.price_per_wheel_1set)*4:null);
    const freight=n(Q.freight_cif_usd);
    const landed=(setPrice!==null&&freight!==null)?setPrice+freight:null;
    const rfq=S.rfqs[s.id];
    return `<div class="sup ${OPEN===s.id?'open':''}" id="s-${s.id}">
      <div class="sup-h" onclick="toggle('${s.id}')">
        <div><div class="sup-n">${s.id}</div>
          <div class="sup-m">${answered}/${Object.keys(S.fields).length} answered
            ${s.meta&&s.meta.reply_received?"· replied "+s.meta.reply_received:"· no reply yet"}</div>
          <div style="margin-top:7px">${pills}</div>
          ${platRow}</div>
        <div class="sup-p">
          ${landed!==null
            ? `<div><b>$${landed.toLocaleString()}</b></div>
               <span>est. landed / set of 4</span>`
            : Q.price_per_wheel_1set
              ? `<div><b>$${Q.price_per_wheel_1set}</b> per wheel</div>
                 <span>${Q.freight_cif_usd===null||Q.freight_cif_usd===undefined?"no freight quoted":"no set price"}</span>`
              : L.listed_price_low
                ? `<span>listed $${L.listed_price_low}</span>` : ""}
        </div>
      </div>
      <div class="body">
        ${rfq?`<span class="lbl">Message to send</span>
          <textarea rows="10" id="rfq-${s.id}" readonly>${rfq}</textarea>
          <div class="row" style="margin:8px 0 18px"><button class="ghost" onclick="copyRfq('${s.id}')">Copy message</button>
          <a href="${(s.listings&&s.listings[0])}" target="_blank"><button class="ghost">Open listing</button></a></div>`:""}
        <span class="lbl">Paste their reply</span>
        <textarea rows="7" id="rep-${s.id}" placeholder="Paste the raw reply here, unedited. Broken English is fine."></textarea>
        <div class="row" style="margin:8px 0 18px">
          <div style="max-width:170px"><span class="lbl">Date received</span>
            <input id="dt-${s.id}" value="${new Date().toISOString().slice(0,10)}"></div>
          <button data-send="${s.id}" onclick="sendReply('${s.id}', this)">Parse reply</button>
          <button class="ghost" onclick="resetSupplier('${s.id}', this)">Reset data</button>
        </div>
        <span class="lbl">Quoted data · dashed border means still unanswered</span>
        <div class="grid" style="margin-top:8px">${fields}${extracted}</div>
      </div></div>`;
  }).join("") || `<div class="hint">Nothing yet. Add listing URLs above and pull.</div>`;
}

function toggle(id){OPEN=OPEN===id?null:id;render();}
function copyRfq(id){const t=document.getElementById("rfq-"+id);t.select();
  navigator.clipboard.writeText(t.value).catch(()=>document.execCommand("copy"));}
async function sendReply(id, btn){
  const text=document.getElementById("rep-"+id).value.trim(); if(!text) return;
  // btn is passed in rather than read off the implicit global `event`, which
  // is undefined once this is called from anything but an inline handler.
  btn=btn||document.querySelector('[data-send="'+id+'"]');
  const label=btn?btn.textContent:"";
  if(btn){btn.textContent="Parsing...";btn.disabled=true;}
  try{
    const r=await post("/api/reply",{wheel:S.wheel,id,text,date:document.getElementById("dt-"+id).value});
    if(r&&r.ok&&r.record){
      S.suppliers=S.suppliers.map(s=>s.id===id?withGaps(r.record):s);
      render();
    }else{
      toast("Parse failed: "+((r&&r.error)||"the server did not return a record."),"bad");
    }
  }catch(e){
    // Without this the rejected await skipped render(), the button stayed on
    // "Parsing..." forever, and a request that failed in 20ms read as a hang.
    toast("Parse failed: "+e.message,"bad");
  }finally{
    if(btn&&btn.isConnected){btn.textContent=label;btn.disabled=false;}
  }
}
async function setField(id,key,value){
  const r=await post("/api/field",{wheel:S.wheel,id,key,value});
  if(r&&r.ok&&r.record){S.suppliers=S.suppliers.map(s=>s.id===id?withGaps(r.record):s);render();}
}
async function resetSupplier(id,btn){
  const ok=await ask({
    title:"Reset "+id.replace(/-/g," "),
    body:`<p>Clears every quoted field, the parse cache, and the reply history
      on this record. The raw replies stay on disk, so you can parse them again.</p>
      <p><b>Anything typed in by hand is lost too.</b></p>`,
    ok:"Reset data"});
  if(!ok) return;
  const label=btn.textContent;
  btn.textContent="Resetting...";btn.disabled=true;
  try{
    const r=await post("/api/reset-supplier/"+id,{wheel:S.wheel});
    // The record is what the row is rebuilt from. Guard it: an endpoint that
    // returns {ok:true} and nothing else used to throw here and report a
    // failure for a reset that had already written to disk.
    if(r&&r.ok&&r.record){
      S.suppliers=S.suppliers.map(s=>s.id===id?withGaps(r.record):s);
      render();
      toast(id+" cleared. Paste a reply, or run ingest, to fill it again.");
    }else{
      toast("Reset failed: "+((r&&r.error)||"the server did not return a record."),"bad");
    }
  }catch(e){
    toast("Reset failed: "+e.message,"bad");
  }finally{
    if(btn&&btn.isConnected){btn.textContent=label;btn.disabled=false;}
  }
}
boot("");
</script></body></html>"""


def requests_file():
    return Path("wheel_requests.json")


def load_requests():
    import json
    f = requests_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except:
            return []
    return []


def save_requests(reqs):
    import json
    requests_file().write_text(json.dumps(reqs, indent=2))


@app.post("/api/request-wheel")
async def request_wheel(req: Request):
    data = await req.json()
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"ok": False}, status_code=400)

    email = data.get("email", "").strip() if data.get("email") else None

    reqs = load_requests()
    entry = {"name": name, "date": str(date.today())}
    if email:
        entry["email"] = email
    reqs.append(entry)
    save_requests(reqs)
    return JSONResponse({"ok": True})


def suggestions_file():
    return Path("supplier_suggestions.json")


def load_suggestions():
    import json
    f = suggestions_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except:
            return []
    return []


def save_suggestions(sugs):
    import json
    suggestions_file().write_text(json.dumps(sugs, indent=2))


# Discovery is alibaba.com and s.1688.com, so a suggestion from anywhere else
# is not something this workflow can act on. The endpoint is public and
# unauthenticated, and an open "post us any link" box is a spam target, so the
# host check is the filter. Kept in step with the Pages Function that serves
# the same route in production.
SUGGEST_HOSTS = ("alibaba.com", "1688.com")
MAX_URL, MAX_EMAIL = 600, 254
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def _listing_host_ok(url):
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    # Match the registrable domain, not a substring: "alibaba.com.evil.tld"
    # contains the string and is not Alibaba.
    return any(host == d or host.endswith("." + d) for d in SUGGEST_HOSTS)


@app.post("/api/suggest-supplier")
async def suggest_supplier(req: Request):
    data = await req.json()
    url = str(data.get("url") or "").strip()[:MAX_URL]
    if not url:
        return JSONResponse({"ok": False, "error": "A listing URL is required."}, status_code=400)
    if not _listing_host_ok(url):
        return JSONResponse(
            {"ok": False, "error": "Only alibaba.com and 1688.com listings can be vetted."},
            status_code=400)

    raw_email = str(data.get("email") or "").strip()[:MAX_EMAIL]
    # A junk address is worse than none: it reads as a contactable lead and is
    # not one. Same rule as the wheel request endpoint.
    email = raw_email if _EMAIL_RE.match(raw_email) else None

    entry = {"url": url, "wheel": str(data.get("wheel") or "").strip()[:80] or None,
             "date": str(date.today())}
    if email:
        entry["email"] = email

    sugs = load_suggestions()
    # The same listing suggested twice is one listing to vet. Keep the first,
    # and take an alert address if the duplicate carried one and the original
    # did not.
    for s in sugs:
        if s.get("url") == url:
            if email and not s.get("email"):
                s["email"] = email
                save_suggestions(sugs)
            return JSONResponse({"ok": True, "duplicate": True})

    sugs.append(entry)
    save_suggestions(sugs)
    return JSONResponse({"ok": True})


@app.get("/requests", response_class=HTMLResponse)
def show_requests():
    from html import escape
    reqs = load_requests()
    rows = "".join(f"<tr><td>{r['name']}</td><td>{r['date']}</td><td>{r.get('email', '')}</td></tr>" for r in reqs)
    # Escaped, unlike the wheel rows above: these values are a URL and an
    # address straight off a public form, and this page renders them as HTML.
    sug_rows = "".join(
        f'<tr><td class="email"><a href="{escape(s["url"], quote=True)}" rel="nofollow noopener" '
        f'target="_blank">{escape(s["url"])}</a></td>'
        f'<td>{escape(s.get("wheel") or "")}</td><td>{escape(s["date"])}</td>'
        f'<td class="email">{escape(s.get("email", ""))}</td></tr>'
        for s in load_suggestions())
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Wheel Requests</title>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'IBM Plex Mono', monospace; background: #f5f5f0; color: #333; padding: 40px 20px; }}
    h1 {{ font-family: 'Barlow Condensed'; font-size: 36px; font-weight: 700; margin-bottom: 32px; text-transform: uppercase; }}
    table {{ width: 100%; max-width: 900px; border-collapse: collapse; }}
    th {{ text-align: left; padding: 12px; border-bottom: 2px solid #333; font-weight: 600; text-transform: uppercase; font-size: 12px; }}
    td {{ padding: 12px; border-bottom: 1px solid #ddd; }}
    tr:hover {{ background: #f0f0ea; }}
    .empty {{ color: #999; font-style: italic; padding: 32px 0; text-align: center; }}
    .email {{ font-size: 12px; word-break: break-all; }}
  </style>
</head>
<body>
  <h1>Wheel Requests</h1>
  <table>
    <thead>
      <tr>
        <th>Wheel Name</th>
        <th>Date Requested</th>
        <th>Email</th>
      </tr>
    </thead>
    <tbody>
      {rows or '<tr><td colspan="3" class="empty">No requests yet</td></tr>'}
    </tbody>
  </table>

  <h1 style="margin-top:56px;">Supplier suggestions</h1>
  <table>
    <thead>
      <tr>
        <th>Listing</th>
        <th>Wheel</th>
        <th>Date</th>
        <th>Email</th>
      </tr>
    </thead>
    <tbody>
      {sug_rows or '<tr><td colspan="4" class="empty">No suggestions yet</td></tr>'}
    </tbody>
  </table>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


if __name__ == "__main__":
    Path("wheels").mkdir(exist_ok=True)
    print("\n  http://localhost:8420\n")
    # Reload on save. The page is one big string in this file, so before this
    # a stale process served old HTML indefinitely and looked like a bug in
    # the code you had just fixed: the Finish field stayed on the form for
    # three hours after being deleted.
    #
    # "app:app" rather than the object, because reload re-imports the module
    # in a child process and cannot do that from an instance. The __main__
    # guard is what stops that child running this block again.
    #
    # Scoped to this directory and to the two files the GUI is actually built
    # from. Watching everything would mean .venv, and writing YAML into
    # wheels/ on every paste would restart the server mid-request. Excludes
    # need watchfiles installed; uvicorn silently ignores them otherwise and
    # warns, so if that warning ever appears, reinstall it.
    uvicorn.run("app:app", host="127.0.0.1", port=8420, log_level="warning",
                reload=True, reload_dirs=["."],
                reload_includes=["app.py", "pipeline.py"],
                reload_excludes=[".venv/*", "wheels/*", "site/*", ".git/*"])
