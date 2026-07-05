#!/usr/bin/env python3
"""Export a browser-agent-observer session to a self-contained replay .html
from the command line — same artifact as the dashboard's Export button.

    python3 tools/export_session.py                 # redacted, auto-named file
    python3 tools/export_session.py --out s.html --no-redact
    DASH_URL=http://host:8790 python3 tools/export_session.py

Handy for `/observe-agent-browser` record mode and CI/automation.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

# Self-contained: embedded frames + events + a tiny scrubber whose position
# highlights the traffic/activity captured nearest that frame in time.
TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>browser-agent-observer replay</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;background:#0d1117;color:#c9d1d9;font:13px/1.5 monospace}
  header{padding:8px 16px;background:#161b22;border-bottom:2px solid #f59e0b;color:#f59e0b;font-weight:700}
  #wrap{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#30363d;height:calc(100vh - 40px)}
  section{background:#0d1117;overflow:auto;padding:10px}
  img{max-width:100%;background:#000}
  input[type=range]{width:100%}
  table{width:100%;border-collapse:collapse}td{padding:3px 6px;border-bottom:1px solid #161b22;font-size:11.5px}
  .tag{font-size:9px;text-transform:uppercase;color:#8b949e;border:1px solid #30363d;border-radius:3px;padding:0 4px;margin-right:6px}
  .a{color:#ffd9a0}.c{color:#60a5fa}.n{color:#c9d1d9}.w{color:#7fd4ff}
  .hl{background:#2a2410 !important;outline:1px solid #f59e0b}
  h3{color:#8b949e;font-size:11px;text-transform:uppercase}
</style></head><body>
<header id="hdr"></header>
<div id="wrap">
  <section>
    <h3>Browser (<span id="pos">0</span>/<span id="nframes">0</span>)</h3>
    <img id="frame"><br><input id="scrub" type="range" min="0" max="0" value="0"><div id="fts"></div>
  </section>
  <section>
    <h3>Activity</h3><div id="log"></div>
    <h3 id="trafh">Traffic</h3><table id="traf"></table>
  </section>
</div>
<script>
const D=__DATA__;
const img=document.getElementById('frame'),scrub=document.getElementById('scrub'),
      pos=document.getElementById('pos'),fts=document.getElementById('fts');
document.getElementById('hdr').textContent='browser-agent-observer replay — '+
  new Date(D.meta.exported_ts).toLocaleString()+(D.meta.redacted?' (redacted)':'');
document.getElementById('nframes').textContent=D.frames.length;
document.getElementById('trafh').textContent='Traffic ('+D.flows.length+')';
scrub.max=Math.max(0,D.frames.length-1);
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
const ev=[].concat(
  D.narration.map(x=>({ts:x.ts,k:'n',t:x.text})),
  D.actions.map(x=>({ts:x.ts,k:'a',t:x.action+' '+(x.target||'')+(x.coords?' ('+x.coords.x+','+x.coords.y+')':'')})),
  D.commands.map(x=>({ts:x.ts,k:'c',t:'$ '+x.cmd})),
  (D.ws||[]).map(x=>({ts:x.ts,k:'w',t:(x.from_client?'▲ ':'▼ ')+(x.encoding==='base64'?'[binary '+x.size+'B]':x.payload)}))
).sort((a,b)=>a.ts-b.ts);
document.getElementById('log').innerHTML=ev.map(e=>
  '<div class="'+e.k+'" data-ts="'+e.ts+'"><span class="tag">'+({n:'narr',a:'action',c:'cmd',w:'ws'}[e.k])+'</span>'+
  new Date(e.ts).toLocaleTimeString()+' '+esc(e.t)+'</div>').join('');
document.getElementById('traf').innerHTML=D.flows.map(f=>
  '<tr data-ts="'+(f.ts||0)+'"><td>'+esc(f.method)+'</td><td>'+esc(f.path||f.url||'')+'</td><td>'+(f.status||'-')+
  '</td><td>'+(f.duration_ms!=null?f.duration_ms+'ms':'-')+'</td></tr>').join('');
function nearest(sel,ts){let best=null,bd=Infinity;document.querySelectorAll(sel).forEach(function(el){var t=+el.dataset.ts;if(!t)return;var d=Math.abs(t-ts);if(d<bd){bd=d;best=el;}});return best;}
function show(i){if(!D.frames.length)return;const f=D.frames[i];img.src='data:image/jpeg;base64,'+f.data;
  pos.textContent=i+1;fts.textContent=new Date(f.ts).toLocaleTimeString();
  document.querySelectorAll('.hl').forEach(el=>el.classList.remove('hl'));
  var l=nearest('#log>div',f.ts); if(l){l.classList.add('hl');l.scrollIntoView({block:'nearest'});}
  var r=nearest('#traf tr',f.ts); if(r){r.classList.add('hl');r.scrollIntoView({block:'nearest'});}
}
scrub.oninput=e=>show(+e.target.value);show(0);
</script></body></html>"""


def fetch(url: str, redact: bool) -> dict:
    u = f"{url.rstrip('/')}/export?redact={'1' if redact else '0'}"
    with urllib.request.urlopen(u, timeout=15) as r:
        return json.load(r)


def build_html(data: dict) -> str:
    # </script> inside embedded JSON would end the script tag early; escape it.
    payload = json.dumps(data).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", payload)


def main():
    ap = argparse.ArgumentParser(description="Export a session to a replay .html")
    ap.add_argument("--url", default=os.environ.get("DASH_URL", "http://127.0.0.1:8790"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-redact", action="store_true", help="include secrets (default: redacted)")
    args = ap.parse_args()

    try:
        data = fetch(args.url, redact=not args.no_redact)
    except Exception as e:
        print(f"error: could not reach {args.url}/export ({e})", file=sys.stderr)
        return 1

    out = args.out or f"pentest-session-{time.strftime('%Y-%m-%dT%H-%M-%S')}.html"
    with open(out, "w") as f:
        f.write(build_html(data))
    print(f"wrote {out}  ({len(data['frames'])} frames, {len(data['flows'])} flows, "
          f"redacted={data['meta'].get('redacted')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
