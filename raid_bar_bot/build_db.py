"""
One-time setup script — build the RSL champion portrait database.

Source: raidshadowlegends.fandom.com (official RSL wiki, globally accessible)
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

# Redirect from raid-shadow-legends.fandom.com → raidshadowlegends.fandom.com
# httpx follows it automatically; we point directly at the destination
FANDOM_API = "https://raidshadowlegends.fandom.com/api.php"


# ---------------------------------------------------------------------------
# MediaWiki API helpers
# ---------------------------------------------------------------------------

async def _api_get(client: httpx.AsyncClient, params: dict) -> dict:
    resp = await client.get(FANDOM_API, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


async def get_subcategories(client: httpx.AsyncClient, category: str) -> list[str]:
    """Return all direct subcategory names inside a given category."""
    subcats: list[str] = []
    params: dict = {
        "action":   "query",
        "list":     "categorymembers",
        "cmtitle":  f"Category:{category}",
        "cmtype":   "subcat",
        "cmlimit":  "500",
        "format":   "json",
    }
    while True:
        data = await _api_get(client, params)
        for item in data.get("query", {}).get("categorymembers", []):
            title = item.get("title", "")
            if title.startswith("Category:"):
                subcats.append(title[len("Category:"):])
        if "continue" not in data:
            break
        params.update(data["continue"])
        await asyncio.sleep(0.2)
    return subcats


async def get_pages_with_images(
    client: httpx.AsyncClient, category: str
) -> dict[str, str]:
    """Return {champion_name: thumbnail_url} for all pages in a category."""
    result: dict[str, str] = {}
    params: dict = {
        "action":      "query",
        "generator":   "categorymembers",
        "gcmtitle":    f"Category:{category}",
        "gcmtype":     "page",
        "gcmlimit":    "50",
        "prop":        "pageimages",
        "pithumbsize": "300",
        "format":      "json",
    }
    while True:
        data = await _api_get(client, params)
        for page in data.get("query", {}).get("pages", {}).values():
            name  = page.get("title", "").strip()
            thumb = page.get("thumbnail", {}).get("source", "")
            if name and thumb and 2 <= len(name) <= 50:
                result[name] = thumb
        if "continue" not in data:
            break
        params.update(data["continue"])
        await asyncio.sleep(0.2)
    return result


# ---------------------------------------------------------------------------
# Fetch all champions (two-level: parent category → subcategories → pages)
# ---------------------------------------------------------------------------

async def fetch_champion_list(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    champions: dict[str, str] = {}

    # Level 1 — try pages directly in Category:Champions
    logger.info("Checking Category:Champions for direct pages…")
    direct = await get_pages_with_images(client, "Champions")
    champions.update(direct)
    logger.info(f"  Direct pages: {len(direct)}")

    # Level 2 — get subcategories (factions) and pull pages from each
    logger.info("Fetching subcategories of Category:Champions…")
    subcats = await get_subcategories(client, "Champions")
    logger.info(f"  Found {len(subcats)} subcategories: {subcats[:5]} …")

    for subcat in subcats:
        pages = await get_pages_with_images(client, subcat)
        before = len(champions)
        champions.update(pages)
        added = len(champions) - before
        logger.info(f"  [{subcat}] +{added} champions (total {len(champions)})")
        await asyncio.sleep(0.1)

    logger.info(f"Total unique champions: {len(champions)}")
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
            logger.error(
                "No champions found.\n"
                "Open https://raidshadowlegends.fandom.com/wiki/Category:Champions\n"
                "and check what subcategories/pages are listed there."
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
                logger.warning(f"[{i:>3}/{len(champions)}] FAIL  {name}")

            await asyncio.sleep(0.2)

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    logger.info(f"\nDone! {len(db)} champions saved to {DB_FILE}")


if __name__ == "__main__":
    asyncio.run(build())
