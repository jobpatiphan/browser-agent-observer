# Driver recipes

The observer is **agent-agnostic**: it only needs a browser that (1) exposes CDP
on `:9222` and (2) routes HTTP through the proxy on `:8083`. Any driver that can
set those two flags works. Optionally use the zero-dependency `clients/observer`
to narrate intent onto the timeline.

Start the observer first: `./run.sh up`.

## The two flags (all drivers)

```
--remote-debugging-port=9222        # CDP, for the screencast
--proxy-server=127.0.0.1:8083       # traffic capture
--ignore-certificate-errors         # or trust the mitmproxy CA
```

## Playwright (Python)

Runnable example: [`playwright/observe_playwright.py`](playwright/observe_playwright.py).

```python
browser = p.chromium.launch(
    proxy={"server": "http://127.0.0.1:8083"},
    args=["--remote-debugging-port=9222", "--ignore-certificate-errors"],
)
```

## Puppeteer (Node)

```js
const browser = await puppeteer.launch({
  args: [
    "--remote-debugging-port=9222",
    "--proxy-server=127.0.0.1:8083",
    "--ignore-certificate-errors",
  ],
});
// optional narration:
await fetch("http://127.0.0.1:8790/narrate", {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ text: "Puppeteer: logging in", level: "info" }),
});
```

## Selenium (Python)

```python
from selenium.webdriver.chrome.options import Options
opts = Options()
opts.add_argument("--remote-debugging-port=9222")
opts.add_argument("--proxy-server=127.0.0.1:8083")
opts.add_argument("--ignore-certificate-errors")
driver = webdriver.Chrome(options=opts)
```

## browser-use / LangGraph / CrewAI browser tools

These drive Chromium under the hood — pass the same three flags through whatever
`launch_args` / `chrome_args` option the framework exposes, then narrate from
your agent loop with `from observer import obs; obs.narrate(...)`.

## Anthropic computer-use / Claude Code

See [`claude-code/`](claude-code/) — a global hook mirrors every command, and the
`/observe-agent-browser` skill flips everything on. For raw computer-use loops,
`clients/observer.py` (`obs.click(...)`, `obs.narrate(...)`) is the drop-in.
