#!/usr/bin/env python3
"""Resolve a Douyin video URL to a direct media URL via a real browser.

Why this exists (2026-11): Douyin moved its share page behind Argus / UIFID
risk control. The share page now returns only anti-bot JS — no `play_addr`,
no `playApi`, no media URL at all — and yt-dlp's own extractor answers
"Fresh cookies (not necessarily logged in) are needed". Neither path works.

What does work: open the video page in a real browser (playwright chromium),
let the page's JS run and mint the risk-control cookies, then read
`video.currentSrc`. That is the actual CDN URL (douyinvod.com), usually the
watermark-free `play` rendition, and it downloads with a plain Referer header.

Usage:
    python3 douyin_browser_resolve.py <douyin_url> [--json]
    python3 douyin_browser_resolve.py <douyin_url> --cookie-out FILE

Prints the direct media URL on stdout (or a JSON object with --json).
Exit codes: 0 ok, 3 bad url, 4 page loaded but no media found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RESOLVE_JS = """
() => {
  const v = document.querySelector('video');
  if (!v) return {url: '', w: 0, h: 0, title: document.title};
  return {
    url: v.currentSrc || v.src || '',
    w: v.videoWidth || 0,
    h: v.videoHeight || 0,
    title: document.title || '',
  };
}
"""


def extract_aweme_id(url: str) -> str:
    m = re.search(r"/(?:video|share/video)/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"modal_id=(\d+)", url)
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cookie-out", help="write the page cookies as a Netscape cookie file")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    aweme_id = extract_aweme_id(args.url)
    is_short_link = not aweme_id

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed (pip install playwright && playwright install chromium)", file=sys.stderr)
        return 4

    if aweme_id:
        page_url = f"https://www.douyin.com/video/{aweme_id}"
    else:
        # Short link (v.douyin.com/xxx) or an unfamiliar douyin url: hand it
        # straight to the browser, which follows the redirect to the full page.
        page_url = args.url
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(page_url, timeout=args.timeout * 1000, wait_until="domcontentloaded")
            # Give the risk-control JS time to mint cookies and the player time to
            # attach a media source.
            try:
                page.wait_for_function(
                    "() => { const v = document.querySelector('video'); return v && v.currentSrc; }",
                    timeout=args.timeout * 1000,
                )
            except Exception:
                pass
            page.wait_for_timeout(1500)
            info = page.evaluate(RESOLVE_JS)

            if is_short_link:
                # The redirect landed on the canonical video page; recover the id.
                final_id = extract_aweme_id(page.url)
                if final_id:
                    aweme_id = final_id

            if args.cookie_out:
                cookies = ctx.cookies()
                lines = ["# Netscape HTTP Cookie File"]
                for c in cookies:
                    domain = c.get("domain", ".douyin.com")
                    if "douyin" not in domain and "bytedance" not in domain:
                        continue
                    lines.append(
                        "\t".join([
                            domain,
                            "TRUE" if domain.startswith(".") else "FALSE",
                            c.get("path", "/"),
                            "FALSE",
                            str(int(c.get("expires") or 0) if c.get("expires", 0) > 0 else 1793511091),
                            c["name"],
                            c["value"],
                        ])
                    )
                with open(args.cookie_out, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
        finally:
            browser.close()

    media = info.get("url") or ""
    if not media:
        print(f"ERROR: page loaded but no media URL found (title={info.get('title')!r})", file=sys.stderr)
        print("The video may be deleted, private, or region-locked.", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps({
            "aweme_id": aweme_id,
            "media_url": media,
            "width": info.get("w"),
            "height": info.get("h"),
            "title": info.get("title"),
        }, ensure_ascii=False))
    else:
        print(media)
    return 0


if __name__ == "__main__":
    sys.exit(main())
