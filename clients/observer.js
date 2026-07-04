// Tiny client for browser-agent-observer — no dependencies (global fetch,
// Node 18+ or a browser). Narrate what your agent is doing so the dashboard
// timeline reflects it:
//
//   import { obs } from "./observer.js";
//   obs.narrate("Logging in with test creds");
//   await obs.click("button#login", 203, 411);   // cursor + in-page highlight
//   obs.command("curl -s https://target/api/me");
//
// Calls are best-effort and never throw, so observability can't break your
// agent. Point elsewhere with DASH_URL env or new Observer(base).

const DEFAULT_URL =
  (typeof process !== "undefined" && process.env && process.env.DASH_URL) ||
  "http://127.0.0.1:8790";

export class Observer {
  constructor(base) {
    this.base = (base || DEFAULT_URL).replace(/\/$/, "");
  }

  async _post(path, body) {
    try {
      await fetch(this.base + path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (_) {
      /* observability must never break the agent */
    }
  }

  // level: info | warn | error
  narrate(text, level = "info") {
    return this._post("/narrate", { text, level });
  }

  // kind: click | type | scroll | navigate | key. x/y are page pixels.
  action(kind, target = null, x = null, y = null) {
    const coords = x != null && y != null ? { x, y } : null;
    return this._post("/action", { type: kind, target, coords });
  }

  click(target = null, x = null, y = null) {
    return this.action("click", target, x, y);
  }

  command(cmd) {
    return this._post("/command", { cmd });
  }
}

export const obs = new Observer();
