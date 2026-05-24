"""
Parser for https://raid-cheap.com

Strategy:
  - Click each MYTHIC champion card one at a time
  - After each click: press Search → collect accounts → press Clear
  - Deduplicate across all searches (one account may have several mythics)
  - Return list sorted cheapest first

Why mythics only:
  - AND-logic search: selecting multiple heroes at once returns 0 results
    unless an account has ALL of them
  - Mythic accounts are the ones worth reselling
  - ~30-40 mythics → ~30-40 searches (~2-3 min per cycle)
"""

import asyncio
import io
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from parser import AccountData, _parse_number, _parse_silver_millions
from translator import translate_to_en
import champion_matcher

logger = logging.getLogger(__name__)

BASE_URL = "https://raid-cheap.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

try:
    import pytesseract
    from PIL import Image
    _OCR = True
except ImportError:
    _OCR = False


# ---------------------------------------------------------------------------
# Playwright scraper
# ---------------------------------------------------------------------------

class RaidCheapScraper:

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
    # Find mythic cards
    # ------------------------------------------------------------------

    async def _find_mythic_cards(self, page: Page) -> list:
        """
        Return a list of card descriptors for all MYTHIC champion cards.

        Strategy (in priority order):
          1. Keyword match — any attribute/class contains 'mythic', 'rarity5', etc.
          2. Red-border detection — getComputedStyle shows red-ish color on element
             or up to 3 ancestor elements.

        On failure saves raidcheap_debug.html + raidcheap_mythic_not_found.png.
        """
        # Scroll down to trigger any lazy-loaded content, then return to top
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.8)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # Wait for champion images to load (jQuery renders cards async)
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('img').length > 5",
                timeout=15000,
            )
        except Exception:
            pass
        await asyncio.sleep(2.0)

        coords: list[dict] = await page.evaluate("""
            () => {
                // ── colour helpers ──────────────────────────────────────────
                const isRedish = (str) => {
                    if (!str || str === 'none' || str === 'transparent') return false;
                    // rgb() / rgba() — red must dominate and be bright enough
                    const rgbs = str.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/g) || [];
                    for (const rgb of rgbs) {
                        const vals = rgb.match(/\\d+/g).map(Number);
                        const r = vals[0], g = vals[1], b = vals[2];
                        if (r > 100 && r > g + 50 && r > b + 50) return true;
                    }
                    // hex: #c00, #cc0000, #d00, #e00, #f00, #ff0000…
                    if (/#[cCdDeEfF][0-5][0-5]([0-9a-fA-F]{3})?/.test(str)) return true;
                    // keyword 'red'
                    if (/\\bred\\b/i.test(str)) return true;
                    return false;
                };

                const getRedProp = (el) => {
                    if (!el) return null;
                    try {
                        const cs = window.getComputedStyle(el);
                        const props = [
                            cs.borderColor, cs.borderTopColor, cs.borderRightColor,
                            cs.borderBottomColor, cs.borderLeftColor,
                            cs.outlineColor, cs.boxShadow,
                            el.getAttribute('style') || '',
                        ];
                        return props.find(v => isRedish(v)) || null;
                    } catch(e) { return null; }
                };

                // ── keyword helpers ─────────────────────────────────────────
                const MYTHIC_KW = [
                    'mythic', 'myth', 'rarity5', 'rarity-5', 'tier5', 'tier-5',
                    'rank5', 'rank-5', 'ss-rank', 'ssrank', '神话', 'divine',
                ];
                const hasMythicKeyword = (el) => {
                    if (!el) return false;
                    const names = el.getAttributeNames ? el.getAttributeNames() : [];
                    const attrBlob = names.map(n => el.getAttribute(n) || '').join(' ').toLowerCase()
                        + ' ' + (el.className || '').toString().toLowerCase()
                        + ' ' + (el.id || '').toLowerCase();
                    return MYTHIC_KW.some(kw => attrBlob.includes(kw));
                };

                // ── scan all visible elements ───────────────────────────────
                const results = [];
                const seen = new Set();

                for (const el of document.querySelectorAll('*')) {
                    const r = el.getBoundingClientRect();
                    // Size filter: card-like (30–350 px wide, 30–400 px tall)
                    if (!r.width || r.width < 30 || r.width > 350
                                 || r.height < 30 || r.height > 400) continue;
                    // Must be near / inside the viewport
                    if (r.top > window.innerHeight + 200 || r.bottom < -50) continue;

                    let reason = null;

                    // Priority 1: mythic keyword in any attribute
                    if (hasMythicKeyword(el) || hasMythicKeyword(el.parentElement)) {
                        reason = 'keyword';
                    }
                    // Priority 2: red CSS on element or up to 3 ancestors
                    if (!reason) {
                        let cur = el;
                        for (let i = 0; i < 3 && cur; i++, cur = cur.parentElement) {
                            const prop = getRedProp(cur);
                            if (prop) {
                                reason = 'red@' + i + ':' + prop.slice(0, 30);
                                break;
                            }
                        }
                    }

                    if (!reason) continue;

                    // De-duplicate by 20 px grid
                    const key = Math.round(r.x / 20) + ',' + Math.round(r.y / 20);
                    if (seen.has(key)) continue;
                    seen.add(key);

                    results.push({
                        x: r.x + r.width  / 2,
                        y: r.y + r.height / 2,
                        w: r.width, h: r.height,
                        tag: el.tagName,
                        cls: (el.className || '').toString().slice(0, 80),
                        reason,
                    });
                }
                results.sort((a, b) => a.y - b.y || a.x - b.x);
                return results;
            }
        """)

        if coords:
            sample = [(c["tag"], c["cls"][:30], f"{c['w']:.0f}x{c['h']:.0f}", c["reason"])
                      for c in coords[:5]]
            logger.info(f"Found {len(coords)} mythic cards. Sample: {sample}")
            return coords

        # Nothing found — save comprehensive debug artefacts
        await page.screenshot(path="raidcheap_mythic_not_found.png", full_page=True)
        html = await page.content()
        with open("raidcheap_debug.html", "w", encoding="utf-8") as _f:
            _f.write(html)
        logger.error(
            f"No mythic cards found (URL: {page.url}, HTML: {len(html)} bytes). "
            "Saved raidcheap_mythic_not_found.png and raidcheap_debug.html — "
            "open the HTML in a browser to inspect the page structure."
        )
        return []

    # ------------------------------------------------------------------
    # Click one card → Search → collect → Clear
    # ------------------------------------------------------------------

    async def _click_search_btn(self, page: Page) -> bool:
        for sel in [
            "button:has-text('Search')",
            "input[value='Search']",
            "button:has-text('搜索')",
            ".search-btn",
            "button[type='submit']",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    return True
            except Exception:
                continue
        logger.warning("Search button not found")
        return False

    async def _click_clear_btn(self, page: Page) -> None:
        for sel in [
            "button:has-text('Clear')",
            "button:has-text('清除')",
            "button:has-text('Reset')",
            ".clear-btn",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                    return
            except Exception:
                continue
        # Fallback: reload the page to reset selection
        logger.debug("Clear button not found — reloading page to reset")
        await page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)

    async def _collect_results(self, page: Page) -> dict[str, tuple[str, float]]:
        """
        Walk through all result pages and return {account_id: (url, price)}.
        """
        results: dict[str, tuple[str, float]] = {}

        while True:
            content = await page.content()
            soup = BeautifulSoup(content, "lxml")

            # --- Find account links ---
            for a in soup.find_all("a", href=True):
                href: str = a["href"]
                # Typical patterns: /account/ID, /item/ID, /product/ID, /buy/ID
                m = re.search(
                    r"/(?:account|item|product|listing|detail|buy|order)/([A-Za-z0-9_-]{3,})",
                    href,
                )
                if not m:
                    m = re.search(r"/([A-Za-z0-9_-]{6,})/?$", href)
                if m:
                    rid = m.group(1)
                    if rid in ("search", "index", "page", "list", "filter"):
                        continue
                    if rid not in results:
                        full = href if href.startswith("http") else BASE_URL + href
                        price = _price_from_tag(a)
                        results[rid] = (full, price)

            # --- [data-id] cards ---
            for card in soup.find_all(attrs={"data-id": True}):
                rid = card["data-id"].strip()
                if re.match(r"[A-Za-z0-9_-]{3,}", rid) and rid not in results:
                    results[rid] = (f"{BASE_URL}/account/{rid}", _price_from_tag(card))

            # --- Pagination ---
            next_a = soup.find("a", string=re.compile(r"Next|下一页|›|»", re.I))
            if not next_a or not next_a.get("href"):
                break
            next_url = next_a["href"]
            if not next_url.startswith("http"):
                next_url = BASE_URL + next_url
            await page.goto(next_url, wait_until="networkidle", timeout=20_000)

        return results

    async def _search_one_mythic(
        self, page: Page, card: dict, index: int, total: int
    ) -> dict[str, tuple[str, float]]:
        """Click one mythic card (by centre coordinates), search, collect, clear."""
        try:
            await page.mouse.click(card["x"], card["y"])
        except Exception as e:
            logger.warning(f"Could not click card {index}: {e}")
            return {}

        ok = await self._click_search_btn(page)
        if not ok:
            await self._click_clear_btn(page)
            return {}

        await page.wait_for_load_state("networkidle", timeout=20_000)
        results = await self._collect_results(page)
        logger.info(f"  Mythic {index}/{total}: {len(results)} accounts found")

        # Go back to selection page for next mythic
        await self._click_clear_btn(page)

        return results

    # ------------------------------------------------------------------
    # Public: full listing fetch
    # ------------------------------------------------------------------

    async def fetch_listing(self) -> list[tuple[str, str, float]]:
        """
        Search raid-cheap.com mythic by mythic.
        Returns (account_id, detail_url, price_usd) sorted cheapest first.
        """
        page = await self._ctx.new_page()
        try:
            await page.goto(BASE_URL, wait_until="networkidle", timeout=40_000)

            mythic_cards = await self._find_mythic_cards(page)
            if not mythic_cards:
                logger.error("No mythic cards found — check raidcheap_mythic_not_found.png")
                return []

            logger.info(f"Starting mythic-by-mythic search ({len(mythic_cards)} mythics)")
            all_results: dict[str, tuple[str, float]] = {}

            for i, card in enumerate(mythic_cards, 1):
                # After Clear the page reloads — re-detect cards to keep coords fresh
                if i > 1:
                    fresh = await self._find_mythic_cards(page)
                    if i - 1 < len(fresh):
                        card = fresh[i - 1]

                batch = await self._search_one_mythic(page, card, i, len(mythic_cards))
                for rid, (url, price) in batch.items():
                    if rid not in all_results:
                        all_results[rid] = (url, price)

                await asyncio.sleep(1)

            final = [
                ("rc_" + rid, url, price)
                for rid, (url, price) in all_results.items()
            ]
            final.sort(key=lambda x: x[2])  # cheapest first
            logger.info(
                f"raid-cheap.com: {len(final)} unique accounts across "
                f"{len(mythic_cards)} mythic searches"
            )
            return final

        finally:
            await page.close()


# ---------------------------------------------------------------------------
# Price helper
# ---------------------------------------------------------------------------

def _price_from_tag(tag) -> float:
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
        m2 = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|¥|CNY|RMB)", text)
        if m2:
            try:
                return round(float(m2.group(1)) / 7.2, 2)
            except ValueError:
                pass
    return 0.0


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------

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


