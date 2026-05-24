"""
Parser for https://raid-cheap.com — Chinese RSL account marketplace.

The site requires champion selection before showing any accounts.
We use Playwright to:
  1. Open the page (full browser, not a simple HTTP request)
  2. Click ALL champion portrait cards to select them
  3. Click "Search"
  4. Collect all account listings from the results
  5. Visit each account detail page and extract data

Chinese text is auto-translated to English via deep-translator.
"""

import io
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from parser import AccountData, _parse_number, _parse_silver_millions
from translator import translate_to_en, translate_champion_list

logger = logging.getLogger(__name__)

BASE_URL = "https://raid-cheap.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ── Optional OCR ──────────────────────────────────────────────────────────────
try:
    import pytesseract
    from PIL import Image
    _OCR = True
except ImportError:
    _OCR = False


# ---------------------------------------------------------------------------
# Playwright scraper class
# ---------------------------------------------------------------------------

class RaidCheapScraper:
    """Manages a Playwright browser session for raid-cheap.com."""

    def __init__(self) -> None:
        self._pw = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None

    async def __aenter__(self) -> "RaidCheapScraper":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._ctx = await self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ------------------------------------------------------------------
    # Step 1 — Select all champions and run Search
    # ------------------------------------------------------------------

    async def _select_all_champions_and_search(self, page: Page) -> bool:
        """
        Click every champion portrait card, then click Search.
        Returns True if at least one account result appeared.
        """
        await page.goto(BASE_URL, wait_until="networkidle", timeout=40_000)

        # The champion selection grid — try several selectors that match
        # what's visible in the screenshot (portrait image cards in a grid).
        card_selectors = [
            "td img",           # table-based grid
            ".champion-card",
            "[class*='champion'] img",
            "[class*='card'] img",
            ".hero img",
            "figure img",
            "ul li img",
            "div img[src*='champion']",
            "div img[src*='hero']",
        ]

        clicked = 0
        for sel in card_selectors:
            cards = await page.locator(sel).all()
            if not cards:
                continue
            logger.info(f"Clicking {len(cards)} champion cards via selector '{sel}'")
            for card in cards:
                try:
                    await card.click(timeout=2_000)
                    clicked += 1
                except Exception:
                    # Some cards may be off-screen; scroll into view first
                    try:
                        await card.scroll_into_view_if_needed()
                        await card.click(timeout=2_000)
                        clicked += 1
                    except Exception:
                        pass
            if clicked > 0:
                break

        if clicked == 0:
            logger.warning(
                "Could not click any champion cards on raid-cheap.com. "
                "Saving debug screenshot to raidcheap_debug.png"
            )
            await page.screenshot(path="raidcheap_debug.png")
            return False

        logger.info(f"Selected {clicked} champion cards")

        # Click the Search button (labelled "Search" in English per screenshot)
        search_selectors = [
            "button:has-text('Search')",
            "input[value='Search']",
            "button:has-text('搜索')",
            "#search-btn",
            ".search-btn",
            "button[type='submit']",
        ]
        for sel in search_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    break
            except Exception:
                continue

        await page.wait_for_load_state("networkidle", timeout=30_000)

        # Save a debug screenshot so you can verify results
        await page.screenshot(path="raidcheap_after_search.png")
        logger.info("Search done — screenshot saved to raidcheap_after_search.png")
        return True

    # ------------------------------------------------------------------
    # Step 2 — Parse search results page
    # ------------------------------------------------------------------

    async def _parse_results_page(self, page: Page) -> list[tuple[str, str, float]]:
        """
        Extract (account_id, detail_url, price_usd) from the search results.
        Handles pagination if a 'Next page' link exists.
        """
        results: dict[str, tuple[str, float]] = {}

        while True:
            content = await page.content()
            soup = BeautifulSoup(content, "lxml")

            # --- Find account listing links ---
            for a in soup.find_all("a", href=True):
                href: str = a["href"]
                m = re.search(
                    r"/(?:account|item|product|listing|detail|buy|order)/([A-Za-z0-9_-]{3,})",
                    href,
                )
                if not m:
                    # Try just /ID at end of path
                    m = re.search(r"/([A-Za-z0-9_-]{6,})/?$", href)
                if m:
                    raw_id = m.group(1)
                    if raw_id in ("search", "index", "page", "list"):
                        continue
                    if raw_id not in results:
                        full_url = href if href.startswith("http") else BASE_URL + href
                        price = _extract_price_from_tag(a)
                        results[raw_id] = (full_url, price)

            # --- Also look for [data-id] cards ---
            for card in soup.find_all(attrs={"data-id": True}):
                raw_id = card["data-id"].strip()
                if re.match(r"[A-Za-z0-9_-]{3,}", raw_id) and raw_id not in results:
                    price = _extract_price_from_tag(card)
                    url = f"{BASE_URL}/account/{raw_id}"
                    results[raw_id] = (url, price)

            # --- Pagination: follow "Next" link if present ---
            next_link = soup.find("a", string=re.compile(r"Next|下一页|›|»", re.I))
            if not next_link or not next_link.get("href"):
                break
            next_url = next_link["href"]
            if not next_url.startswith("http"):
                next_url = BASE_URL + next_url
            logger.info(f"Following pagination → {next_url}")
            await page.goto(next_url, wait_until="networkidle", timeout=20_000)

        if not results:
            logger.warning(
                "No account listings found in search results. "
                "Check raidcheap_after_search.png to see what the page looks like."
            )

        logger.info(f"raid-cheap.com search: {len(results)} accounts found")
        return [("rc_" + rid, url, price) for rid, (url, price) in results.items()]

    # ------------------------------------------------------------------
    # Public: fetch the listing
    # ------------------------------------------------------------------

    async def fetch_listing(self) -> list[tuple[str, str, float]]:
        """Open raid-cheap.com, select all champs, search, return (id, url, price)."""
        page = await self._ctx.new_page()
        try:
            ok = await self._select_all_champions_and_search(page)
            if not ok:
                return []
            return await self._parse_results_page(page)
        finally:
            await page.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_price_from_tag(tag) -> float:
    """Walk up the DOM to find a USD price near this tag."""
    candidates = [tag]
    p = getattr(tag, "parent", None)
    for _ in range(5):
        if p:
            candidates.append(p)
            p = getattr(p, "parent", None)

    for t in candidates:
        text = t.get_text() if hasattr(t, "get_text") else str(t)
        m = re.search(
            r"(?:USD|US\$|\$)\s*([0-9]+(?:\.[0-9]{1,2})?)"
            r"|([0-9]+(?:\.[0-9]{1,2})?)\s*(?:USD|US\$|\$)",
            text,
        )
        if m:
            try:
                return float(m.group(1) or m.group(2))
            except ValueError:
                pass
        # CNY fallback (rough /7.2 conversion)
        m2 = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|¥|CNY|RMB)", text)
        if m2:
            try:
                return round(float(m2.group(1)) / 7.2, 2)
            except ValueError:
                pass
    return 0.0


