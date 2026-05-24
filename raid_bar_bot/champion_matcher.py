"""
Champion identification by portrait image.

How it works:
  1. Load champions_db.json  (built by build_db.py)
  2. Download the champion portrait from the site
  3. Compute pHash (perceptual hash — 64-bit fingerprint of the image)
  4. Find the closest hash in the database (Hamming distance)
  5. If distance ≤ threshold → match found → return English name

pHash is robust to:
  - Different image sizes (64px card vs 256px full portrait)
  - JPEG compression artifacts
  - Minor border/frame differences

Threshold = 12 bits out of 64: allows ~18% pixel difference.
Lower = stricter. Raise to 15 if getting too many misses.
"""

import io
import json
import logging
from pathlib import Path
from typing import Optional

import httpx
import imagehash
from PIL import Image

logger = logging.getLogger(__name__)

DB_FILE = Path("champions_db.json")
MATCH_THRESHOLD = 12  # max Hamming distance (0 = identical, 64 = opposite)

# In-memory cache — loaded once, reused for every match
_db_hashes: list[tuple[imagehash.ImageHash, str]] | None = None


def _load_db() -> list[tuple[imagehash.ImageHash, str]]:
    """Load and parse champions_db.json into a list of (hash_obj, name) pairs."""
    global _db_hashes
    if _db_hashes is not None:
        return _db_hashes

    if not DB_FILE.exists():
        raise FileNotFoundError(
            f"{DB_FILE} not found.\n"
            "Run:  python build_db.py\n"
            "This downloads all RSL champion portraits and builds the match database."
        )

    with open(DB_FILE, encoding="utf-8") as f:
        raw: dict[str, str] = json.load(f)

    _db_hashes = []
    for hash_str, name in raw.items():
        try:
            _db_hashes.append((imagehash.hex_to_hash(hash_str), name))
        except Exception:
            continue

    logger.info(f"Champion DB loaded: {len(_db_hashes)} entries")
    return _db_hashes


def db_loaded() -> bool:
    """True if champions_db.json exists and is non-empty."""
    return DB_FILE.exists() and DB_FILE.stat().st_size > 10


def find_by_bytes(img_bytes: bytes) -> Optional[str]:
    """
    Given raw image bytes, return the English champion name or None.

    Returns None if:
      - Image can't be decoded
      - Best match distance > MATCH_THRESHOLD (portrait not in DB)
    """
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        query = imagehash.phash(img)
    except Exception as exc:
        logger.debug(f"Could not hash image bytes: {exc}")
        return None

    db = _load_db()
    if not db:
        return None

    best_name: Optional[str] = None
    best_dist = MATCH_THRESHOLD + 1

    for db_hash, name in db:
        dist = query - db_hash
        if dist < best_dist:
            best_dist = dist
            best_name = name

    if best_name and best_dist <= MATCH_THRESHOLD:
        logger.debug(f"Image match: '{best_name}'  (Hamming distance = {best_dist})")
        return best_name

    logger.debug(f"No match found (best distance = {best_dist} > {MATCH_THRESHOLD})")
    return None


async def find_by_url(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Download portrait from URL and return English champion name, or None."""
    try:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        return find_by_bytes(resp.content)
    except Exception as exc:
        logger.debug(f"Could not fetch/match image {url}: {exc}")
        return None


async def identify_champion_cards(
    img_urls: list[str], client: httpx.AsyncClient
) -> list[str]:
    """
    Given a list of portrait URLs, return a list of English names.
    Unmatched portraits are skipped (not included in result).
    """
    results: list[str] = []
    for url in img_urls:
        name = await find_by_url(url, client)
        if name:
            results.append(name)
    return results
