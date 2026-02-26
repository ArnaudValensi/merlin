---
name: screenshot
description: Capture screenshots of web pages at multiple viewports for visual validation. Use this skill when you need to verify responsive layout or take before/after screenshots of UI changes.
user-invocable: true
allowed-tools: Bash, Read
---

# Screenshot Skill

Capture web pages at 6 viewports using Playwright (headless Firefox).

## Script

```bash
uv run .claude/skills/screenshot/screenshot.py <url> [options]
```

Run from the **project root**.

## Common Usage

```bash
# All viewports for one page
uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/overview --user admin --pass secret

# All pages at all viewports (18 screenshots)
uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass secret

# Single viewport
uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/overview --viewport desktop

# Custom output directory
uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/overview --output /tmp/shots
```

## Viewports

| Name | Size |
|------|------|
| desktop | 1200x800 |
| tablet | 768x1024 |
| mobile | 375x667 |
| mobile-large | 414x896 |
| tablet-landscape | 1024x768 |
| 4k | 1920x1080 |

## Output

Screenshots go to `./screenshots/` (relative to cwd), named `<page>-<viewport>.png`.

## Validation Workflow

1. Start the web server
2. Run screenshots: `uv run .claude/skills/screenshot/screenshot.py --all <base-url> --user <user> --pass <pass>`
3. Read the PNG files to visually verify layout at each viewport
4. Check for: broken layouts, overlapping elements, missing content, chart rendering

## Interactive Screenshots

The CLI captures static pages. For interactive UI — modals, command palettes, dropdowns, hover states — use inline Playwright Python instead.

### When to use inline Playwright

- You need to **click** an element before screenshotting (e.g. open a modal or palette)
- You need to **type** into an input (e.g. search query, form field)
- You need to **wait** for dynamic content (animations, search results, lazy-loaded data)
- You need to capture a **specific transient state** that the CLI can't reach

### Invocation pattern

```bash
uv run --with playwright python3 -c "
from playwright.sync_api import sync_playwright
import time, base64
...
"
```

### Complete example: screenshot a command palette with search results

```bash
uv run --with playwright python3 -c "
from playwright.sync_api import sync_playwright
import time, base64
from pathlib import Path

URL = 'http://localhost:3123/overview'
USER, PASS = 'admin', 'secret'
OUTPUT_DIR = Path('screenshots')
OUTPUT_DIR.mkdir(exist_ok=True)

VIEWPORTS = {
    'desktop':          {'width': 1200, 'height': 800},
    'tablet':           {'width': 768,  'height': 1024},
    'mobile':           {'width': 375,  'height': 667},
    'mobile-large':     {'width': 414,  'height': 896},
    'tablet-landscape': {'width': 1024, 'height': 768},
    '4k':               {'width': 1920, 'height': 1080},
}

with sync_playwright() as p:
    browser = p.firefox.launch(headless=True)
    creds = base64.b64encode(f'{USER}:{PASS}'.encode()).decode()

    for name, size in VIEWPORTS.items():
        context = browser.new_context(
            viewport=size,
            extra_http_headers={'Authorization': f'Basic {creds}'},
        )
        page = context.new_page()
        page.goto(URL, wait_until='networkidle')

        # Interact: open palette, type a query, wait for results
        page.click('#palette-trigger')       # open the command palette
        time.sleep(0.3)                      # wait for animation
        page.fill('#palette-input', 'test')  # type search query
        time.sleep(0.5)                      # wait for results to render

        page.screenshot(path=str(OUTPUT_DIR / f'palette-search-{name}.png'), full_page=True)
        context.close()

    browser.close()
    print(f'Done. Screenshots in {OUTPUT_DIR}/')
"
```

### Key Playwright methods

| Method | Purpose |
|--------|---------|
| `page.goto(url, wait_until='networkidle')` | Navigate and wait for load |
| `page.click(selector)` | Click an element (CSS selector) |
| `page.fill(selector, text)` | Clear an input and type text |
| `page.hover(selector)` | Hover over an element |
| `page.keyboard.press('Escape')` | Press a key |
| `page.wait_for_selector(selector)` | Wait for element to appear |
| `time.sleep(seconds)` | Wait for animations / async rendering |
| `page.screenshot(path=..., full_page=True)` | Capture the screenshot |

### Tips

- **Viewport choice depends on intent**: for **design validation** (layout, responsive CSS), loop through all 6 viewports. For **debugging functionality** (verifying a modal opens, data loads, a button works), a single viewport is enough — default to **iPhone 16 (393x852)** since the app is primarily used on mobile.
- **Create a new context per viewport** — this sets the viewport size cleanly (resizing an existing page can leave stale layout)
- **Use `time.sleep()` generously** after interactions — headless Firefox needs time for CSS transitions and JS-driven UI updates
- **Output naming**: use `<feature>-<viewport>.png` (e.g. `palette-search-desktop.png`) so files sort nicely alongside CLI-generated screenshots

## Prerequisites

Install Playwright Firefox (one-time):

```bash
uv run --with playwright playwright install firefox
```
