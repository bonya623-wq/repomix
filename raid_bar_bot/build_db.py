"""
One-time setup script — build the RSL champion portrait database.

Downloads every champion portrait from raidcodex.com (English names),
computes a perceptual hash (pHash) for each, and saves:

    champions_db.json   { "hash_string": "Valkyrie", ... }

Run ONCE before starting the bot:
    python build_db.py

After that the bot uses this file to identify champions by their portrait
image — no translation needed, works 100% correctly.

Requires:  pip install imagehash Pillow httpx beautifulsoup4 lxml
"""

import argparse
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
CONFIG_FILE = Path("config.json")

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

# Primary source — raidcodex.com lists all RSL champions with English names
CODEX_ROOT = "https://raidcodex.com"
CODEX_LIST = "https://raidcodex.com/champions/"


# ---------------------------------------------------------------------------
# Fetch champion list
# ---------------------------------------------------------------------------

async def fetch_champion_list(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """
    Return [(english_name, portrait_url), ...] for every RSL champion.
    Tries multiple selectors because the site layout can change.
    """
    logger.info(f"Fetching champion list from {CODEX_LIST} ...")
    resp = await client.get(CODEX_LIST, timeout=40)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    champions: list[tuple[str, str]] = []

    # --- Strategy 1: cards with explicit name element + image ---
    for card in soup.select(
        ".champion-card, .champion-item, [class*='champion'], "
        "[class*='hero-card'], [class*='champ']"
    ):
        img = card.find("img")
        name_tag = card.find(
            ["h2", "h3", "h4", "span", "p", "div"],
            class_=re.compile(r"name|title|label", re.I),
        )
        if not name_tag:
            name_tag = card.find(["h2", "h3", "h4"])
        if img and name_tag:
            name = name_tag.get_text(strip=True)
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src", "")
            if name and src and len(name) > 2:
                if not src.startswith("http"):
                    src = CODEX_ROOT + src
                champions.append((name, src))

    # --- Strategy 2: <img alt="Champion Name"> inside <a href="/champions/..."> ---
    if not champions:
        for a in soup.find_all("a", href=re.compile(r"/champions/[^/]+/?$")):
            img = a.find("img")
            if not img:
                continue
            # Name from alt text or href slug
            name = (img.get("alt") or "").strip()
            if not name:
                slug = a["href"].rstrip("/").split("/")[-1]
                name = slug.replace("-", " ").title()
            src = img.get("src") or img.get("data-src", "")
            if name and src and len(name) > 2:
                if not src.startswith("http"):
                    src = CODEX_ROOT + src
                champions.append((name, src))

    # --- Strategy 3: any <img alt> that looks like a champion name ---
    if not champions:
        seen = set()
        for img in soup.find_all("img", alt=True):
            alt = img["alt"].strip()
            src = img.get("src") or img.get("data-src", "")
            # Filter out nav/logo images — champion names are 3-40 chars
            if 3 <= len(alt) <= 40 and src and alt not in seen:
                if not src.startswith("http"):
                    src = CODEX_ROOT + src
                seen.add(alt)
                champions.append((alt, src))

    # De-duplicate by name
    seen_names: set[str] = set()
    unique: list[tuple[str, str]] = []
    for name, src in champions:
        if name not in seen_names:
            seen_names.add(name)
            unique.append((name, src))

    logger.info(f"Found {len(unique)} champions on listing page")
    return unique


# ---------------------------------------------------------------------------
# Image hashing
# ---------------------------------------------------------------------------

async def hash_portrait(client: httpx.AsyncClient, url: str) -> str | None:
    """Download portrait and return its pHash string, or None on failure."""
    try:
        resp = await client.get(url, timeout=20)
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        return str(imagehash.phash(img))
    except Exception as exc:
        logger.debug(f"  hash failed {url}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_proxy(proxy_arg: str) -> dict | None:
    """Return httpx proxy dict from --proxy arg or config.json, or None."""
    if proxy_arg:
        return {"http://": proxy_arg, "https://": proxy_arg}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        p = cfg.get("proxy", {})
        server = p.get("server", "")
        if server:
            user = p.get("username", "")
            pw   = p.get("password", "")
            if user and pw:
                # inject credentials into URL: http://user:pass@host:port
                proto, rest = server.split("://", 1)
                url = f"{proto}://{user}:{pw}@{rest}"
            else:
                url = server
            return {"http://": url, "https://": url}
    return None


async def build(proxy_arg: str = "") -> None:
    existing: dict[str, str] = {}
    if DB_FILE.exists():
        with open(DB_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        logger.info(f"Existing DB: {len(existing)} entries — will add new ones only")

    proxies = _load_proxy(proxy_arg)
    if proxies:
        logger.info(f"Using proxy: {list(proxies.values())[0]}")

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, proxies=proxies) as client:
        champions = await fetch_champion_list(client)

        if not champions:
            logger.error(
                "No champions found. The raidcodex.com page structure may have changed.\n"
                "Check build_db.log and try again, or open raidcodex.com/champions/ manually\n"
                "to find the correct CSS selector and update fetch_champion_list()."
            )
            return

        db: dict[str, str] = dict(existing)
        already_named = set(existing.values())

        for i, (name, portrait_url) in enumerate(champions, 1):
            if name in already_named:
                logger.info(f"[{i:>3}/{len(champions)}] SKIP (already in DB): {name}")
                continue

            h = await hash_portrait(client, portrait_url)
            if h:
                db[h] = name
                logger.info(f"[{i:>3}/{len(champions)}] OK  {name}")
            else:
                logger.warning(f"[{i:>3}/{len(champions)}] FAIL  {name}  {portrait_url}")

            await asyncio.sleep(0.3)  # be polite to the server

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    logger.info(f"\nDone! Database: {len(db)} champions saved to {DB_FILE}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build RSL champion portrait DB")
    ap.add_argument(
        "--proxy",
        default="",
        metavar="URL",
        help="Proxy URL, e.g. http://user:pass@host:port  (overrides config.json)",
    )
    args = ap.parse_args()
    asyncio.run(build(args.proxy))
