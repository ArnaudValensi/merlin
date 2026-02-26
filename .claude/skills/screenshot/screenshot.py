# /// script
# dependencies = ["playwright"]
# ///
"""
Screenshot utility — captures web pages at multiple viewports using Playwright.

Portable skill: outputs to ./screenshots/ relative to cwd (override with --output).

Prerequisites:
    uv run --with playwright playwright install firefox

Usage:
    uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/overview
    uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass secret
    uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/overview --output /tmp/shots

Output:
    screenshots/<page>-<viewport>.png
    e.g. screenshots/overview-desktop.png
"""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

DEFAULT_OUTPUT_DIR = Path.cwd() / "screenshots"

VIEWPORTS = {
    "desktop": {"width": 1200, "height": 800},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 375, "height": 667},
    "mobile-large": {"width": 414, "height": 896},
    "tablet-landscape": {"width": 1024, "height": 768},
    "4k": {"width": 1920, "height": 1080},
}

PAGES = ["/overview", "/performance", "/logs"]


def capture(url: str, output_path: Path, viewport: dict, user: str | None = None, password: str | None = None) -> None:
    """Capture a single screenshot."""
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context_opts = {"viewport": viewport}
        if user and password:
            import base64
            creds = base64.b64encode(f"{user}:{password}".encode()).decode()
            context_opts["extra_http_headers"] = {"Authorization": f"Basic {creds}"}
        context = browser.new_context(**context_opts)
        page = context.new_page()
        page.goto(url, wait_until="networkidle")
        page.screenshot(path=str(output_path), full_page=True)
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture dashboard screenshots at multiple viewports.",
        epilog="""
Viewports:
  desktop          1200x800
  tablet            768x1024
  mobile            375x667
  mobile-large      414x896
  tablet-landscape 1024x768
  4k               1920x1080

Examples:
  uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/overview
  uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --user admin --pass secret
  uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/overview --output /tmp/shots
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", help="URL to screenshot")
    parser.add_argument("--viewport", choices=list(VIEWPORTS.keys()), help="Single viewport (default: all)")
    parser.add_argument("--all", action="store_true", help="Screenshot all pages (/overview, /performance, /logs)")
    parser.add_argument("--user", help="Basic auth username")
    parser.add_argument("--pass", dest="password", help="Basic auth password")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory (default: ./screenshots)")

    args = parser.parse_args()
    output_dir = args.output

    output_dir.mkdir(parents=True, exist_ok=True)

    viewports = {args.viewport: VIEWPORTS[args.viewport]} if args.viewport else VIEWPORTS

    # Build list of URLs to capture
    if args.all:
        base = args.url.rstrip("/")
        urls = [(f"{base}{page}", page.strip("/")) for page in PAGES]
    else:
        parsed = urlparse(args.url)
        page_name = parsed.path.strip("/") or "index"
        urls = [(args.url, page_name)]

    total = len(urls) * len(viewports)
    count = 0

    for url, page_name in urls:
        for vp_name, vp_size in viewports.items():
            count += 1
            output = output_dir / f"{page_name}-{vp_name}.png"
            print(f"[{count}/{total}] {page_name} @ {vp_name} ({vp_size['width']}x{vp_size['height']})...", end=" ", flush=True)
            try:
                capture(url, output, vp_size, args.user, args.password)
                print(f"saved → {output}")
            except Exception as e:
                print(f"FAILED: {e}", file=sys.stderr)

    print(f"\nDone. {count} screenshots in {output_dir}/")


if __name__ == "__main__":
    main()
