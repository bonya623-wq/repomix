"""
Parser for https://raid-cheap.com

Uses the site's own JSON APIs directly — no browser/Playwright needed:

  GET cat.php?id=51
    → all RSL champions: [{id, en_name, border_color, role_img}, ...]

  GET go.php?id[]=CHAMP_ID&game_id=3&page=P&sort=price_asc
    → paginated accounts: {code:1, data:{totalpage:N, 0:{account,price,role,...}, ...}}

Price field is in CNY.  USD = price × 0.165  (same formula the site uses).
Champion names are returned in English (en_name) — no translation needed.
Champion rarity comes from border_color in cat.php data:
  #FF3300 (red)  → Mythic
  #FFCC66 (gold) → Legendary
"""

import asyncio
import logging
from typing import Optional

import httpx

from parser import AccountData

logger = logging.getLogger(__name__)

BASE_URL    = "https://raid-cheap.com"
GAME_ID     = "3"          # RSL in go.php
CATEGORY_ID = "51"         # RSL Champions tab in cat.php

CNY_TO_USD  = 0.165        # conversion rate hard-coded by the site

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer":          BASE_URL + "/",
    "X-Requested-With": "XMLHttpRequest",
}

# Module-level cache populated by fetch_listing, consumed by fetch_raidcheap_account
_ACCOUNT_CACHE: dict[str, AccountData] = {}


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _is_red(hex6: str) -> bool:
    """True for mythic orange-red (#FF3300 family)."""
    if len(hex6) < 6:
        return False
    r, g, b = int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)
    return r > 150 and r > g * 2 and r > b * 2


