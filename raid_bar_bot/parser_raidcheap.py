"""
Parser for https://raid-cheap.com

Uses Playwright to visually click champion cards and Search,
while intercepting the site's go.php JSON API responses for data.

Flow for each mythic champion:
  1. Click the champion card in .card-list  (li[data-id=ID])
  2. Click "Search" button
  3. Site auto-loads all result pages via search_append() AJAX
  4. We intercept every /go.php response to build the account list

Champion catalogue loaded via cat.php at the start of each scan.
Price field in go.php is CNY.  USD = price × 0.165  (site's own rate).
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import httpx
from playwright.async_api import Response

from parser import AccountData

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

BASE_URL    = "https://www.raidmmo.com"
CATEGORY_ID = "51"          # RSL Champions tab in cat.php

CNY_TO_USD  = 0.165

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

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
    """
    Opens a browser tab on raid-cheap.com, clicks mythic champion cards,
    clicks Search, and captures the go.php JSON responses.
    """

    def __init__(self, context: "BrowserContext") -> None:
        self._context = context
        self._color_by_name: dict[str, str] = {}
        self._mythic_champs: list[tuple[int, str]] = []
        self._page: Optional["Page"] = None

    async def __aenter__(self) -> "RaidCheapScraper":
        self._page = await self._context.new_page()
        return self

    async def __aexit__(self, *_) -> None:
        if self._page:
            await self._page.close()
            self._page = None

    # ── Champion catalogue ─────────────────────────────────────────────────────

    async def _load_champions(self) -> None:
        """Fetch champion list from cat.php using the active tab's category ID."""
        # Read category ID from the active nav tab on the page (avoids hardcoding)
        cat_id = await self._page.evaluate(
            "() => document.querySelector('.top-nav a.current')?.dataset?.id || '51'"
        )
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(
                f"{BASE_URL}/cat.php", params={"id": cat_id}, timeout=20
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
            f"cat.php: {len(champions)} champions, "
            f"{len(self._mythic_champs)} mythic"
        )

    # ── Click UI + intercept go.php ────────────────────────────────────────────

    async def _search_one_mythic(
        self, champ_id: int, champ_name: str
    ) -> dict[str, dict]:
        """
        Click champion card → Click Search → collect all go.php pages.
        The site auto-loads every page via search_append(); we just intercept.
        Returns {account_username: raw_api_item}.
        """
        page  = self._page
        found: dict[str, dict] = {}

        # Clear previous selection
        try:
            await page.click("button.clear-btn", timeout=4_000)
            await page.wait_for_timeout(400)
        except Exception:
            pass

        # Find the champion card and click it
        card = await page.query_selector(f".card-list li[data-id='{champ_id}']")
        if not card:
            logger.warning(f"Card not found for {champ_name} (id={champ_id})")
            return found

        await card.scroll_into_view_if_needed()
        await card.click()
        await page.wait_for_timeout(400)

        # Intercept go.php responses (the site calls it for every page automatically)
        collected: list[dict] = []
        total_pages: list[int] = [1]

        async def on_response(response: Response) -> None:
            if "/go.php" not in response.url:
                return
            try:
                data = await response.json()
            except Exception:
                return
            raw = data.get("data", {})
            if isinstance(raw, dict) and "totalpage" in raw:
                try:
                    total_pages[0] = max(total_pages[0], int(raw["totalpage"]))
                except Exception:
                    pass
            if data.get("code") == 1:
                collected.append(raw)

        page.on("response", on_response)

        # Click Search — triggers AJAX + recursive search_append()
        try:
            await page.click("button.search-btn", timeout=5_000)
        except Exception as e:
            logger.warning(f"Could not click Search for {champ_name}: {e}")
            page.remove_listener("response", on_response)
            return found

        # Wait until all pages arrive (search_append auto-paginates, max 60 s)
        for _ in range(120):
            await asyncio.sleep(0.5)
            if len(collected) >= total_pages[0]:
                await asyncio.sleep(0.8)   # buffer for the very last request
                break

        page.remove_listener("response", on_response)

        # Extract account items from all collected pages
        for raw in collected:
            if isinstance(raw, dict):
                items = [v for v in raw.values() if isinstance(v, dict) and "account" in v]
            elif isinstance(raw, list):
                items = [v for v in raw if isinstance(v, dict) and "account" in v]
            else:
                continue
            for item in items:
                acc = str(item.get("account", "")).strip()
                if acc and acc not in found:
                    found[acc] = item

        logger.info(
            f"  {champ_name}: {len(found)} accounts "
            f"({len(collected)}/{total_pages[0]} pages)"
        )
        return found

    # ── Build AccountData ──────────────────────────────────────────────────────

    def _build_account_data(
        self, acc_id: str, price_usd: float, item: dict
    ) -> AccountData:
        mythics:     list[str] = []
        legendaries: list[str] = []

        roles = item.get("role", [])
        if isinstance(roles, dict):
            roles = list(roles.values())

        for role in roles:
            if not isinstance(role, dict):
                continue
            name  = (role.get("en_name") or "").strip()
            if not name:
                continue
            color = self._color_by_name.get(name.lower(), "")
            if _is_red(color):
                if name not in mythics:
                    mythics.append(name)
            elif _is_gold(color):
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
        Navigate to the site, click every mythic champion, collect accounts.
        Returns (account_id, url, price_usd) sorted cheapest first.
        Populates _ACCOUNT_CACHE for fetch_raidcheap_account().
        """
        global _ACCOUNT_CACHE
        _ACCOUNT_CACHE = {}

        logger.info("Navigating to raid-cheap.com …")
        await self._page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)

        # Static page HTML may have stale cards — click the active Champions tab
        # to trigger cat.php AJAX reload and get the full current champion list.
        try:
            async with self._page.expect_response(
                lambda r: "cat.php" in r.url, timeout=10_000
            ):
                await self._page.click(".top-nav a.current", timeout=5_000)
            await self._page.wait_for_timeout(600)
            logger.debug("Card list refreshed via Champions tab click")
        except Exception as e:
            logger.warning(f"Could not refresh card list: {e}")

        await self._load_champions()

        all_raw: dict[str, dict] = {}

        for champ_id, champ_name in self._mythic_champs:
            logger.info(f"Searching: {champ_name} (id={champ_id})")
            batch = await self._search_one_mythic(champ_id, champ_name)
            new = sum(1 for k in batch if k not in all_raw)
            all_raw.update({k: v for k, v in batch.items() if k not in all_raw})
            logger.info(
                f"  → +{new} new  (total unique: {len(all_raw)})"
            )

        result: list[tuple[str, str, float]] = []

        for acc_name, item in all_raw.items():
            price_cny = float(item.get("price", 0) or 0)
            usd_price = round(price_cny * CNY_TO_USD, 2)
            if usd_price <= 0:
                continue
            acc_id   = f"rc_{acc_name}"
            url      = f"{BASE_URL}/{acc_name}"
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
    logger.warning(f"{account_id} not in cache — returning empty AccountData")
    return AccountData(
        account_id=account_id,
        price_usd=price_usd,
        mythic_champions=[],
        legendary_champions=[],
    )
