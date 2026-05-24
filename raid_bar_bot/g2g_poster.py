"""
G2G listing automation via Playwright (async).

HOW TO SET UP COOKIES
---------------------
Use a browser extension like "Cookie-Editor" or "EditThisCookie":
  1. Log in to g2g.com as your seller account.
  2. Export cookies as JSON (Netscape / Playwright format).
  3. Save the JSON file alongside config.json, e.g. cookies_g2g.json.
  4. Set in config.json:  "g2g_cookies_file": "cookies_g2g.json"

The JSON format is an array of objects, exactly as exported by most
cookie-editor extensions:
  [{"name": "...", "value": "...", "domain": ".g2g.com", ...}, ...]

SELECTOR NOTES
--------------
G2G updates its frontend regularly. If the bot stops working, take a
screenshot (saved as g2g_debug_*.png) and update the selectors below.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

G2G_BASE = "https://www.g2g.com"
# G2G's offer-creation URL; the game slug is appended as a query param.
G2G_CREATE_URL = f"{G2G_BASE}/offer/create"
G2G_SELLING_URL = f"{G2G_BASE}/account/selling"


class G2GPoster:
    def __init__(self, config: dict) -> None:
        self._config = config
        self._pw = None
        self._browser = None
        self._ctx: Optional[BrowserContext] = None

    async def __aenter__(self) -> "G2GPoster":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._ctx = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        await self._load_cookies()
        return self

    async def __aexit__(self, *_) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    async def _load_cookies(self) -> None:
        """
        Load G2G session cookies. Accepts three formats (in priority order):

        1. "g2g_cookies_file": "cookies_g2g.json"   ← path to exported JSON
        2. "g2g_cookie": [{"name":...}, ...]         ← inline JSON array
        3. "g2g_cookie": "name=val; name2=val2"      ← legacy string
        """
        cookies: list[dict] = []

        cookies_file = self._config.get("g2g_cookies_file", "")
        if cookies_file:
            p = Path(cookies_file)
            if not p.exists():
                raise FileNotFoundError(f"Cookie file not found: {p.resolve()}")
            with open(p, encoding="utf-8") as f:
                cookies = json.load(f)
            logger.info(f"Loaded {len(cookies)} cookies from {p}")

        elif isinstance(self._config.get("g2g_cookie"), list):
            cookies = self._config["g2g_cookie"]
            logger.info(f"Loaded {len(cookies)} inline cookies from config")

        elif isinstance(self._config.get("g2g_cookie"), str):
            raw = self._config["g2g_cookie"].strip()
            if not raw:
                raise ValueError(
                    "No G2G cookies configured. "
                    "Set 'g2g_cookies_file': 'cookies_g2g.json' in config.json"
                )
            for chunk in raw.split(";"):
                chunk = chunk.strip()
                if "=" not in chunk:
                    continue
                name, _, value = chunk.partition("=")
                cookies.append(
                    {"name": name.strip(), "value": value.strip(),
                     "domain": ".g2g.com", "path": "/", "sameSite": "Lax"}
                )
            logger.info(f"Loaded {len(cookies)} cookies from string")

        else:
            raise ValueError(
                "No G2G cookies configured. "
                "Set 'g2g_cookies_file': 'cookies_g2g.json' in config.json"
            )

        # Playwright needs at least path and sameSite
        for c in cookies:
            c.setdefault("path", "/")
            c.setdefault("sameSite", "Lax")

        await self._ctx.add_cookies(cookies)
        logger.info(f"G2G session: {len(cookies)} cookies loaded")

    async def _is_logged_in(self, page: Page) -> bool:
        return "login" not in page.url and "sign-in" not in page.url

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    async def _fill(self, page: Page, selectors: list[str], value: str, required: bool = True) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.scroll_into_view_if_needed()
                    await loc.clear()
                    await loc.fill(value)
                    logger.debug(f"Filled {sel!r} ← {value[:60]!r}")
                    return True
            except Exception:
                continue
        if required:
            logger.warning(f"Could not fill field — tried: {selectors}")
        return False

    async def _fill_rich_text(self, page: Page, value: str) -> None:
        """Handle plain <textarea>, contenteditable, TinyMCE, Quill."""
        # Plain textarea
        if await self._fill(
            page,
            [
                "textarea[name='description']",
                "textarea[name='offer_description']",
                "textarea[placeholder*='description' i]",
                "#description",
                "textarea",
            ],
            value,
            required=False,
        ):
            return

        # Contenteditable (Quill, ProseMirror, etc.)
        loc = page.locator("[contenteditable='true']").first
        if await loc.count() > 0:
            await loc.click()
            # Select all + replace
            await loc.press("Control+a")
            await loc.type(value)
            return

        # TinyMCE iframe
        try:
            frame = page.frame_locator("iframe[id*='mce'], iframe[id*='tiny']").first
            body = frame.locator("body")
            if await body.count() > 0:
                await body.click()
                await body.press("Control+a")
                await body.type(value)
                return
        except Exception:
            pass

        logger.warning("Description field not found — listing may be created without description")

    async def _click_button(self, page: Page, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_listing(self, title: str, description: str, price: float) -> Optional[str]:
        """
        Publish a new G2G listing. Returns the lot ID string on success.

        Saves a debug screenshot to g2g_debug_create.png if something fails.
        """
        page = await self._ctx.new_page()
        try:
            return await self._create(page, title, description, price)
        except Exception as exc:
            logger.error(f"create_listing failed: {exc}")
            await page.screenshot(path="g2g_debug_create.png")
            return None
        finally:
            await page.close()

    async def _create(self, page: Page, title: str, description: str, price: float) -> Optional[str]:
        category = self._config.get("g2g_listing_category", "raid-shadow-legends")

        # Navigate to offer creation.
        # Try the direct URL first; fall back to the "Sell" dashboard if G2G
        # doesn't allow the direct URL (varies by account type).
        url = f"{G2G_CREATE_URL}?game={category}"
        logger.info(f"Opening: {url}")
        await page.goto(url, wait_until="networkidle", timeout=40_000)

        if not await self._is_logged_in(page):
            raise RuntimeError("G2G session expired — refresh cookies in config.json")

        # Some G2G flows need the game/category selected from a dropdown
        # before the form fields appear. Try to select it if a picker exists.
        await self._select_game_category(page, category)

        # ---- Fill form fields ----

        await self._fill(
            page,
            [
                "input[name='offer_name']",
                "input[name='title']",
                "input[name='name']",
                "input[placeholder*='title' i]",
                "input[placeholder*='offer name' i]",
                "#offer-name",
                "#title",
            ],
            title,
        )

        await self._fill_rich_text(page, description)

        await self._fill(
            page,
            [
                "input[name='price']",
                "input[name='unit_price']",
                "input[placeholder*='price' i]",
                "input[type='number'][name*='price']",
                "#price",
                "#unit-price",
            ],
            f"{price:.2f}",
        )

        # Stock / Quantity = 1 (unique account)
        await self._fill(
            page,
            [
                "input[name='stock']",
                "input[name='quantity']",
                "input[name='qty']",
                "input[placeholder*='quantity' i]",
                "input[placeholder*='stock' i]",
                "#stock",
                "#quantity",
            ],
            "1",
            required=False,
        )

        # ---- Submit ----
        clicked = await self._click_button(
            page,
            [
                "button[type='submit']",
                "button:has-text('Publish')",
                "button:has-text('Post Offer')",
                "button:has-text('Submit')",
                "button:has-text('Create')",
                "input[type='submit']",
            ],
        )
        if not clicked:
            raise RuntimeError("Submit button not found")

        # Wait for redirect to the listing URL
        try:
            await page.wait_for_url(
                re.compile(r"g2g\.com/(offer|listing)/[A-Za-z0-9]"),
                timeout=20_000,
            )
        except Exception:
            await page.screenshot(path="g2g_debug_after_submit.png")
            logger.warning("Did not redirect to listing page; trying to extract lot ID from current page")

        return self._extract_lot_id(page.url) or await self._extract_lot_id_from_page(page)

    async def _select_game_category(self, page: Page, category: str) -> None:
        """Try to pick Raid Shadow Legends from a game selector dropdown."""
        try:
            picker = page.locator("[name='game'], [name='category'], .game-selector").first
            if await picker.count() > 0:
                tag = await picker.evaluate("el => el.tagName")
                if tag == "SELECT":
                    await picker.select_option(label=re.compile(r"Raid", re.I))
                else:
                    await picker.click()
                    option = page.locator(f"li:has-text('Raid'), [data-value*='raid']").first
                    if await option.count() > 0:
                        await option.click()
        except Exception as e:
            logger.debug(f"Game category picker not found or not needed: {e}")

    def _extract_lot_id(self, url: str) -> Optional[str]:
        m = re.search(r"/(offer|listing)/([A-Za-z0-9_-]+)", url)
        return m.group(2) if m else None

    async def _extract_lot_id_from_page(self, page: Page) -> Optional[str]:
        content = await page.content()
        m = re.search(r"offer[_\-]?id[\":\s]+([A-Za-z0-9_-]+)", content, re.I)
        if m:
            return m.group(1)
        # Last resort: look for an ID-like value in meta tags
        m = re.search(r'<meta[^>]+content="([A-Za-z0-9_-]{8,})"', content)
        return m.group(1) if m else None

    # ------------------------------------------------------------------

    async def deactivate_listing(self, lot_id: str) -> bool:
        """
        Deactivate (pause/remove) a G2G listing by its lot ID.
        Returns True on success.
        """
        page = await self._ctx.new_page()
        try:
            return await self._deactivate(page, lot_id)
        except Exception as exc:
            logger.error(f"deactivate_listing {lot_id} failed: {exc}")
            await page.screenshot(path=f"g2g_debug_deactivate_{lot_id}.png")
            return False
        finally:
            await page.close()

    async def _deactivate(self, page: Page, lot_id: str) -> bool:
        # Try direct manage/edit URLs first
        for url in (
            f"{G2G_BASE}/offer/{lot_id}/manage",
            f"{G2G_BASE}/offer/{lot_id}/edit",
            f"{G2G_BASE}/offer/{lot_id}",
        ):
            await page.goto(url, wait_until="networkidle", timeout=20_000)
            if "404" not in await page.title() and lot_id in page.url:
                break
        else:
            # Fall back to seller dashboard — find the listing there
            await page.goto(G2G_SELLING_URL, wait_until="networkidle", timeout=20_000)
            link = page.locator(f"a[href*='{lot_id}']").first
            if await link.count() > 0:
                await link.click()
                await page.wait_for_load_state("networkidle")

        clicked = await self._click_button(
            page,
            [
                "button:has-text('Deactivate')",
                "button:has-text('Pause')",
                "button:has-text('Disable')",
                "a:has-text('Deactivate')",
                "[data-action='deactivate']",
                "[data-action='pause']",
                ".deactivate-btn",
                ".pause-btn",
            ],
        )
        if not clicked:
            logger.error(f"Deactivate button not found for lot {lot_id}")
            await page.screenshot(path=f"g2g_debug_no_deactivate_{lot_id}.png")
            return False

        # Handle confirmation modal if present
        await asyncio.sleep(1)
        await self._click_button(
            page,
            [
                "button:has-text('Confirm')",
                "button:has-text('Yes')",
                "button:has-text('OK')",
                ".modal button[type='submit']",
            ],
        )
        await asyncio.sleep(1)

        logger.info(f"Deactivated lot {lot_id}")
        return True
