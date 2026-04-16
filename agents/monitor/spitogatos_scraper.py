"""
Spitogatos Scraper — GitHub Actions CI version
Runs headless with stealth settings, connects to VPS Redis via Tailscale.
Config comes entirely from environment variables (GitHub Secrets).

Keys used:
  spitogatos:seen_listings  — Redis Set  — tracks already-seen listing IDs
  spitogatos:new_listings   — Redis List — queue of new listing JSON payloads
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

import redis
from bs4 import BeautifulSoup
from loguru import logger
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# ── Config (all from env, never hardcoded) ────────────────────────────────────
VPS_TAILSCALE_IP = os.environ["VPS_TAILSCALE_IP"]   # e.g. 100.113.88.103
REDIS_PASSWORD   = os.environ["REDIS_PASSWORD"]
REDIS_PORT       = int(os.environ.get("REDIS_PORT", "6379"))

REDIS_SEEN_KEY    = "spitogatos:seen_listings"
REDIS_RESULTS_KEY = "spitogatos:new_listings"

URLS = [
    {"url": "https://www.spitogatos.gr/pwliseis-katoikies/crete", "type": "Κατοικία"},
    {"url": "https://www.spitogatos.gr/pwliseis-oikopeda/crete",  "type": "Οικόπεδο"},
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
# ─────────────────────────────────────────────────────────────────────────────

# Configure loguru: stdout only (GitHub Actions captures it)
logger.remove()
logger.add(
    sys.stdout,
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
    colorize=False,
)


# ── Redis ─────────────────────────────────────────────────────────────────────

def get_redis() -> redis.Redis:
    return redis.Redis(
        host=VPS_TAILSCALE_IP,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=10,
    )


# ── Parsing helpers ───────────────────────────────────────────────────────────

def make_id(url: str) -> str:
    """Fallback ID when listing URL has no /aggelia/ number."""
    return hashlib.md5(url.encode()).hexdigest()


def parse_title_attr(title_attr: str) -> dict:
    """
    Spitogatos packs metadata into the <a title> attribute.
    Format: "Πώληση,Κατοικία,Τύπος, XXτ.μ.,€XXX.XXX,Περιοχή (Νομός)"
    """
    parts = [p.strip() for p in title_attr.split(",")]
    size  = next((p for p in parts if "τ.μ" in p), "")
    price = next((p for p in parts if "€" in p), "")
    return {"size": size, "price_from_title": price}


def parse_card(card, property_type: str) -> Optional[dict]:
    link = card.select_one("a.tile__link")
    if not link:
        return None

    href = link.get("href", "").strip()
    if not href:
        return None

    url = f"https://www.spitogatos.gr{href}" if href.startswith("/") else href
    title_attr = link.get("title", "")
    extra = parse_title_attr(title_attr)

    # Price — prefer DOM element, fall back to title attribute
    price_el = card.select_one("[class*='price']")
    price = price_el.get_text(strip=True) if price_el else extra.get("price_from_title", "")

    # Location
    loc_el = card.select_one("[class*='location'], [class*='area'], [class*='address']")
    location = loc_el.get_text(strip=True) if loc_el else ""

    # Size
    size_el = card.select_one("[class*='size'], [class*='sqm'], [class*='surface']")
    size = size_el.get_text(strip=True) if size_el else extra.get("size", "")

    # Description
    desc_el = card.select_one("[class*='description'], [class*='desc']")
    description = desc_el.get_text(strip=True)[:500] if desc_el else ""

    # Listing ID from URL path
    id_match = re.search(r"/aggelia/(\d+)", url)
    listing_id = id_match.group(1) if id_match else make_id(url)

    # Drop cards with no useful data
    if not price and not location:
        return None

    return {
        "id": listing_id,
        "url": url,
        "type": property_type,
        "title": title_attr,
        "price": price,
        "location": location,
        "size": size,
        "description": description,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": "github_actions",
    }


# ── Scraper ───────────────────────────────────────────────────────────────────

async def scrape_page(url: str, property_type: str) -> list[dict]:
    listings: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="el-GR",
            timezone_id="Europe/Athens",
            viewport={"width": 1366, "height": 768},
            # Lie about being a real desktop browser
            java_script_enabled=True,
            bypass_csp=False,
        )
        page = await context.new_page()

        # Apply playwright-stealth to mask headless signals
        await Stealth().apply_stealth_async(page)

        # Block images and fonts — faster loading, irrelevant for data
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot}",
            lambda route: route.abort(),
        )

        try:
            logger.info(f"Fetching: {url}")
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000,
            )

            status = response.status if response else "unknown"
            logger.info(f"HTTP status: {status}")

            if status and status >= 400:
                logger.warning(f"Non-2xx response ({status}) — bot block likely")
                return listings

            # Extra wait for JS-rendered content
            await page.wait_for_timeout(8000)

            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")
            cards = soup.select("article.ordered-element")

            if not cards:
                # Log a portion of raw HTML to help diagnose selector drift / blocks
                snippet = content[:2000].replace("\n", " ")
                logger.warning(
                    f"0 cards found on {url} — possible bot block or selector change. "
                    f"HTML snippet: {snippet}"
                )
            else:
                logger.info(f"Parsed {len(cards)} cards from {url}")

            for card in cards:
                try:
                    listing = parse_card(card, property_type)
                    if listing:
                        listings.append(listing)
                except Exception as exc:
                    logger.warning(f"Card parse error: {exc}")

        except Exception as exc:
            logger.error(f"Scrape failed for {url}: {exc}")
        finally:
            await browser.close()

    return listings


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> int:
    """Returns count of new listings pushed to Redis."""
    logger.info("=" * 60)
    logger.info("Spitogatos Scraper — GitHub Actions")
    logger.info(f"Target Redis: {VPS_TAILSCALE_IP}:{REDIS_PORT}")
    logger.info("=" * 60)

    # Redis health check
    try:
        r = get_redis()
        r.ping()
        seen_count = r.scard(REDIS_SEEN_KEY)
        logger.info(f"Redis OK — {seen_count} listings already seen")
    except Exception as exc:
        logger.error(f"Redis connection failed: {exc}")
        logger.error("Check VPS_TAILSCALE_IP, REDIS_PASSWORD, and Tailscale connectivity")
        return 0

    new_count = 0

    for source in URLS:
        listings = await scrape_page(source["url"], source["type"])
        logger.info(f"{source['type']}: {len(listings)} listings scraped")

        for listing in listings:
            lid = listing["id"]

            if r.sismember(REDIS_SEEN_KEY, lid):
                continue

            # Mark as seen and push to queue atomically-ish
            r.sadd(REDIS_SEEN_KEY, lid)
            r.lpush(REDIS_RESULTS_KEY, json.dumps(listing, ensure_ascii=False))
            new_count += 1

            # Human-readable stdout for GitHub Actions log
            print(f"\n{'='*60}")
            print(f"  NEW: {listing['type']}")
            print(f"  {listing.get('title', '(no title)')}")
            print(f"  Price   : {listing.get('price', 'N/A')}")
            print(f"  Location: {listing.get('location', 'N/A')}")
            print(f"  Size    : {listing.get('size', 'N/A')}")
            print(f"  URL     : {listing['url']}")
            print(f"{'='*60}\n")

    logger.info(f"Done — {new_count} new listings pushed to Redis")
    return new_count


if __name__ == "__main__":
    result = asyncio.run(run())
    # Exit 0 always — 0 new listings is not an error
    sys.exit(0)