def _extract_resources(text: str) -> dict:
    def _int(pattern: str):
        m = re.search(pattern, text, re.I)
        if not m:
            return None
        v = _parse_number(m.group(1))
        return int(v) if v is not None else None

    def _float(pattern: str):
        m = re.search(pattern, text, re.I)
        return _parse_number(m.group(1)) if m else None

    silver_m = re.search(r"[Ss]ilver[:\s]+([0-9.,]+\s*[MmKk]?)", text)
    silver = _parse_silver_millions(silver_m.group(1)) if silver_m else None
    gems = _int(r"[Gg]ems?[:\s]+([0-9,]+)")
    energy = _float(r"[Ee]nergy[:\s]+([0-9.,]+\s*[MmKk]?)")
    cb_keys = _int(r"CB\s*[Kk]eys?[:\s]+([0-9]+)")
    brews = _int(r"[Bb]rews?[:\s]+([0-9]+)")

    age_m = re.search(r"(\d+)\s+days?\s+old|[Aa]ge[:\s]+(\d+)", text)
    age = int(age_m.group(1) or age_m.group(2)) if age_m else None

    heroes_m = re.search(r"[Tt]otal\s+[Hh]eroes?[:\s]+(\d+)|[Hh]eroes?[:\s]+(\d+)", text)
    heroes = int(heroes_m.group(1) or heroes_m.group(2)) if heroes_m else None

    tomes: dict[str, int] = {}
    for rarity in ("Rare", "Epic", "Legendary"):
        m = re.search(rf"(\d+)\s+{rarity}\s+[Tt]ome", text, re.I)
        if m:
            tomes[rarity] = int(m.group(1))

    shards: dict[str, int] = {}
    for stype in ("Ancient", "Void", "Sacred", "Epic", "Legendary"):
        m = re.search(rf"(\d+)\s+{stype}\s+[Ss]hard", text, re.I)
        if m:
            shards[stype] = int(m.group(1))

    chickens: dict[str, int] = {}
    for cm in re.finditer(r"([2-6])[★*]\s*[×xX](\d+)", text):
        chickens[f"{cm.group(1)}★"] = int(cm.group(2))

    return dict(
        silver=silver, gems=gems, energy=energy, cb_keys=cb_keys,
        brews=brews, account_age_days=age, total_heroes=heroes,
        tomes=tomes, shards=shards, chickens=chickens,
    )


