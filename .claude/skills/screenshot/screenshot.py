# /// script
# dependencies = ["playwright"]
# ///
"""
Screenshot utility — captures web pages at multiple viewports using Playwright.

Portable skill: outputs to ./screenshots/ relative to cwd (override with --output).

Auth: uses cookie-based login (POST /login with password). If --pass is omitted,
screenshots are taken without auth (for local-only mode without a password).

Prerequisites:
    uv run --with playwright playwright install firefox

Usage:
    uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/commits
    uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/commits --pass secret
    uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --pass secret
    uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/files --viewport mobile --output /tmp/shots

Output:
    screenshots/<page>-<viewport>.png
    e.g. screenshots/commits-mobile.png
"""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright  # ty: ignore[unresolved-import]  # PEP 723 inline dep

DEFAULT_OUTPUT_DIR = Path.cwd() / "screenshots"

VIEWPORTS = {
    "desktop": {"width": 1200, "height": 800},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 375, "height": 667},
    "mobile-large": {"width": 414, "height": 896},
    "tablet-landscape": {"width": 1024, "height": 768},
    "4k": {"width": 1920, "height": 1080},
}

PAGES = [
    "/files",
    "/commits",
    "/terminal",
    "/notes",
    "/overview",
    "/performance",
    "/logs",
]


def _login(page, base_url: str, password: str) -> None:
    """Log in via the cookie-based login form."""
    page.goto(f"{base_url}/login", wait_until="networkidle")
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def capture_all(
    urls: list[tuple[str, str]],
    viewports: dict[str, dict],
    output_dir: Path,
    base_url: str,
    password: str | None = None,
) -> int:
    """Capture screenshots for all url/viewport combinations.

    Reuses a single browser and one context per viewport (cookie auth is
    shared across pages within a context).
    """
    count = 0
    total = len(urls) * len(viewports)

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)

        for vp_name, vp_size in viewports.items():
            context = browser.new_context(viewport=vp_size)
            auth_page = context.new_page()

            # Authenticate once per context
            if password:
                _login(auth_page, base_url, password)

            for url, page_name in urls:
                count += 1
                output = output_dir / f"{page_name}-{vp_name}.png"
                print(
                    f"[{count}/{total}] {page_name} @ {vp_name} "
                    f"({vp_size['width']}x{vp_size['height']})...",
                    end=" ",
                    flush=True,
                )
                try:
                    auth_page.goto(url, wait_until="networkidle")
                    auth_page.screenshot(path=str(output), full_page=True)
                    print(f"saved → {output}")
                except Exception as e:
                    print(f"FAILED: {e}", file=sys.stderr)

            context.close()

        browser.close()

    return count


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
  uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/commits --pass secret
  uv run .claude/skills/screenshot/screenshot.py --all http://localhost:3123 --pass secret
  uv run .claude/skills/screenshot/screenshot.py http://localhost:3123/files --viewport mobile
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", help="URL to screenshot")
    parser.add_argument(
        "--viewport",
        choices=list(VIEWPORTS.keys()),
        help="Single viewport (default: all)",
    )
    parser.add_argument("--all", action="store_true", help="Screenshot all pages")
    parser.add_argument(
        "--pass", dest="password", help="Dashboard password (cookie auth via /login)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory (default: ./screenshots)",
    )

    args = parser.parse_args()
    output_dir = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    viewports = (
        {args.viewport: VIEWPORTS[args.viewport]} if args.viewport else VIEWPORTS
    )

    base_url = args.url.rstrip("/")

    # Build list of URLs to capture
    if args.all:
        # Strip path from base URL for --all mode
        parsed = urlparse(base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        urls = [(f"{base}{page}", page.strip("/")) for page in PAGES]
    else:
        parsed = urlparse(args.url)
        page_name = parsed.path.strip("/") or "index"
        urls = [(args.url, page_name)]

    count = capture_all(urls, viewports, output_dir, base_url, args.password)
    print(f"\nDone. {count} screenshots in {output_dir}/")


if __name__ == "__main__":
    main()
