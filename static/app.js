(function () {
  const statusEl = document.getElementById("conn-status");
  const flowCountEl = document.getElementById("flow-count");
  const frameEl = document.getElementById("browser-frame");
  const trafficBody = document.getElementById("traffic-body");
  const narrationBody = document.getElementById("narration-body");

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

  function addNarration(event) {
    const line = document.createElement("div");
    line.className = "narr-line narr-" + (event.level || "info");
    line.innerHTML = `<span class="narr-ts">${fmtTime(event.ts)}</span>${escapeHtml(event.text)}`;
    narrationBody.appendChild(line);
    narrationBody.scrollTop = narrationBody.scrollHeight;
  }

  function handleMessage(event) {
    if (event.type === "flow") {
      upsertFlow(event);
    } else if (event.type === "frame") {
      frameEl.src = "data:image/jpeg;base64," + event.data;
    } else if (event.type === "narration") {
      addNarration(event);
    }
  }

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
