# Agent clients

One-line helpers so *your* agent — Claude computer-use, a Codex script, a
Playwright/Puppeteer driver, anything that can make an HTTP call — can push
narration, actions and commands onto the dashboard timeline.

The dashboard doesn't care what drives the browser. It only needs:

1. the browser launched with `--remote-debugging-port` (CDP) and
   `--proxy-server` pointing at the observer (see `../run.sh`), and
2. *(optional but nice)* your agent calling these helpers so the timeline shows
   *what* it's doing, not just the resulting traffic.

## Python (`observer.py`, stdlib only)

```python
from observer import obs          # or Observer(base="http://host:8790")

obs.narrate("Trying SQLi on the login form", level="warn")
obs.click("button#login", x=203, y=411)   # cursor + highlight baked into feed
obs.action("navigate", target="https://target/admin")
obs.command("sqlmap -u https://target/login --forms")
```

### With a Claude computer-use loop
After you execute each tool call the model requested, mirror it:

```python
if tool == "computer" and inp["action"] == "left_click":
    x, y = inp["coordinate"]
    obs.click(x=x, y=y)
# and post the model's reasoning text as narration:
obs.narrate(assistant_summary)
```

## JavaScript (`observer.js`, Node 18+ or browser, no deps)

```js
import { obs } from "./observer.js";
obs.narrate("Enumerating endpoints");
await obs.click("a.nav-link", 120, 88);
```

### With Playwright
```js
page.on("framenavigated", (f) => obs.action("navigate", f.url()));
await page.click("#login");
await obs.click("#login");   // reflect it on the timeline
```

Both point at `http://127.0.0.1:8790` by default; override with the `DASH_URL`
env var or the constructor. All calls are best-effort and never throw.
