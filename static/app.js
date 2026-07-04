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
  const detailRequest = document.getElementById("detail-request");
  const detailResponse = document.getElementById("detail-response");
  document.getElementById("detail-close").addEventListener("click", () => {
    overlay.classList.add("hidden");
  });
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.classList.add("hidden");
  });

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
        `<td>${escapeHtml(event.method)}</td>` +
        `<td>${escapeHtml(event.path || event.url)}</td>` +
        `<td>…</td><td>-</td><td>${fmtTime(event.ts)}</td>`;
    } else {
      row.className = "";
      row.innerHTML =
        `<td>${escapeHtml(event.method)}</td>` +
        `<td>${escapeHtml(event.path || event.url)}</td>` +
        `<td class="${statusClass(event.status)}">${event.status ?? "-"}</td>` +
        `<td>${fmtSize(event.size)}</td><td>${fmtTime(event.ts)}</td>`;
      flowDataById.set(event.id, event);
    }

    trafficBody.scrollTop = trafficBody.scrollHeight;
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
    detailRequest.textContent = renderSide(event.request);
    detailResponse.textContent = renderSide(event.response);
    overlay.classList.remove("hidden");
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
  }

  function addNarration(event) {
    addActivity("narration", event.ts,
      escapeHtml(event.text), "level-" + (event.level || "info"));
  }

  function addAction(event) {
    const c = event.coords ? ` <span class="log-coords">(${event.coords.x},${event.coords.y})</span>` : "";
    const tgt = event.target ? " " + escapeHtml(event.target) : "";
    addActivity("action", event.ts,
      `<b>${escapeHtml(event.action)}</b>${tgt}${c}`);
    if (event.coords) showCursor(event.coords);
    markFilmstrip(event);
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

  // Put an action marker on the most recent thumb (closest in time).
  function markFilmstrip(event) {
    const last = filmstrip.lastElementChild;
    if (last) last.classList.add("has-action");
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
    }
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