def _extract_resources(text: str) -> dict:
    def _int(pat):
        m = re.search(pat, text, re.I)
        if not m:
            return None
        v = _parse_number(m.group(1))
        return int(v) if v is not None else None

    def _flt(pat):
        m = re.search(pat, text, re.I)
        return _parse_number(m.group(1)) if m else None

    sm = re.search(r"[Ss]ilver[:\s]+([0-9.,]+\s*[MmKk]?)", text)
    silver = _parse_silver_millions(sm.group(1)) if sm else None
    gems = _int(r"[Gg]ems?[:\s]+([0-9,]+)")
    energy = _flt(r"[Ee]nergy[:\s]+([0-9.,]+\s*[MmKk]?)")
    cb_keys = _int(r"CB\s*[Kk]eys?[:\s]+([0-9]+)")
    brews = _int(r"[Bb]rews?[:\s]+([0-9]+)")

    am = re.search(r"(\d+)\s+days?\s+old|[Aa]ge[:\s]+(\d+)", text)
    age = int(am.group(1) or am.group(2)) if am else None

    hm = re.search(r"[Tt]otal\s+[Hh]eroes?[:\s]+(\d+)|[Hh]eroes?[:\s]+(\d+)", text)
    heroes = int(hm.group(1) or hm.group(2)) if hm else None

    tomes: dict[str, int] = {}
    for r in ("Rare", "Epic", "Legendary"):
        m = re.search(rf"(\d+)\s+{r}\s+[Tt]ome", text, re.I)
        if m:
            tomes[r] = int(m.group(1))

    shards: dict[str, int] = {}
    for s in ("Ancient", "Void", "Sacred", "Epic", "Legendary"):
        m = re.search(rf"(\d+)\s+{s}\s+[Ss]hard", text, re.I)
        if m:
            shards[s] = int(m.group(1))

    chickens: dict[str, int] = {}
    for cm in re.finditer(r"([2-6])[★*]\s*[×xX](\d+)", text):
        chickens[f"{cm.group(1)}★"] = int(cm.group(2))

    return dict(
        silver=silver, gems=gems, energy=energy, cb_keys=cb_keys,
        brews=brews, account_age_days=age, total_heroes=heroes,
        tomes=tomes, shards=shards, chickens=chickens,
    )


