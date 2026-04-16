"""
Spitogatos Monitor — runs on Mac (residential IP, bypasses CloudFront block)
Scrapes Crete listings every 30 min, pushes new ones to VPS Redis via Tailscale.
"""

import asyncio
import hashlib
import json
import re
import os
from datetime import datetime
from typing import Optional

import redis
from bs4 import BeautifulSoup
from loguru import logger
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# ── Config ────────────────────────────────────────────────
# Load from env vars (set in your shell or .env file — never hardcode)
VPS_TAILSCALE_IP  = os.environ.get("VPS_TAILSCALE_IP", "100.113.88.103")
REDIS_PORT        = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD    = os.environ["REDIS_PASSWORD"]

INTERVAL_MINUTES  = 30
REDIS_SEEN_KEY    = "spitogatos:seen_listings"
REDIS_RESULTS_KEY = "spitogatos:new_listings"

URLS = [
    {"url": "https://www.spitogatos.gr/pwliseis-katoikies/crete", "type": "Κατοικία"},
    {"url": "https://www.spitogatos.gr/pwliseis-oikopeda/crete",  "type": "Οικόπεδο"},
]
# ─────────────────────────────────────────────────────────

logger.add(
    "/tmp/spitogatos_mac.log",
    rotation="10 MB",
    retention="7 days",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
)


def get_redis():
    return redis.Redis(
        host=VPS_TAILSCALE_IP,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
    )


def make_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def parse_title_attr(title_attr: str) -> dict:
    """
    Spitogatos packs everything into the <a title> attribute.
    Format: "Πώληση,Κατοικία,Τύπος, XXτ.μ.,€XXX.XXX,Περιοχή (Νομός)"
    """
    parts = [p.strip() for p in title_attr.split(",")]
    size  = next((p for p in parts if "τ.μ" in p), "")
    price = next((p for p in parts if "€" in p), "")
    return {"size": size, "price_from_title": price}


async def scrape_page(url: str, property_type: str) -> list[dict]:
    listings = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="el-GR",
            timezone_id="Europe/Athens",
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        # Block images/fonts to speed up
        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}",
            lambda route: route.abort()
        )

        try:
            logger.info(f"Scraping {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(6000)

            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")
            cards = soup.select("article.ordered-element")
            logger.info(f"Found {len(cards)} cards on {url}")

            for card in cards:
                try:
                    listing = parse_card(card, property_type)
                    if listing:
                        listings.append(listing)
                except Exception as e:
                    logger.warning(f"Card parse error: {e}")

        except Exception as e:
            logger.error(f"Scrape error: {e}")
        finally:
            await browser.close()

    return listings


def parse_card(card, property_type: str) -> Optional[dict]:
    link = card.select_one("a.tile__link")
    if not link:
        return None

    href = link.get("href", "")
    if not href:
        return None

    url = f"https://www.spitogatos.gr{href}" if href.startswith("/") else href
    title_attr = link.get("title", "")

    # Parse size and price from title attribute
    extra = parse_title_attr(title_attr)

    # Price
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

    # Listing ID from URL
    listing_id = re.search(r"/aggelia/(\d+)", url)
    listing_id = listing_id.group(1) if listing_id else make_id(url)

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
        "scraped_at": datetime.utcnow().isoformat(),
    }


def format_message(listing: dict) -> str:
    icon = "🏠" if listing["type"] == "Κατοικία" else "🌍"
    lines = [f"{icon} *Νέα αγγελία — {listing['type']} στην Κρήτη*", ""]

    if listing.get("title"):
        lines.append(f"*{listing['title']}*")
    if listing.get("price"):
        lines.append(f"💰 {listing['price']}")
    if listing.get("location"):
        lines.append(f"📍 {listing['location']}")
    if listing.get("size"):
        lines.append(f"📐 {listing['size']}")
    if listing.get("description"):
        lines.append(f"\n{listing['description'][:400]}")

    lines.append(f"\n🔗 {listing['url']}")
    lines.append(f"🕐 {listing['scraped_at'][:16]} UTC")
    return "\n".join(lines)


async def run():
    print(f"\n🏠 Spitogatos Monitor (Mac)")
    print(f"   Κατοικίες + Οικόπεδα — Κρήτη")
    print(f"   Interval: every {INTERVAL_MINUTES} minutes")
    print(f"   VPS Redis: {VPS_TAILSCALE_IP}:{REDIS_PORT}\n")

    r = get_redis()

    try:
        r.ping()
        print("✅ Redis connection OK\n")
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        print("   Check VPS_TAILSCALE_IP and REDIS_PASSWORD")
        return

    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting check...")
            new_count = 0

            for source in URLS:
                listings = await scrape_page(source["url"], source["type"])
                print(f"  {source['type']}: {len(listings)} listings scraped")

                for listing in listings:
                    lid = listing["id"]

                    if r.sismember(REDIS_SEEN_KEY, lid):
                        continue

                    r.sadd(REDIS_SEEN_KEY, lid)
                    new_count += 1

                    print(f"\n{'='*60}")
                    print(f"  NEW: {listing['type']}")
                    print(f"  {listing.get('title', '')}")
                    print(f"  💰 {listing.get('price', 'N/A')}")
                    print(f"  📍 {listing.get('location', 'N/A')}")
                    print(f"  📐 {listing.get('size', 'N/A')}")
                    print(f"  🔗 {listing['url']}")
                    print(f"{'='*60}\n")

                    # Push to VPS Redis for Discord forwarding
                    r.lpush(REDIS_RESULTS_KEY, json.dumps(listing))

            print(f"✅ Done — {new_count} new. Next check in {INTERVAL_MINUTES} min.\n")

        except Exception as e:
            logger.error(f"Error: {e}")
            print(f"❌ Error: {e}")

        await asyncio.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(run())
