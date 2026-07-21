#!/usr/bin/env python3
"""
Browser simulator for Splunk RUM session generation.

Visits the astroshop demo UI via rum-proxy (which injects the Splunk RUM JS
snippet), generating real browser sessions that flow to Splunk Observability
Cloud RUM. Each session simulates a realistic user journey through the store.

Configured via environment variables:
  DEMO_URL        Target URL (default: http://rum-proxy:80)
  SIM_INTERVAL    Seconds between sessions (default: 45)
  PAGE_WAIT_MIN   Min ms to linger on a page (default: 2000)
  PAGE_WAIT_MAX   Max ms to linger on a page (default: 5000)
"""

import logging
import os
import random
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

DEMO_URL       = os.environ.get("DEMO_URL", "http://rum-proxy:80")
SIM_INTERVAL   = int(os.environ.get("SIM_INTERVAL", "45"))
PAGE_WAIT_MIN  = int(os.environ.get("PAGE_WAIT_MIN", "2000"))
PAGE_WAIT_MAX  = int(os.environ.get("PAGE_WAIT_MAX", "5000"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s UTC] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

USER_AGENTS = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
]

VIEWPORTS = [
    {"width": 1280, "height": 800},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 390,  "height": 844},   # iPhone 14
    {"width": 768,  "height": 1024},  # iPad
]

LOCALES = ["en-US", "en-GB", "de-DE", "fr-FR", "ja-JP"]


def _wait(min_ms=None, max_ms=None):
    lo = min_ms or PAGE_WAIT_MIN
    hi = max_ms or PAGE_WAIT_MAX
    time.sleep(random.uniform(lo, hi) / 1000)


def simulate_session(page, session_id):
    """Simulate one user browsing session; returns a short journey description."""
    journey = []

    # ── 1. Home page ────────────────────────────────────────────────────────
    page.goto(DEMO_URL, wait_until="load", timeout=30000)
    journey.append("home")
    _wait()

    # Scroll to expose product grid
    page.evaluate("(offset) => window.scrollBy(0, offset)", random.randint(200, 600))
    _wait(800, 2000)

    # ── 2. Browse a product ─────────────────────────────────────────────────
    product_links = page.query_selector_all("a[href*='/product/']")
    if not product_links:
        log.warning(f"  [{session_id}] no product links on home page — abbreviated session")
        return journey

    pool = product_links[:8] if len(product_links) >= 8 else product_links
    chosen = random.choice(pool)
    href   = chosen.get_attribute("href") or ""
    log.info(f"  [{session_id}] product: {href}")
    journey.append(f"product:{href.split('/')[-1][:8]}")

    chosen.click()
    page.wait_for_load_state("load", timeout=20000)
    _wait()

    # Scroll product detail
    page.evaluate("window.scrollBy(0, 300)")
    _wait(1000, 3000)

    # ── 3. Add to cart (60 % of sessions) ──────────────────────────────────
    if random.random() < 0.6:
        add_btn = (
            page.query_selector("button:has-text('Add To Cart')") or
            page.query_selector("button:has-text('Add to Cart')") or
            page.query_selector("button[type='submit']")
        )
        if add_btn:
            log.info(f"  [{session_id}] add to cart")
            journey.append("add_to_cart")
            add_btn.click()
            page.wait_for_load_state("load", timeout=15000)
            _wait()

            # ── 4. View cart (70 % of add-to-cart sessions) ─────────────────
            if random.random() < 0.7:
                log.info(f"  [{session_id}] cart")
                journey.append("cart")
                page.goto(f"{DEMO_URL}/cart", wait_until="load", timeout=20000)
                _wait()

                # ── 5. Proceed to checkout (30 % of cart sessions) ──────────
                if random.random() < 0.3:
                    checkout_btn = (
                        page.query_selector("button:has-text('Place Order')") or
                        page.query_selector("a:has-text('Checkout')")
                    )
                    if checkout_btn:
                        log.info(f"  [{session_id}] checkout")
                        journey.append("checkout")
                        checkout_btn.click()
                        page.wait_for_load_state("load", timeout=20000)
                        _wait()

    # ── 6. Browse a second product (40 % of sessions) ───────────────────────
    if random.random() < 0.4:
        log.info(f"  [{session_id}] back to home (second browse)")
        page.goto(DEMO_URL, wait_until="load", timeout=20000)
        journey.append("home2")
        _wait(800, 1500)

        product_links = page.query_selector_all("a[href*='/product/']")
        if product_links:
            pool2 = product_links[:8] if len(product_links) >= 8 else product_links
            p2 = random.choice(pool2)
            href2 = p2.get_attribute("href") or ""
            log.info(f"  [{session_id}] second product: {href2}")
            journey.append(f"product2:{href2.split('/')[-1][:8]}")
            p2.click()
            page.wait_for_load_state("load", timeout=20000)
            _wait()

    return journey


def run():
    session_count = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        log.info("=" * 60)
        log.info("  Splunk RUM browser simulator")
        log.info(f"  Target:   {DEMO_URL}")
        log.info(f"  Interval: {SIM_INTERVAL}s between sessions")
        log.info("=" * 60)

        while True:
            session_count += 1
            sid = f"sim-{session_count:04d}"

            viewport = random.choice(VIEWPORTS)
            context  = browser.new_context(
                viewport=viewport,
                user_agent=random.choice(USER_AGENTS),
                locale=random.choice(LOCALES),
            )
            page = context.new_page()

            log.info(f"[{sid}] starting  viewport={viewport['width']}x{viewport['height']}")
            try:
                journey = simulate_session(page, sid)
                log.info(f"[{sid}] complete  journey={' → '.join(journey)}")
            except PlaywrightTimeout as e:
                log.warning(f"[{sid}] timeout: {e}")
            except Exception as e:
                log.warning(f"[{sid}] error: {e}")
            finally:
                context.close()

            log.info(f"[{sid}] sleeping {SIM_INTERVAL}s ...")
            time.sleep(SIM_INTERVAL)


if __name__ == "__main__":
    run()