def _is_gold(hex6: str) -> bool:
    """True for legendary gold (#FFCC66 family)."""
    if len(hex6) < 6:
        return False
    r, g, b = int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)
    return r > 200 and g > 150 and b < 150 and not _is_red(hex6)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class RaidCheapScraper:
    """Fetches account listings via raid-cheap.com JSON APIs."""

    def __init__(self) -> None:
        # en_name.lower() → border_color (populated by _load_champions)
        self._color_by_name: dict[str, str] = {}
        # list of (champion_id, en_name) for mythic champions only
        self._mythic_champs: list[tuple[int, str]] = []

    async def __aenter__(self) -> "RaidCheapScraper":
        return self

    async def __aexit__(self, *_) -> None:
        pass

    # ── Champion catalogue ─────────────────────────────────────────────────────

    async def _load_champions(self, client: httpx.AsyncClient) -> None:
        """Load RSL champion catalogue from cat.php."""
        resp = await client.get(
            f"{BASE_URL}/cat.php", params={"id": CATEGORY_ID}, timeout=20
        )
        resp.raise_for_status()
        champions = resp.json().get("data", [])

        self._color_by_name = {}
        self._mythic_champs = []

        for c in champions:
            name  = (c.get("en_name") or "").strip()
            color = (c.get("border_color") or "").upper()
            if name:
                self._color_by_name[name.lower()] = color
            if name and _is_red(color):
                self._mythic_champs.append((int(c["id"]), name))

        logger.info(
            f"cat.php: {len(champions)} champions total, "
            f"{len(self._mythic_champs)} mythic"
        )

    # ── Account search ─────────────────────────────────────────────────────────

    async def _search_one_mythic(
        self,
        client: httpx.AsyncClient,
        champ_id: int,
        champ_name: str,
    ) -> dict[str, dict]:
        """
        Call go.php for one mythic champion and paginate through ALL results.
        Returns {account_username: raw_api_item}.
        """
        found: dict[str, dict] = {}
        page = 1

        while True:
            try:
                resp = await client.get(
                    f"{BASE_URL}/go.php",
                    params={
                        "id[]":     str(champ_id),
                        "game_id":  GAME_ID,
                        "page":     str(page),
                        "sort":     "price_asc",
                    },
                    timeout=30,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
            except Exception as exc:
                logger.warning(f"go.php error ({champ_name} p{page}): {exc}")
                break

            if data.get("code") != 1:
                break

            raw = data.get("data", {})

            # Determine total pages and extract account items
            if isinstance(raw, dict):
                try:
                    total_pages = int(raw.get("totalpage", 1))
                except Exception:
                    total_pages = 1
                items = [
                    v for v in raw.values()
                    if isinstance(v, dict) and "account" in v
                ]
            elif isinstance(raw, list):
                total_pages = 1
                items = [v for v in raw if isinstance(v, dict) and "account" in v]
            else:
                break

            for item in items:
                acc = str(item.get("account", "")).strip()
                if acc and acc not in found:
                    found[acc] = item

            logger.debug(f"  {champ_name}: page {page}/{total_pages}, +{len(items)}")

            if page >= total_pages:
                break
            page += 1
            await asyncio.sleep(0.2)

        return found

    # ── Build AccountData from raw API item ────────────────────────────────────

    def _build_account_data(self, acc_id: str, price_usd: float, item: dict) -> AccountData:
        """Extract champion lists from go.php role data using cat.php colour lookup."""
        mythics:    list[str] = []
        legendaries: list[str] = []

        roles = item.get("role", [])
        if isinstance(roles, dict):
            roles = list(roles.values())

        for role in roles:
            if not isinstance(role, dict):
                continue
            name  = (role.get("en_name") or "").strip()
            count = int((role.get("pivot") or {}).get("num", 1))
            if not name:
                continue
            color = self._color_by_name.get(name.lower(), "")
            if _is_red(color):
                for _ in range(count):
                    if name not in mythics:
                        mythics.append(name)
            elif _is_gold(color):
                for _ in range(count):
                    if name not in legendaries:
                        legendaries.append(name)

        return AccountData(
            account_id=acc_id,
            price_usd=price_usd,
            mythic_champions=mythics,
            legendary_champions=legendaries,
        )

    # ── Public ─────────────────────────────────────────────────────────────────

    async def fetch_listing(self) -> list[tuple[str, str, float]]:
        """
        Search all mythic champions via go.php.
        Returns (account_id, url, price_usd) sorted cheapest first.
        Populates _ACCOUNT_CACHE for use by fetch_raidcheap_account().
        """
        global _ACCOUNT_CACHE
        _ACCOUNT_CACHE = {}

        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
            await self._load_champions(client)

            all_raw: dict[str, dict] = {}

            for champ_id, champ_name in self._mythic_champs:
                logger.info(f"Searching: {champ_name} (id={champ_id})")
                batch = await self._search_one_mythic(client, champ_id, champ_name)
                new = sum(1 for k in batch if k not in all_raw)
                all_raw.update({k: v for k, v in batch.items() if k not in all_raw})
                logger.info(
                    f"  {champ_name}: {len(batch)} accounts (+{new} new, "
                    f"total unique: {len(all_raw)})"
                )
                await asyncio.sleep(0.3)

        result: list[tuple[str, str, float]] = []

        for acc_name, item in all_raw.items():
            price_cny = float(item.get("price", 0) or 0)
            usd_price = round(price_cny * CNY_TO_USD, 2)
            if usd_price <= 0:
                continue

            acc_id = f"rc_{acc_name}"
            url    = f"{BASE_URL}/{acc_name}"

            acc_data = self._build_account_data(acc_id, usd_price, item)
            _ACCOUNT_CACHE[acc_id] = acc_data

            result.append((acc_id, url, usd_price))

        result.sort(key=lambda x: x[2])
        logger.info(f"raid-cheap.com: {len(result)} unique accounts")
        return result


# ---------------------------------------------------------------------------
# Called by main.py
# ---------------------------------------------------------------------------

async def fetch_raidcheap_list(scraper: RaidCheapScraper) -> list[tuple[str, str, float]]:
    return await scraper.fetch_listing()


async def fetch_raidcheap_account(
    account_id: str, detail_url: str, price_usd: float
) -> Optional[AccountData]:
    """Return AccountData from cache (populated by fetch_listing)."""
    cached = _ACCOUNT_CACHE.get(account_id)
    if cached:
        return cached

    # Fallback if called before fetch_listing or for an unknown ID
    logger.warning(f"{account_id} not in cache — returning empty AccountData")
    return AccountData(
        account_id=account_id,
        price_usd=price_usd,
        mythic_champions=[],
        legendary_champions=[],
    )