async def fetch_raidcheap_account(
    account_id: str, detail_url: str, price_usd: float
) -> Optional[AccountData]:
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers=HEADERS
        ) as client:
            resp = await client.get(detail_url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # ── Identify champions by portrait image matching ─────────────
            # This is the primary method — no translation needed.
            # Each RSL champion has a unique portrait identical in all languages.
            mythics: list[str] = []
            legendaries: list[str] = []

            if champion_matcher.db_loaded():
                # Collect all portrait image URLs from the page
                portrait_urls: list[tuple[str, str]] = []  # (full_url, context_text)
                for img in soup.find_all("img", src=True):
                    src: str = img["src"]
                    # Skip nav/UI images, keep only portrait-sized images
                    if any(s in src.lower() for s in ("logo", "icon", "btn", "bg", "banner")):
                        continue
                    full = src if src.startswith("http") else BASE_URL + src
                    # Try to get surrounding context to determine rarity
                    parent_text = ""
                    p = img.parent
                    for _ in range(4):
                        if p:
                            parent_text = p.get_text(" ", strip=True).lower()
                            p = p.parent
                    portrait_urls.append((full, parent_text))

                for img_url, context in portrait_urls:
                    name = await champion_matcher.find_by_url(img_url, client)
                    if not name:
                        continue
                    # Assign to mythic or legendary based on context
                    if "mythic" in context or "神话" in context:
                        if name not in mythics:
                            mythics.append(name)
                    else:
                        if name not in legendaries and name not in mythics:
                            legendaries.append(name)

            # ── Fallback: text translation (if DB not built or image fails) ─
            raw = soup.get_text(separator="\n")
            translated = translate_to_en(raw)
            combined = translated

            for img in soup.find_all("img", src=True):
                src = img["src"]
                if any(s in src.lower() for s in ("logo", "icon", "avatar", "banner", "btn")):
                    continue
                full = src if src.startswith("http") else BASE_URL + src
                ocr = await _ocr_image(full, client)
                if ocr.strip():
                    combined += "\n\n" + ocr

            # Only use text-based extraction if image matching found nothing
            if not mythics and not legendaries:
                mm = re.search(r"Mythic[^:]*:([^\n]{5,200})", combined, re.I)
                if mm:
                    mythics = [c.strip() for c in re.split(r"[,，•·]", mm.group(1)) if c.strip()]
                lm = re.search(r"Legendary[^:]*:([^\n]{5,500})", combined, re.I)
                if lm:
                    legendaries = [
                        c.strip() for c in re.split(r"[,，•·]", lm.group(1))
                        if c.strip() and c.strip() not in mythics
                    ]

        return AccountData(
            account_id=account_id,
            price_usd=price_usd,
            mythic_champions=mythics,
            legendary_champions=legendaries,
            **_extract_resources(combined),
        )
    except Exception as exc:
        logger.error(f"Detail fetch failed {account_id}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Used by main.py
# ---------------------------------------------------------------------------

async def fetch_raidcheap_list(scraper: RaidCheapScraper) -> list[tuple[str, str, float]]:
    return await scraper.fetch_listing()
