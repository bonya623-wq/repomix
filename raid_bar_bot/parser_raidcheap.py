"""
Parser for https://www.raidmmo.com

Uses Playwright to visually click champion cards and Search,
intercepting the site's go.php JSON API responses for data extraction.

Flow for each mythic champion:
  1. Click the champion card in .card-list  (li[data-id=ID])
  2. Click "Search" button
  3. Site auto-loads all result pages via search_append() AJAX
  4. We intercept every /go.php response to build the account list

Champion catalogue loaded by clicking the Champions tab and intercepting
the cat.php AJAX response — works regardless of category IDs used by the site.

Price field in go.php is in USD (raidmmo.com stores prices in USD).
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from playwright.async_api import Response

from parser import AccountData

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

BASE_URL = "https://www.raidmmo.com"

_ACCOUNT_CACHE: dict[str, AccountData] = {}


# ---------------------------------------------------------------------------
# Colour helpers  (border_color from cat.php)
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
    Opens a browser tab on raidmmo.com, clicks mythic champion cards,
    clicks Search, and captures go.php JSON responses.
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
        """
        Read champion data directly from the DOM (.card-list li[data-id]).
        The page HTML already contains all cards with border_color in inline style.
        No cat.php AJAX needed.
        """
        page = self._page

        await page.wait_for_selector(".card-list li[data-id]", timeout=15_000)

        champs = await page.evaluate("""
            () => Array.from(
                document.querySelectorAll('.card-list li[data-id]')
            ).map(li => {
                const style = li.querySelector('.imgcont')?.getAttribute('style') || '';
                const m = style.match(/background-color:\\s*#([0-9a-fA-F]{6})/i);
                return {
                    id:           li.dataset.id,
                    en_name:      (li.querySelector('p.name')?.textContent || '').trim(),
                    border_color: m ? m[1].toUpperCase() : ''
                };
            })
        """)

        self._color_by_name = {}
        self._mythic_champs = []

        for c in champs:
            name  = c.get("en_name", "").strip()
            color = c.get("border_color", "")
            if name:
                self._color_by_name[name.lower()] = color
            if name and _is_red(color):
                try:
                    self._mythic_champs.append((int(c["id"]), name))
                except (ValueError, KeyError):
                    pass

        logger.info(
            f"DOM: {len(champs)} champions loaded, "
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

        try:
            await page.click("button.clear-btn", timeout=4_000)
            await page.wait_for_timeout(400)
        except Exception:
            pass

        card = await page.query_selector(f".card-list li[data-id='{champ_id}']")
        if not card:
            logger.warning(f"Card not found for {champ_name} (id={champ_id})")
            return found

        await card.scroll_into_view_if_needed()
        await card.click()
        await page.wait_for_timeout(400)

        collected:          list[dict] = []
        total_pages:        list[int]   = [1]
        last_response_at:   list[float] = [0.0]

        async def on_response(response: Response) -> None:
            if "/go.php" not in response.url:
                return
            last_response_at[0] = asyncio.get_event_loop().time()
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

        try:
            await page.click("button.search-btn", timeout=5_000)
        except Exception as e:
            logger.warning(f"Could not click Search for {champ_name}: {e}")
            page.remove_listener("response", on_response)
            return found

        start = asyncio.get_event_loop().time()
        for _ in range(120):
            await asyncio.sleep(0.5)
            now = asyncio.get_event_loop().time()

            # All expected pages arrived
            if len(collected) >= total_pages[0] and last_response_at[0] > 0:
                await asyncio.sleep(0.5)
                break

            # Got a response but nothing more for 3 s → done
            if last_response_at[0] > 0 and (now - last_response_at[0]) > 3.0:
                break

            # No response at all within 10 s → probably no results
            if last_response_at[0] == 0 and (now - start) > 10.0:
                break

        page.remove_listener("response", on_response)

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
        Navigate to raidmmo.com, click every mythic champion, collect accounts.
        Returns (account_id, url, price_usd) sorted cheapest first.
        Populates _ACCOUNT_CACHE for fetch_raidcheap_account().
        """
        global _ACCOUNT_CACHE
        _ACCOUNT_CACHE = {}

        logger.info(f"Navigating to {BASE_URL} …")
        await self._page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)

        await self._load_champions()

        all_raw: dict[str, dict] = {}

        for champ_id, champ_name in self._mythic_champs:
            logger.info(f"Searching: {champ_name} (id={champ_id})")
            batch = await self._search_one_mythic(champ_id, champ_name)
            new = sum(1 for k in batch if k not in all_raw)
            all_raw.update({k: v for k, v in batch.items() if k not in all_raw})
            logger.info(f"  → +{new} new  (total unique: {len(all_raw)})")

        result: list[tuple[str, str, float]] = []

        for acc_name, item in all_raw.items():
            # raidmmo.com stores prices in USD
            price_usd = round(float(item.get("price", 0) or 0), 2)
            if price_usd <= 0:
                continue
            acc_id   = f"rc_{acc_name}"
            url      = f"{BASE_URL}/{acc_name}"
            acc_data = self._build_account_data(acc_id, price_usd, item)
            _ACCOUNT_CACHE[acc_id] = acc_data
            result.append((acc_id, url, price_usd))

        result.sort(key=lambda x: x[2])
        logger.info(f"raidmmo.com: {len(result)} unique accounts")
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
