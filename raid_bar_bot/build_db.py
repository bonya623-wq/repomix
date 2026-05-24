"""
One-time setup script — build the RSL champion portrait database.

Source: ayumilove.net (accessible worldwide, English names, in-game portraits)

Downloads every champion portrait, computes a perceptual hash (pHash) for each,
and saves:

    champions_db.json   { "hash_string": "Valkyrie", ... }

Run ONCE before starting the bot:
    python build_db.py

After that the bot uses this file to identify champions by their portrait
image — no translation needed, works 100% correctly.

Requires:  pip install imagehash Pillow httpx beautifulsoup4 lxml
"""

import asyncio
import io
import json
import logging
import re
import sys
from pathlib import Path

import httpx
import imagehash
from bs4 import BeautifulSoup
from PIL import Image

DB_FILE = Path("champions_db.json")
LOG_FILE = Path("build_db.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

ROOT = "https://ayumilove.net"
LIST_URL = "https://ayumilove.net/raid-shadow-legends-champion-tier-list/"


# ---------------------------------------------------------------------------
# Fetch champion list from ayumilove.net
# ---------------------------------------------------------------------------

async def fetch_champion_list(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """
    Return [(english_name, portrait_url), ...] for every RSL champion.

    ayumilove.net structure: champion cards inside the tier table,
    each card is an <a> link to the champion guide page, with an <img>
    whose alt= is the English champion name.
    """
    logger.info(f"Fetching champion list from {LIST_URL} ...")
    resp = await client.get(LIST_URL, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    champions: dict[str, str] = {}  # name → portrait_url

    # Strategy 1 — links to individual champion guide pages
    # href pattern: /raid-shadow-legends-NAME-skill-mastery-equip-guide/
    for a in soup.find_all("a", href=re.compile(r"raid-shadow-legends-.+-skill", re.I)):
        img = a.find("img")
        if not img:
            continue

        # Name from alt attribute
        name = (img.get("alt") or "").strip()

        # Fallback: derive name from href slug
        if not name:
            slug = a["href"].strip("/").split("/")[-1]
            # strip suffix -skill-mastery-equip-guide
            slug = re.sub(r"-skill.*$", "", slug)
            name = slug.replace("-", " ").title()

        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original", "")
        )
        if name and src and len(name) > 2 and name not in champions:
            if not src.startswith("http"):
                src = ROOT + src
            champions[name] = src

    # Strategy 2 — any <img> whose alt looks like a champion name + src has "champion" in path
    if not champions:
        logger.info("Strategy 1 found nothing — trying strategy 2 (img[alt] with champion path)")
        for img in soup.find_all("img", alt=True):
            alt = img["alt"].strip()
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src", "")
            )
            if not src:
                continue
            if not src.startswith("http"):
                src = ROOT + src
            if (
                3 <= len(alt) <= 45
                and re.search(r"[A-Z]", alt)          # at least one capital → name
                and "champion" in src.lower()
                and alt not in champions
            ):
                champions[alt] = src

    # Strategy 3 — broadest fallback: any <img alt> 3–45 chars with a plausible portrait URL
    if not champions:
        logger.info("Strategy 2 found nothing — trying strategy 3 (broad img scan)")
        for img in soup.find_all("img", alt=True):
            alt = img["alt"].strip()
            src = img.get("src") or img.get("data-src", "")
            if src and 3 <= len(alt) <= 45 and re.search(r"[A-Z]", alt):
                if not src.startswith("http"):
                    src = ROOT + src
                champions.setdefault(alt, src)

    result = list(champions.items())
    logger.info(f"Found {len(result)} champions")
    return result


# ---------------------------------------------------------------------------
# Image hashing
# ---------------------------------------------------------------------------

async def hash_portrait(client: httpx.AsyncClient, url: str) -> str | None:
    """Download portrait and return its pHash string, or None on failure."""
    try:
        resp = await client.get(url, timeout=20)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        return str(imagehash.phash(img))
    except Exception as exc:
        logger.debug(f"  hash failed {url}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def build() -> None:
    existing: dict[str, str] = {}
    if DB_FILE.exists():
        with open(DB_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        logger.info(f"Existing DB: {len(existing)} entries — will add new ones only")

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        champions = await fetch_champion_list(client)

        if not champions:
            logger.error(
                "No champions found on ayumilove.net.\n"
                "The page structure may have changed — check build_db.log."
            )
            return

        db: dict[str, str] = dict(existing)
        already_named = set(existing.values())

        for i, (name, portrait_url) in enumerate(champions, 1):
            if name in already_named:
                logger.info(f"[{i:>3}/{len(champions)}] SKIP  {name}")
                continue

            h = await hash_portrait(client, portrait_url)
            if h:
                db[h] = name
                logger.info(f"[{i:>3}/{len(champions)}] OK    {name}")
            else:
                logger.warning(f"[{i:>3}/{len(champions)}] FAIL  {name}  {portrait_url}")

            await asyncio.sleep(0.2)

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    logger.info(f"\nDone! {len(db)} champions saved to {DB_FILE}")


if __name__ == "__main__":
    asyncio.run(build())
