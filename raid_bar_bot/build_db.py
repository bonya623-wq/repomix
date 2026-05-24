"""
One-time setup script — build the RSL champion portrait database.

Source: raid-shadow-legends.fandom.com (official RSL wiki, globally accessible)
Uses the MediaWiki API — no scraping, no proxy needed.

Downloads every champion portrait, computes a perceptual hash (pHash) for each,
and saves:

    champions_db.json   { "hash_string": "Valkyrie", ... }

Run ONCE before starting the bot:
    python build_db.py

Requires:  pip install imagehash Pillow httpx
"""

import asyncio
import io
import json
import logging
import sys
from pathlib import Path

import httpx
import imagehash
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

FANDOM_API = "https://raid-shadow-legends.fandom.com/api.php"


# ---------------------------------------------------------------------------
# Fetch champion list via MediaWiki API
# ---------------------------------------------------------------------------

async def fetch_champion_list(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """
    Returns [(english_name, portrait_url), ...] for every RSL champion.

    Uses the MediaWiki generator API to list all pages in Category:Champions
    and fetch their lead image (the in-game portrait) in one paginated request.
    """
    champions: dict[str, str] = {}
    params: dict = {
        "action":     "query",
        "generator":  "categorymembers",
        "gcmtitle":   "Category:Champions",
        "gcmtype":    "page",
        "gcmlimit":   "50",
        "prop":       "pageimages",
        "pithumbsize": "300",
        "format":     "json",
    }

    page_num = 0
    while True:
        page_num += 1
        logger.info(f"  API page {page_num} — {len(champions)} champions so far…")
        resp = await client.get(FANDOM_API, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("query", {}).get("pages", {}).values():
            name  = page.get("title", "").strip()
            thumb = page.get("thumbnail", {}).get("source", "")
            # Skip category/template pages; champion names are 2–40 chars
            if name and thumb and 2 <= len(name) <= 40:
                champions[name] = thumb

        if "continue" not in data:
            break
        params.update(data["continue"])
        await asyncio.sleep(0.3)

    logger.info(f"Total champions found: {len(champions)}")
    return list(champions.items())


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
            logger.error("No champions found — check your internet connection.")
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
                logger.warning(f"[{i:>3}/{len(champions)}] FAIL  {name}")

            await asyncio.sleep(0.2)

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    logger.info(f"\nDone! {len(db)} champions saved to {DB_FILE}")


if __name__ == "__main__":
    asyncio.run(build())