async def _ocr_image(url: str, client: httpx.AsyncClient) -> str:
    if not _OCR:
        return ""
    try:
        resp = await client.get(url, timeout=20)
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        text_en = pytesseract.image_to_string(img, lang="eng")
        if len(text_en.strip()) > 30:
            return text_en
        text_cn = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return translate_to_en(text_cn) if text_cn.strip() else text_en
    except Exception as exc:
        logger.debug(f"OCR failed {url}: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Detail page fetch (uses plain httpx — detail pages are static HTML)
# ---------------------------------------------------------------------------

async def fetch_raidcheap_account(
    account_id: str, detail_url: str, price_usd: float
) -> Optional[AccountData]:
    """Fetch and parse one account detail page from raid-cheap.com."""
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers=HEADERS
        ) as client:
            resp = await client.get(detail_url)
            resp.raise_for_status()
            html = resp.text

            soup = BeautifulSoup(html, "lxml")
            raw_text = soup.get_text(separator="\n")
            translated = translate_to_en(raw_text)
            combined = translated

            # OCR account screenshots
            img_tags = soup.find_all("img", src=True)
            ocr_parts: list[str] = []
            for img in img_tags:
                src: str = img["src"]
                if any(s in src.lower() for s in ("logo", "icon", "avatar", "banner", "btn")):
                    continue
                full_src = src if src.startswith("http") else BASE_URL + src
                ocr_text = await _ocr_image(full_src, client)
                if ocr_text.strip():
                    ocr_parts.append(ocr_text)
            if ocr_parts:
                combined += "\n\n" + "\n\n".join(ocr_parts)

        # Champion lists — try regex on translated text
        mythics: list[str] = []
        legendaries: list[str] = []

        myth_m = re.search(r"Mythic[^:]*:([^\n]{5,200})", combined, re.I)
        if myth_m:
            mythics = [c.strip() for c in re.split(r"[,，•·]", myth_m.group(1)) if c.strip()]

        leg_m = re.search(r"Legendary[^:]*:([^\n]{5,500})", combined, re.I)
        if leg_m:
            legendaries = [
                c.strip()
                for c in re.split(r"[,，•·]", leg_m.group(1))
                if c.strip() and c.strip() not in mythics
            ]

        res = _extract_resources(combined)

        return AccountData(
            account_id=account_id,
            price_usd=price_usd,
            mythic_champions=mythics,
            legendary_champions=legendaries,
            **res,
        )

    except Exception as exc:
        logger.error(f"raid-cheap.com detail fetch failed for {account_id}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Public API used by main.py
# ---------------------------------------------------------------------------

async def fetch_raidcheap_list(scraper: "RaidCheapScraper") -> list[tuple[str, str, float]]:
    """Fetch (account_id, url, price) list using an existing RaidCheapScraper."""
    return await scraper.fetch_listing()
