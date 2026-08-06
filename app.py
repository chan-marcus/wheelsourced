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

import re, threading
from pathlib import Path
from datetime import date

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

import pipeline as P     # reuse QUOTE_ONLY, _flags, _ask_model, REPLY_SCHEMA

app = FastAPI()
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
    return {"wheels": ws, "wheel": w, "suppliers": load(w) if w else [],
            "fields": P.ALL_FIELDS, "busy": BUSY["running"], "log": LOG[-40:]}


@app.post("/api/wheel")
async def new_wheel(req: Request):
    name = re.sub(r"[^a-z0-9-]+", "-", (await req.json())["name"].lower()).strip("-")
    for sub in ("suppliers", "outbox", "replies"):
        (Path("wheels") / name / sub).mkdir(parents=True, exist_ok=True)
    (Path("wheels") / name / "picks.txt").touch()
    return {"ok": True, "wheel": name}


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
          b.get("pcd", "5x114.3"), b.get("finish", "Bronze"))
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
    for k in P.ALL_FIELDS:
        rec["from_quote"].setdefault(k, None)
    got = P._ask_model(b["text"], P.REPLY_SCHEMA)

    new_this_round, changed = 0, []
    for k in P.ALL_FIELDS:
        v = got.get(k)
        if v in (None, ""):
            continue
        prev = rec["from_quote"].get(k)
        if prev in (None, ""):
            new_this_round += 1
        elif str(prev) != str(v):
            # They said something different the second time. Keep the newer
            # value but record the change: a price that moves between rounds
            # is worth knowing about.
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
    elif re.fullmatch(r"-?\d+(\.\d+)?", str(v)):
        v = float(v) if "." in str(v) else int(v)
    elif str(v).lower() in ("true", "false"):
        v = str(v).lower() == "true"
    rec["from_quote"][b["key"]] = v
    rec["flags"] = P._flags(rec)
    save(b["wheel"], rec)
    return {"ok": True, "record": rec}


# --------------------------------------------------------------------------
# ui
# --------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wheel sourcing</title>
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
</style></head><body><div class="wrap">

<header>
  <h1>Wheel sourcing</h1>
  <div style="display:flex;gap:10px;align-items:flex-end">
    <div><span class="lbl">Wheel</span><select id="wheel" onchange="boot(this.value)"></select></div>
    <button class="ghost" onclick="newWheel()">New</button>
  </div>
</header>

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
  <div><span class="lbl">Finish</span><input id="finish" value="Bronze"></div>
  <button onclick="genRfq()">Generate</button>
</div>
<div class="hint">Generated messages appear on each supplier below, with a copy button.</div>

<div class="band">3 · Suppliers <span id="supcount"></span></div>
<div id="sups"></div>

</div>
<script>
let S={wheel:"",suppliers:[],fields:{},rfqs:{}}, OPEN=null;

async function boot(w){
  const r = await (await fetch("/api/state?wheel="+encodeURIComponent(w||""))).json();
  S.wheel=r.wheel; S.suppliers=r.suppliers; S.fields=r.fields;
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
async function genRfq(){
  const r=await post("/api/rfq",{wheel:S.wheel,size:size.value,offset:offset.value,pcd:pcd.value,finish:finish.value});
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
    const fields=Object.entries(S.fields).map(([k,desc])=>{
      const v=Q[k]; const empty=(v===null||v===undefined||v==="");
      return `<div class="f"><label title="${desc}">${k.replace(/_/g," ")}</label>
        <input class="${empty?'empty':''}" value="${empty?'':v}"
          onchange="setField('${s.id}','${k}',this.value)"></div>`;
    }).join("");
    const rfq=S.rfqs[s.id];
    return `<div class="sup ${OPEN===s.id?'open':''}" id="s-${s.id}">
      <div class="sup-h" onclick="toggle('${s.id}')">
        <div><div class="sup-n">${s.id}</div>
          <div class="sup-m">${answered}/${Object.keys(S.fields).length} answered
            ${s.meta&&s.meta.reply_received?"· replied "+s.meta.reply_received:"· no reply yet"}</div>
          <div style="margin-top:7px">${pills}</div>
          ${platRow}</div>
        <div class="sup-p">
          ${L.listed_price_low?`<span>listed $${L.listed_price_low}</span>`:""}
          ${Q.price_per_wheel_1set?`<div><b>quoted $${Q.price_per_wheel_1set}</b></div>`:""}
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
          <button onclick="sendReply('${s.id}')">Parse reply</button>
        </div>
        <span class="lbl">Quoted data · dashed border means still unanswered</span>
        <div class="grid" style="margin-top:8px">${fields}</div>
      </div></div>`;
  }).join("") || `<div class="hint">Nothing yet. Add listing URLs above and pull.</div>`;
}

function toggle(id){OPEN=OPEN===id?null:id;render();}
function copyRfq(id){const t=document.getElementById("rfq-"+id);t.select();
  navigator.clipboard.writeText(t.value).catch(()=>document.execCommand("copy"));}
async function sendReply(id){
  const text=document.getElementById("rep-"+id).value.trim(); if(!text) return;
  const btn=event.target; btn.textContent="Parsing..."; btn.disabled=true;
  const r=await post("/api/reply",{wheel:S.wheel,id,text,date:document.getElementById("dt-"+id).value});
  if(r.ok){S.suppliers=S.suppliers.map(s=>s.id===id?{...r.record,_gaps:Object.keys(S.fields).filter(k=>!r.record.from_quote[k]&&r.record.from_quote[k]!==false)}:s);}
  render();
}
async function setField(id,key,value){
  const r=await post("/api/field",{wheel:S.wheel,id,key,value});
  if(r.ok){S.suppliers=S.suppliers.map(s=>s.id===id?{...r.record,_gaps:Object.keys(S.fields).filter(k=>!r.record.from_quote[k]&&r.record.from_quote[k]!==false)}:s);render();}
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

    reqs = load_requests()
    reqs.append({"name": name, "date": str(date.today())})
    save_requests(reqs)
    return JSONResponse({"ok": True})


@app.get("/requests", response_class=HTMLResponse)
def show_requests():
    reqs = load_requests()
    rows = "".join(f"<tr><td>{r['name']}</td><td>{r['date']}</td></tr>" for r in reqs)
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
    table {{ width: 100%; max-width: 600px; border-collapse: collapse; }}
    th {{ text-align: left; padding: 12px; border-bottom: 2px solid #333; font-weight: 600; text-transform: uppercase; font-size: 12px; }}
    td {{ padding: 12px; border-bottom: 1px solid #ddd; }}
    tr:hover {{ background: #f0f0ea; }}
    .empty {{ color: #999; font-style: italic; padding: 32px 0; text-align: center; }}
  </style>
</head>
<body>
  <h1>Wheel Requests</h1>
  <table>
    <thead>
      <tr>
        <th>Wheel Name</th>
        <th>Date Requested</th>
      </tr>
    </thead>
    <tbody>
      {rows or '<tr><td colspan="2" class="empty">No requests yet</td></tr>'}
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
    uvicorn.run(app, host="127.0.0.1", port=8420, log_level="warning")
