"""
Parser for https://www.raidmmo.com

Flow:
  1. Load champion catalogue (.card-list) → build name→rarity map
     #FF3300 = Mythic, #FFCC66 = Legendary, #FF00FF = Epic
  2. Click first champion in the list → click Search
  3. Collect all account rows from the results table
  4. For each account extract: nickname, price, 16 stats (by position),
     champion list (rarity resolved from catalogue map)

Stats order (by icon position, 1-indexed):
  1  account_age_days
  2  energy
  3  gems  (рубины)
  4  silver  (raw number → divide by 1M if ≥ 1_000_000)
  5  arena_classic_tokens
  6  arena_tag_tokens
  7  doom_tower_tokens
  8  ancient_shards
  9  void_shards
  10 sacred_shards
  11 chickens_5star
  12 legendary_tomes
  13 epic_tomes
  14 energy_refills
  15 arena_refill_tokens
  16 multi_battles
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

BASE_URL = "https://www.raidmmo.com"

# Rarity by background-color
_COLOR_MYTHIC    = "FF3300"
_COLOR_LEGENDARY = "FFCC66"
_COLOR_EPIC      = "FF00FF"

# Global cache: populated by fetch_listing, read by fetch_raidcheap_account
_ACCOUNT_CACHE: dict[str, "AccountData"] = {}


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class AccountData:
    account_id:          str
    price_usd:           float

    # Champions
    mythic_champions:    list[str] = field(default_factory=list)
    legendary_champions: list[str] = field(default_factory=list)

    # Stats (None = not present on this account)
    account_age_days:    Optional[int]   = None
    energy:              Optional[int]   = None
    gems:                Optional[int]   = None
    silver:              Optional[float] = None   # in millions
    arena_classic_tokens: Optional[int]  = None
    arena_tag_tokens:    Optional[int]   = None
    doom_tower_tokens:   Optional[int]   = None
    ancient_shards:      Optional[int]   = None
    void_shards:         Optional[int]   = None
    sacred_shards:       Optional[int]   = None
    chickens_5star:      Optional[int]   = None
    legendary_tomes:     Optional[int]   = None
    epic_tomes:          Optional[int]   = None
    energy_refills:      Optional[int]   = None
    arena_refill_tokens: Optional[int]   = None
    multi_battles:       Optional[int]   = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_stat(text: str) -> Optional[int]:
    """Parse stat text like '53.9K', '7.6M', '1,234' → int. None on failure."""
    if not text:
        return None
    t = text.strip().replace(",", "").replace(" ", "")
    try:
        if t.upper().endswith("K"):
            return int(float(t[:-1]) * 1_000)
        if t.upper().endswith("M"):
            return int(float(t[:-1]) * 1_000_000)
        return int(float(t))
    except (ValueError, IndexError):
        return None


def _silver_millions(raw: Optional[int]) -> Optional[float]:
    if raw is None:
        return None
    return raw / 1_000_000 if raw >= 1_000_000 else float(raw)


def _color_to_rarity(hex6: str) -> str:
    """Return 'mythic', 'legendary', 'epic', or '' for unknown."""
    c = hex6.upper().lstrip("#")
    if c == _COLOR_MYTHIC:
        return "mythic"
    if c == _COLOR_LEGENDARY:
        return "legendary"
    if c == _COLOR_EPIC:
        return "epic"
    return ""


# ---------------------------------------------------------------------------
# Main scraper class
# ---------------------------------------------------------------------------

class RaidCheapScraper:
    def __init__(self, context: "BrowserContext") -> None:
        self._context = context
        self._page: Optional["Page"] = None
        # name.lower() → rarity string
        self._rarity_map: dict[str, str] = {}

    async def __aenter__(self) -> "RaidCheapScraper":
        self._page = await self._context.new_page()
        return self

    async def __aexit__(self, *_) -> None:
        if self._page:
            await self._page.close()
            self._page = None

    # ── Step 1: build rarity map from catalogue ───────────────────────────

    async def _load_rarity_map(self) -> None:
        page = self._page
        await page.wait_for_selector(".card-list li[data-id]", timeout=20_000)

        entries = await page.evaluate("""
            () => Array.from(
                document.querySelectorAll('.card-list li[data-id]')
            ).map(li => {
                const style = li.querySelector('.imgcont')?.getAttribute('style') || '';
                const m = style.match(/background-color:\\s*#([0-9a-fA-F]*)/i);
                return {
                    name:  (li.querySelector('p.name')?.textContent || '').trim(),
                    color: m ? m[1].toUpperCase() : ''
                };
            })
        """)

        self._rarity_map = {}
        for e in entries:
            name = e.get("name", "").strip()
            if name:
                self._rarity_map[name.lower()] = _color_to_rarity(e.get("color", ""))

        mythic_count = sum(1 for v in self._rarity_map.values() if v == "mythic")
        leg_count    = sum(1 for v in self._rarity_map.values() if v == "legendary")
        logger.info(
            f"Rarity map: {len(self._rarity_map)} champions "
            f"({mythic_count} mythic, {leg_count} legendary)"
        )

    # ── Step 2: click first champion + Search, collect rows ──────────────

    async def _fetch_accounts_for_first_champion(self) -> list[dict]:
        """
        Click the first champion card, click Search, wait for results,
        return list of raw row dicts.
        """
        page = self._page

        # Click first card
        first_card = await page.query_selector(".card-list li[data-id]")
        if not first_card:
            logger.error("No champion cards found in .card-list")
            return []

        champ_name = await page.evaluate(
            "(li) => (li.querySelector('p.name')?.textContent || '').trim()",
            first_card,
        )
        await first_card.click()
        logger.info(f"Clicked champion: {champ_name}")
        await asyncio.sleep(0.5)

        # Click Search button
        search_btn = await page.query_selector("button.search-btn")
        if not search_btn:
            logger.error("Search button not found")
            return []
        await search_btn.click()
        logger.info("Search clicked — waiting for results…")

        # Wait for at least one result row
        try:
            await page.wait_for_selector(
                "ul.tbody-tr",
                timeout=30_000,
            )
        except Exception:
            # Fallback: give the page more time and try scraping anyway
            await asyncio.sleep(5)

        # Let all pages load (site uses AJAX append)
        await asyncio.sleep(3)

        # Extract rows
        rows = await self._extract_rows()
        logger.info(f"Extracted {len(rows)} account rows")
        return rows

    # ── Step 3: extract rows from DOM ────────────────────────────────────

    async def _extract_rows(self) -> list[dict]:
        """
        Parse every account row in the results table.
        Returns list of dicts with keys:
          nickname, price_usd, stats (list[str] len≤16), champion_names (list[str])
        """
        return await self._page.evaluate("""
            () => {
                const rows = [];

                // Each account is a <tr> that contains sub-rows or is a flat row.
                // raidmmo uses a two-column layout inside tbody:
                //   left td  = champion images (.tdimg-list)
                //   middle   = resource icons (.rescont)
                //   right    = nickname (.numcont) + price (.pricecont)

                const trs = document.querySelectorAll('ul.tbody-tr');
                for (const tr of trs) {
                    // Nickname
                    const numcont = tr.querySelector('.numcont');
                    if (!numcont) continue;
                    const nicknameEl = numcont.querySelector('span.num');
                    const nickname = nicknameEl ? nicknameEl.textContent.trim() : '';
                    if (!nickname) continue;

                    // Price
                    const priceEl = tr.querySelector('.price[data-pricetip-price]');
                    let priceUsd = 0;
                    if (priceEl) {
                        priceUsd = parseFloat(
                            priceEl.getAttribute('data-pricetip-price') || '0'
                        );
                    }
                    if (!priceUsd) {
                        // fallback: parse $NNN text
                        const priceText = (
                            tr.querySelector('.pricecont')?.textContent || ''
                        ).replace(/[^0-9.]/g, '');
                        priceUsd = parseFloat(priceText) || 0;
                    }

                    // Stats: collect <p> text from each .item span (in order)
                    const statEls = tr.querySelectorAll('.rescont .item p');
                    const stats = Array.from(statEls).map(p => p.textContent.trim());

                    // Champions: collect names from .tdimg-list
                    const champEls = tr.querySelectorAll('.tdimg-list p.name');
                    const championNames = Array.from(champEls).map(
                        p => p.textContent.trim()
                    ).filter(Boolean);

                    rows.push({ nickname, priceUsd, stats, championNames });
                }
                return rows;
            }
        """)

    # ── Step 4: build AccountData from a raw row ─────────────────────────

    def _build_account(self, row: dict) -> AccountData:
        nickname = row["nickname"]
        price    = float(row.get("priceUsd", 0))
        stats    = row.get("stats", [])
        champ_names = row.get("championNames", [])

        def _s(idx: int) -> Optional[int]:
            """Get stat by 0-based index."""
            if idx < len(stats):
                return _parse_stat(stats[idx])
            return None

        silver_raw = _s(3)

        mythics:     list[str] = []
        legendaries: list[str] = []
        for name in champ_names:
            rarity = self._rarity_map.get(name.lower(), "")
            if rarity == "mythic" and name not in mythics:
                mythics.append(name)
            elif rarity == "legendary" and name not in legendaries:
                legendaries.append(name)

        return AccountData(
            account_id           = f"rc_{nickname}",
            price_usd            = price,
            mythic_champions     = mythics,
            legendary_champions  = legendaries,
            account_age_days     = _s(0),
            energy               = _s(1),
            gems                 = _s(2),
            silver               = _silver_millions(silver_raw),
            arena_classic_tokens = _s(4),
            arena_tag_tokens     = _s(5),
            doom_tower_tokens    = _s(6),
            ancient_shards       = _s(7),
            void_shards          = _s(8),
            sacred_shards        = _s(9),
            chickens_5star       = _s(10),
            legendary_tomes      = _s(11),
            epic_tomes           = _s(12),
            energy_refills       = _s(13),
            arena_refill_tokens  = _s(14),
            multi_battles        = _s(15),
        )

    # ── Public API ────────────────────────────────────────────────────────

    async def fetch_listing(self) -> list[tuple[str, str, float]]:
        """
        Navigate to raidmmo.com, build rarity map, click first champion,
        search, collect all accounts.

        Returns list of (account_id, url, price_usd) sorted cheapest first.
        Populates _ACCOUNT_CACHE for fetch_raidcheap_account().
        """
        global _ACCOUNT_CACHE
        _ACCOUNT_CACHE = {}

        logger.info(f"Navigating to {BASE_URL} …")
        await self._page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)

        await self._load_rarity_map()

        rows = await self._fetch_accounts_for_first_champion()

        result: list[tuple[str, str, float]] = []
        seen_nicks: set[str] = set()

        for row in rows:
            nickname = row.get("nickname", "")
            if not nickname or nickname in seen_nicks:
                continue
            seen_nicks.add(nickname)

            acc = self._build_account(row)
            if acc.price_usd <= 0:
                continue

            _ACCOUNT_CACHE[acc.account_id] = acc
            result.append((acc.account_id, f"{BASE_URL}/{nickname}", acc.price_usd))

        result.sort(key=lambda x: x[2])
        logger.info(f"raidmmo.com: {len(result)} unique accounts collected")
        return result


# ---------------------------------------------------------------------------
# Called by main.py
# ---------------------------------------------------------------------------

async def fetch_raidcheap_list(scraper: RaidCheapScraper) -> list[tuple[str, str, float]]:
    return await scraper.fetch_listing()


async def fetch_raidcheap_account(
    account_id: str,
    detail_url: str,
    price_usd: float,
) -> Optional[AccountData]:
    """Return AccountData from cache (populated by fetch_listing)."""
    cached = _ACCOUNT_CACHE.get(account_id)
    if cached:
        return cached
    logger.warning(f"{account_id} not in cache — returning stub")
    return AccountData(account_id=account_id, price_usd=price_usd)
