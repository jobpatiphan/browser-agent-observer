(function () {
  const statusEl = document.getElementById("conn-status");
  const flowCountEl = document.getElementById("flow-count");
  const frameEl = document.getElementById("browser-frame");
  const frameBadge = document.getElementById("frame-badge");
  const cursorOverlay = document.getElementById("cursor-overlay");
  const filmstrip = document.getElementById("filmstrip");
  const trafficBody = document.getElementById("traffic-body");
  const narrationBody = document.getElementById("narration-body");

  // Latest frame's page dimensions (px), so we can map action coords onto the
  // scaled <img>. Client-side cursor is approximate; the authoritative
  // highlight is the one baked into the frame by the forwarder.
  let frameW = null, frameH = null;
  let liveFollow = true;   // false while the user is scrubbing the filmstrip
  let cursorTimer = null;

  const overlay = document.getElementById("detail-overlay");
  const detailTitle = document.getElementById("detail-title");
  const tabRequest = document.getElementById("tab-request");
  const tabResponse = document.getElementById("tab-response");
  const tabCookies = document.getElementById("tab-cookies");
  const tabTiming = document.getElementById("tab-timing");
  document.getElementById("detail-close").addEventListener("click", () => {
    overlay.classList.add("hidden");
  });
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.classList.add("hidden");
  });
  document.querySelectorAll(".detail-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".detail-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".detail-view").forEach((v) => v.classList.add("hidden"));
      document.getElementById("tab-" + btn.dataset.tab).classList.remove("hidden");
    });
  });

  // ---- traffic filtering + security headers -----------------------------
  const filterMethod = document.getElementById("filter-method");
  const filterStatus = document.getElementById("filter-status");
  const filterSearch = document.getElementById("filter-search");
  [filterMethod, filterStatus].forEach((el) => el.addEventListener("change", applyFilters));
  filterSearch.addEventListener("input", applyFilters);

  function rowMatches(ev) {
    if (!ev) return true;
    if (filterMethod.value && ev.method !== filterMethod.value) return false;
    if (filterStatus.value) {
      if (ev.status == null) return false;
      if (String(ev.status)[0] !== filterStatus.value) return false;
    }
    const q = filterSearch.value.trim().toLowerCase();
    if (q && !((ev.url || "") + " " + (ev.path || "")).toLowerCase().includes(q)) return false;
    return true;
  }

  function applyFilters() {
    for (const [id, row] of rowsById) {
      row.hidden = !rowMatches(flowDataById.get(id));
    }
  }

  // The 4 headers that matter most for a quick pentest read.
  const SEC_HEADERS = [
    { key: "content-security-policy", label: "Content-Security-Policy" },
    { key: "strict-transport-security", label: "Strict-Transport-Security" },
    { key: "x-frame-options", label: "X-Frame-Options / frame-ancestors" },
    { key: "x-content-type-options", label: "X-Content-Type-Options" },
  ];

  function securityReport(ev) {
    const pairs = headerPairs(ev && ev.response && ev.response.headers);
    const names = new Set(pairs.map(([k]) => String(k).toLowerCase()));
    const cspVal = (pairs.find(([k]) => k.toLowerCase() === "content-security-policy") || [])[1] || "";
    const present = [], missing = [];
    for (const h of SEC_HEADERS) {
      let ok = names.has(h.key);
      // X-Frame-Options is also satisfied by CSP frame-ancestors.
      if (h.key === "x-frame-options" && /frame-ancestors/i.test(cspVal)) ok = true;
      (ok ? present : missing).push(h.label);
    }
    return { present, missing, score: present.length, total: SEC_HEADERS.length };
  }

  const rowsById = new Map();
  const flowDataById = new Map();
  let flowCount = 0;

  function statusClass(status) {
    if (status == null) return "";
    if (status < 300) return "status-2xx";
    if (status < 400) return "status-3xx";
    if (status < 500) return "status-4xx";
    return "status-5xx";
  }

  function fmtTime(ts) {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
  }

  function fmtSize(n) {
    if (n == null) return "-";
    if (n < 1024) return n + "B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + "KB";
    return (n / (1024 * 1024)).toFixed(1) + "MB";
  }

  function upsertFlow(event) {
    let row = rowsById.get(event.id);
    if (!row) {
      // An "error" event for a flow we never saw start is nothing to show.
      if (event.phase === "error") return;
      row = document.createElement("tr");
      row.addEventListener("click", () => showDetail(event.id));
      trafficBody.appendChild(row);
      rowsById.set(event.id, row);
      flowCount++;
      flowCountEl.textContent = flowCount + " requests";
    }

    if (event.phase === "error") {
      // Connection reset / TLS failure / timeout — the flow never completed.
      const first = row.firstElementChild, path = first && first.nextElementSibling;
      row.className = "failed";
      const statusCell = path && path.nextElementSibling;
      if (statusCell) { statusCell.textContent = "ERR"; statusCell.className = "status-5xx"; }
      return;
    }

    if (event.phase === "request") {
      row.className = "pending";
      row.innerHTML =
        `<td class="${methodClass(event.method)}">${escapeHtml(event.method)}</td>` +
        `<td title="${escapeHtml(event.url || "")}">${escapeHtml(event.path || event.url)}</td>` +
        `<td>…</td><td>-</td><td>-</td><td>-</td>`;
    } else {
      row.className = "";
      const sec = securityReport(event);
      row.dataset.ts = event.ts;
      row.innerHTML =
        `<td class="${methodClass(event.method)}">${escapeHtml(event.method)}</td>` +
        `<td title="${escapeHtml(event.url || "")}">${escapeHtml(event.path || event.url)}</td>` +
        `<td class="${statusClass(event.status)}">${event.status ?? "-"}</td>` +
        `<td>${fmtSize(event.size)}</td>` +
        `<td title="${new Date(event.ts).toLocaleTimeString([], { hour12: false })}">${event.duration_ms != null ? event.duration_ms + "ms" : "-"}</td>` +
        `<td>${secBadge(sec)}</td>`;
      flowDataById.set(event.id, event);
      row.hidden = !rowMatches(event);
    }

    if (liveFollow) trafficBody.scrollTop = trafficBody.scrollHeight;
  }

  function methodClass(m) {
    return "m-" + String(m || "").toLowerCase();
  }

  function secBadge(sec) {
    const cls = sec.score === sec.total ? "sec-good" : sec.score === 0 ? "sec-bad" : "sec-warn";
    const tip = (sec.missing.length ? "Missing: " + sec.missing.join(", ") : "All present");
    return `<span class="sec-badge ${cls}" title="${escapeHtml(tip)}">${sec.score}/${sec.total}</span>`;
  }

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // Headers arrive as [[name, value], ...]. Tolerate the old dict shape too so
  // a stale replayed event doesn't throw.
  function headerPairs(headers) {
    if (Array.isArray(headers)) return headers;
    if (headers && typeof headers === "object") return Object.entries(headers);
    return [];
  }

  function renderSide(side) {
    if (!side) return "(no data)";
    const lines = [];
    lines.push(`content-type: ${side.content_type || "-"}`);
    // headers is an ordered list of [name, value] pairs so repeated names
    // (e.g. multiple Set-Cookie) each get their own line.
    for (const [k, v] of headerPairs(side.headers)) {
      lines.push(`${k}: ${v}`);
    }
    lines.push("");
    if (side.body_encoding === "base64") {
      lines.push(`[binary/image, ${side.size} bytes, base64-embedded]`);
    } else if (side.body_encoding === "omitted") {
      lines.push(`[body omitted, ${side.size} bytes]`);
    } else {
      lines.push(side.body || "(empty body)");
      if (side.body_truncated) lines.push("\n… [truncated]");
    }
    return lines.join("\n");
  }

  function showDetail(id) {
    const event = flowDataById.get(id);
    if (!event) return;
    detailTitle.textContent = `${event.method} ${event.url} — ${event.status ?? "-"}`;
    tabRequest.textContent = renderSide(event.request);
    tabResponse.textContent = renderSide(event.response);
    renderCookies(event);
    renderTiming(event);
    // reset to the Request tab
    document.querySelectorAll(".detail-tab").forEach((b, i) => b.classList.toggle("active", i === 0));
    document.querySelectorAll(".detail-view").forEach((v) => v.classList.add("hidden"));
    tabRequest.classList.remove("hidden");
    overlay.classList.remove("hidden");
  }

  function renderCookies(event) {
    const out = [];
    const reqCookie = headerPairs(event.request && event.request.headers)
      .filter(([k]) => k.toLowerCase() === "cookie");
    const setCookies = headerPairs(event.response && event.response.headers)
      .filter(([k]) => k.toLowerCase() === "set-cookie");

    out.push(`<h4>Set-Cookie (${setCookies.length})</h4>`);
    if (setCookies.length) {
      setCookies.forEach(([, v], i) => {
        const flags = [];
        if (/;\s*httponly/i.test(v)) flags.push("HttpOnly");
        if (/;\s*secure/i.test(v)) flags.push("Secure");
        const sm = v.match(/;\s*samesite=([^;]+)/i);
        flags.push("SameSite=" + (sm ? sm[1].trim() : "—"));
        out.push(`<div class="cookie">${i + 1}/${setCookies.length} <code>${escapeHtml(v)}</code>` +
          `<div class="cookie-flags">${flags.map((f) => `<span>${escapeHtml(f)}</span>`).join("")}</div></div>`);
      });
    } else {
      out.push('<div class="muted-block">none</div>');
    }

    out.push(`<h4>Request Cookie</h4>`);
    if (reqCookie.length) {
      reqCookie.forEach(([, v]) => {
        v.split(/;\s*/).forEach((c) => out.push(`<div class="cookie"><code>${escapeHtml(c)}</code></div>`));
      });
    } else {
      out.push('<div class="muted-block">none</div>');
    }
    tabCookies.innerHTML = out.join("");
  }

  function renderTiming(event) {
    const sec = securityReport(event);
    const rows = [
      ["Status", event.status ?? "-"],
      ["Duration", event.duration_ms != null ? event.duration_ms + " ms" : "-"],
      ["Response size", fmtSize(event.size)],
      ["Started", new Date(event.ts).toLocaleTimeString([], { hour12: false })],
    ];
    let html = "<h4>Timing</h4><table class='kv'>" +
      rows.map(([k, v]) => `<tr><td>${k}</td><td>${escapeHtml(String(v))}</td></tr>`).join("") +
      "</table>";
    html += `<h4>Security headers ${sec.score}/${sec.total}</h4><ul class="sec-list">`;
    SEC_HEADERS.forEach((h) => {
      const ok = sec.present.includes(h.label);
      html += `<li class="${ok ? "sec-ok" : "sec-no"}">${ok ? "✓" : "✗"} ${escapeHtml(h.label)}</li>`;
    });
    html += "</ul>";
    tabTiming.innerHTML = html;
  }

  // ---- activity log (narration + actions + commands) --------------------
  let logFilter = "all";

  function addActivity(kind, ts, label, cls) {
    const line = document.createElement("div");
    line.className = "log-line log-" + kind + (cls ? " " + cls : "");
    line.dataset.kind = kind;
    line.innerHTML =
      `<span class="log-ts">${fmtTime(ts)}</span>` +
      `<span class="log-tag">${kind}</span>` +
      `<span class="log-text">${label}</span>`;
    line.hidden = !(logFilter === "all" || logFilter === kind);
    narrationBody.appendChild(line);
    narrationBody.scrollTop = narrationBody.scrollHeight;
    return line;
  }

  function addNarration(event) {
    addActivity("narration", event.ts,
      escapeHtml(event.text), "level-" + (event.level || "info"));
  }

  function addAction(event) {
    const c = event.coords ? ` <span class="log-coords">(${event.coords.x},${event.coords.y})</span>` : "";
    const tgt = event.target ? " " + escapeHtml(event.target) : "";
    const line = addActivity("action", event.ts,
      `<b>${escapeHtml(event.action)}</b>${tgt}${c}`);
    line.classList.add("clickable");
    line.title = "jump to the closest traffic row";
    line.addEventListener("click", () => syncToTraffic(event.ts));
    if (event.coords) showCursor(event.coords);
    markFilmstrip(event);
  }

  // Playwright-style: selecting an action jumps to the traffic captured nearest
  // to it in time, flashing and scrolling the matching row into view.
  function syncToTraffic(ts) {
    let best = null, bestDelta = Infinity;
    for (const row of rowsById.values()) {
      if (row.hidden || !row.dataset.ts) continue;
      const d = Math.abs(Number(row.dataset.ts) - ts);
      if (d < bestDelta) { bestDelta = d; best = row; }
    }
    if (!best) return;
    trafficBody.querySelectorAll("tr.flash").forEach((r) => r.classList.remove("flash"));
    best.classList.add("flash");
    best.scrollIntoView({ block: "center", behavior: "smooth" });
    setTimeout(() => best.classList.remove("flash"), 1500);
  }

  document.querySelectorAll(".log-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".log-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      logFilter = btn.dataset.filter;
      narrationBody.querySelectorAll(".log-line").forEach((l) => {
        l.hidden = !(logFilter === "all" || logFilter === l.dataset.kind);
      });
    });
  });

  // ---- browser frame + client cursor overlay ----------------------------
  function setFrame(event) {
    frameEl.src = "data:image/jpeg;base64," + event.data;
    if (event.width) frameW = event.width;
    if (event.height) frameH = event.height;
    frameBadge.textContent = event.hq ? "HQ" : "live";
    frameBadge.className = "frame-badge " + (event.hq ? "badge-hq" : "badge-live");
  }

  // Map page coords -> position within the rendered (object-fit:contain) image.
  function pageToDisplay(x, y) {
    const wrap = document.getElementById("frame-wrap").getBoundingClientRect();
    const img = frameEl.getBoundingClientRect();
    if (!frameW || !frameH || !img.width) return null;
    const scale = Math.min(img.width / frameW, img.height / frameH);
    const offX = img.left - wrap.left + (img.width - frameW * scale) / 2;
    const offY = img.top - wrap.top + (img.height - frameH * scale) / 2;
    return { x: offX + x * scale, y: offY + y * scale };
  }

  function showCursor(coords) {
    const p = pageToDisplay(coords.x, coords.y);
    if (!p) return;
    cursorOverlay.style.left = p.x + "px";
    cursorOverlay.style.top = p.y + "px";
    cursorOverlay.classList.remove("hidden");
    // restart the ripple animation
    cursorOverlay.classList.remove("ripple");
    void cursorOverlay.offsetWidth;
    cursorOverlay.classList.add("ripple");
    if (cursorTimer) clearTimeout(cursorTimer);
    cursorTimer = setTimeout(() => cursorOverlay.classList.add("hidden"), 1200);
  }

  // ---- filmstrip --------------------------------------------------------
  const MAX_THUMBS = 60;

  function addThumb(event) {
    const thumb = document.createElement("div");
    thumb.className = "thumb" + (event.hq ? " thumb-hq" : "");
    thumb.dataset.ts = event.ts;
    thumb.style.backgroundImage = `url("data:image/jpeg;base64,${event.data}")`;
    thumb.title = new Date(event.ts).toLocaleTimeString([], { hour12: false });
    thumb.addEventListener("click", () => {
      liveFollow = false;
      frameEl.src = `data:image/jpeg;base64,${event.data}`;
      frameBadge.textContent = "paused";
      frameBadge.className = "frame-badge badge-paused";
      filmstrip.querySelectorAll(".thumb").forEach((t) => t.classList.remove("sel"));
      thumb.classList.add("sel");
    });
    filmstrip.appendChild(thumb);
    while (filmstrip.children.length > MAX_THUMBS) filmstrip.removeChild(filmstrip.firstChild);
  }

  // Put an action marker on the thumb closest in time to the action.
  function markFilmstrip(event) {
    let best = null, bestDelta = Infinity;
    for (const t of filmstrip.children) {
      const ts = Number(t.dataset.ts);
      if (!ts) continue;
      const d = Math.abs(ts - event.ts);
      if (d < bestDelta) { bestDelta = d; best = t; }
    }
    (best || filmstrip.lastElementChild)?.classList.add("has-action");
  }

  function addCommand(event) {
    addActivity("command", event.ts, "$ " + escapeHtml(event.cmd));
  }

  function handleMessage(event) {
    if (event.type === "flow") {
      upsertFlow(event);
    } else if (event.type === "frame") {
      if (liveFollow) setFrame(event);
      addThumb(event);
    } else if (event.type === "narration") {
      addNarration(event);
    } else if (event.type === "action") {
      addAction(event);
    } else if (event.type === "command") {
      addCommand(event);
    }
  }

  // ---- session export (self-contained replay HTML) ----------------------
  document.getElementById("export-btn").addEventListener("click", async () => {
    try {
      const data = await (await fetch("/export")).json();
      const html = buildReplayHtml(data);
      const blob = new Blob([html], { type: "text/html" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      a.download = `pentest-session-${stamp}.html`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) {
      console.error("export failed", e);
    }
  });

  function buildReplayHtml(data) {
    // A single self-contained file: embedded frames + events + a tiny scrubber.
    // No external requests, no video encoding — just the pieces we already have.
    const json = JSON.stringify(data).replace(/</g, "\\u003c");
    return `<!doctype html><html><head><meta charset="utf-8">
<title>Pentest session replay</title>
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
  .a{color:#ffd9a0}.c{color:#60a5fa}.n{color:#c9d1d9}
  h3{color:#8b949e;font-size:11px;text-transform:uppercase}
</style></head><body>
<header>Pentest session replay — exported ${new Date(data.meta.exported_ts).toLocaleString()}</header>
<div id="wrap">
  <section>
    <h3>Browser (<span id="pos">0</span>/${data.frames.length})</h3>
    <img id="frame"><br>
    <input id="scrub" type="range" min="0" max="${Math.max(0, data.frames.length - 1)}" value="0">
    <div id="fts"></div>
  </section>
  <section>
    <h3>Activity</h3><div id="log"></div>
    <h3>Traffic (${data.flows.length})</h3><table id="traf"></table>
  </section>
</div>
<script>
const D=${json};
const img=document.getElementById('frame'),scrub=document.getElementById('scrub'),
      pos=document.getElementById('pos'),fts=document.getElementById('fts');
function show(i){if(!D.frames.length)return;const f=D.frames[i];img.src='data:image/jpeg;base64,'+f.data;
  pos.textContent=i+1;fts.textContent=new Date(f.ts).toLocaleTimeString();}
scrub.oninput=e=>show(+e.target.value);show(0);
const ev=[].concat(
  D.narration.map(x=>({ts:x.ts,k:'n',t:x.text})),
  D.actions.map(x=>({ts:x.ts,k:'a',t:x.action+' '+(x.target||'')+(x.coords?' ('+x.coords.x+','+x.coords.y+')':'')})),
  D.commands.map(x=>({ts:x.ts,k:'c',t:'$ '+x.cmd}))
).sort((a,b)=>a.ts-b.ts);
document.getElementById('log').innerHTML=ev.map(e=>
  '<div class="'+e.k+'"><span class="tag">'+({n:'narr',a:'action',c:'cmd'}[e.k])+'</span>'+
  new Date(e.ts).toLocaleTimeString()+' '+esc(e.t)+'</div>').join('');
document.getElementById('traf').innerHTML=D.flows.map(f=>
  '<tr><td>'+esc(f.method)+'</td><td>'+esc(f.path||f.url||'')+'</td><td>'+(f.status||'-')+
  '</td><td>'+(f.duration_ms!=null?f.duration_ms+'ms':'-')+'</td></tr>').join('');
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
</script></body></html>`;
  }

  // Double-click the frame to resume the live feed after scrubbing.
  frameEl.addEventListener("dblclick", () => {
    liveFollow = true;
    filmstrip.querySelectorAll(".thumb").forEach((t) => t.classList.remove("sel"));
  });

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/dashboard`);

    ws.onopen = () => {
      statusEl.textContent = "live";
      statusEl.className = "pill pill-up";
    };
    ws.onclose = () => {
      statusEl.textContent = "reconnecting…";
      statusEl.className = "pill pill-down";
      setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (msg) => {
      try {
        handleMessage(JSON.parse(msg.data));
      } catch (e) {
        console.error("bad message", e);
      }
    };
  }

  connect();
})();
