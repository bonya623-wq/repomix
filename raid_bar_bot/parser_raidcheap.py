"""
Parser for https://raid-cheap.com — Chinese RSL account marketplace.

Data extraction strategy (in priority order):
  1. HTML text on the listing page (translated CN→EN)
  2. OCR of account screenshots (images uploaded by the seller)
  3. Regex over translated text for resources and numbers

Image OCR requires pytesseract + Tesseract with chi_sim+eng packs:
  Ubuntu:  sudo apt install tesseract-ocr tesseract-ocr-chi-sim
  Windows: https://github.com/UB-Mannheim/tesseract/wiki
  Then:    pip install pytesseract Pillow

OCR is optional — if not available, only HTML text is parsed.
"""

import io
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

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

# ── Optional OCR ─────────────────────────────────────────────────────────────
try:
    import pytesseract
    from PIL import Image

    _OCR = True
    logger.info("pytesseract available — image OCR enabled for raid-cheap.com")
except ImportError:
    _OCR = False
    logger.info("pytesseract/Pillow not installed — image OCR disabled")


# ---------------------------------------------------------------------------
# OCR helper
# ---------------------------------------------------------------------------

async def _ocr_image_url(url: str, client: httpx.AsyncClient) -> str:
    """Download an image and extract text via OCR. Returns '' on failure."""
    if not _OCR:
        return ""
    try:
        resp = await client.get(url, timeout=20)
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")

        # Try English first (faster, handles global-server RSL screenshots)
        text_en = pytesseract.image_to_string(img, lang="eng")
        if len(text_en.strip()) > 30:
            return text_en

        # Fall back to Chinese+English OCR (Chinese-server screenshots)
        try:
            text_cn = pytesseract.image_to_string(img, lang="chi_sim+eng")
            return translate_to_en(text_cn) if text_cn.strip() else ""
        except Exception:
            return text_en  # return whatever we got
    except Exception as exc:
        logger.debug(f"OCR failed for {url}: {exc}")
        return ""


# ---------------------------------------------------------------------------
# Resource extraction from translated text
# ---------------------------------------------------------------------------

def _extract_resources(text: str) -> dict:
    """
    Parse resource values from a block of translated English text.
    Returns a dict of recognised fields.
    """

    def _int(pattern: str) -> Optional[int]:
        m = re.search(pattern, text, re.I)
        if not m:
            return None
        v = _parse_number(m.group(1))
        return int(v) if v is not None else None

    def _float(pattern: str) -> Optional[float]:
        m = re.search(pattern, text, re.I)
        return _parse_number(m.group(1)) if m else None

    silver_raw = re.search(r"[Ss]ilver[:\s]+([0-9.,]+\s*[MmKk]?)", text)
    silver = _parse_silver_millions(silver_raw.group(1)) if silver_raw else None

    gems = _int(r"[Gg]ems?[:\s]+([0-9,]+)")
    energy = _float(r"[Ee]nergy[:\s]+([0-9.,]+\s*[MmKk]?)")
    cb_keys = _int(r"CB\s*[Kk]eys?[:\s]+([0-9]+)")
    brews = _int(r"[Bb]rews?[:\s]+([0-9]+)")
    age = _int(r"(\d+)\s+days?\s+old|[Aa]ge[:\s]+(\d+)")
    total_heroes = _int(r"[Tt]otal\s+[Hh]eroes?[:\s]+(\d+)|[Hh]eroes?[:\s]+(\d+)")

    # Tomes
    tomes: dict[str, int] = {}
    for rarity in ("Rare", "Epic", "Legendary"):
        m = re.search(rf"(\d+)\s+{rarity}\s+[Tt]ome", text, re.I)
        if m:
            tomes[rarity] = int(m.group(1))
    if not tomes:
        m = re.search(r"[Tt]omes?[:\s]+([\d\w /]+)", text)
        if m:
            for part in m.group(1).split("/"):
                nm = re.match(r"(\d+)\s+(\w+)", part.strip())
                if nm:
                    tomes[nm.group(2).capitalize()] = int(nm.group(1))

    # Shards
    shards: dict[str, int] = {}
    for stype in ("Ancient", "Void", "Sacred", "Epic", "Legendary"):
        m = re.search(rf"(\d+)\s+{stype}\s+[Ss]hard", text, re.I)
        if m:
            shards[stype] = int(m.group(1))

    # Chickens: "6★ ×6" or "6* x24"
    chickens: dict[str, int] = {}
    for cm in re.finditer(r"([2-6])[★*]\s*[×xX](\d+)", text):
        chickens[f"{cm.group(1)}★"] = int(cm.group(2))

    return dict(
        silver=silver,
        gems=gems,
        energy=energy,
        cb_keys=cb_keys,
        brews=brews,
        account_age_days=age,
        total_heroes=total_heroes,
        tomes=tomes,
        shards=shards,
        chickens=chickens,
    )


def _extract_champions(text: str) -> tuple[list[str], list[str]]:
    """
    Try to pull mythic and legendary champion lists from translated text.
    Returns (mythics, legendaries).
    """
    mythics: list[str] = []
    legendaries: list[str] = []

    myth_m = re.search(r"Mythic[^:]*:([^\n\r]{5,200})", text, re.I)
    if myth_m:
        mythics = [c.strip() for c in re.split(r"[,，•·\n]", myth_m.group(1)) if c.strip()]

    leg_m = re.search(r"Legendary[^:]*:([^\n\r]{5,500})", text, re.I)
    if leg_m:
        legendaries = [
            c.strip()
            for c in re.split(r"[,，•·\n]", leg_m.group(1))
            if c.strip() and c.strip() not in mythics
        ]

    return mythics, legendaries


# ---------------------------------------------------------------------------
# Price extraction
# ---------------------------------------------------------------------------

def _extract_price(element) -> float:
    """Look for a USD price in an element and its ancestors."""
    from bs4 import Tag
    candidates = [element]
    p = element.parent if hasattr(element, "parent") else None
    for _ in range(5):
        if p:
            candidates.append(p)
            p = getattr(p, "parent", None)

    for tag in candidates:
        if tag is None:
            continue
        text = tag.get_text() if hasattr(tag, "get_text") else str(tag)
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

    # Fallback: look for any price-like number
    m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|円|¥|₽|RMB)", str(candidates[0]))
    if m:
        try:
            # Rough CNY→USD conversion (update rate as needed)
            cny = float(m.group(1))
            return round(cny / 7.2, 2)
        except ValueError:
            pass
    return 0.0


# ---------------------------------------------------------------------------
# Listing-page scraper
# ---------------------------------------------------------------------------

async def fetch_raidcheap_list() -> list[tuple[str, str, float]]:
    """
    Return (account_id, detail_url, price_usd) for all accounts on raid-cheap.com.

    NOTE: Selectors are best-effort without live HTML access.
    Run with --parse-only --debug to see raw HTML if nothing is found.
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        resp = await client.get(BASE_URL)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "lxml")
    results: dict[str, tuple[str, float]] = {}

    # Strategy 1: <a> links matching typical account detail URL patterns
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        # Common patterns: /account/ID, /item/ID, /product/ID, /listing/ID
        m = re.search(r"/(?:account|item|product|listing|detail)/([A-Za-z0-9_-]{3,})", href)
        if not m:
            # Or just /ID at the end
            m = re.search(r"/([A-Za-z0-9_-]{6,})$", href)
        if m:
            aid = m.group(1)
            if aid in results:
                continue
            full_url = href if href.startswith("http") else BASE_URL + href
            price = _extract_price(a)
            results[aid] = (full_url, price)

    # Strategy 2: [data-id] or [data-product-id] attributes
    if not results:
        for card in soup.find_all(attrs={"data-id": True}):
            aid = card.get("data-id", "").strip()
            if re.match(r"[A-Za-z0-9_-]{3,}", aid):
                price = _extract_price(card)
                url = f"{BASE_URL}/account/{aid}"
                results[aid] = (url, price)

    if not results:
        logger.warning(
            "raid-cheap.com: no listings found. HTML may have changed.\n"
            f"First 2000 chars:\n{html[:2000]}"
        )
    else:
        logger.info(f"raid-cheap.com: {len(results)} accounts found")

    return [("rc_" + aid, url, price) for aid, (url, price) in results.items()]


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------

async def fetch_raidcheap_account(account_id: str, detail_url: str, price_usd: float) -> Optional[AccountData]:
    """Fetch and parse one raid-cheap.com account detail page."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
            resp = await client.get(detail_url)
            resp.raise_for_status()
            html = resp.text

            soup = BeautifulSoup(html, "lxml")

            # 1. Translate all page text ────────────────────────────────────
            raw_text = soup.get_text(separator="\n")
            translated = translate_to_en(raw_text)
            combined = translated  # start with translated HTML text

            # 2. OCR all account images ─────────────────────────────────────
            img_tags = soup.find_all("img", src=True)
            image_texts: list[str] = []
            for img in img_tags:
                src: str = img["src"]
                if any(skip in src for skip in ("logo", "icon", "avatar", "banner")):
                    continue  # skip UI images, only want account screenshots
                full_src = src if src.startswith("http") else BASE_URL + src
                ocr_text = await _ocr_image_url(full_src, client)
                if ocr_text.strip():
                    image_texts.append(ocr_text)
                    logger.debug(f"OCR {full_src[:80]}: {ocr_text[:120]}")

            if image_texts:
                combined = combined + "\n\n" + "\n\n".join(image_texts)

        # 3. Extract champion names ─────────────────────────────────────────
        # First try from page structure (dedicated sections)
        mythics: list[str] = []
        legendaries: list[str] = []

        soup2 = BeautifulSoup(html, "lxml")
        for tag in soup2.find_all(["li", "span", "div"]):
            cls = " ".join(tag.get("class", []))
            if "mythic" in cls.lower() or "神话" in tag.get_text():
                names = [t.get_text(strip=True) for t in tag.find_all(["span", "p", "li"])]
                mythics.extend(translate_champion_list([n for n in names if n]))

        # Fallback to regex on combined translated text
        if not mythics and not legendaries:
            mythics, legendaries = _extract_champions(combined)

        # 4. Extract resources ─────────────────────────────────────────────
        res = _extract_resources(combined)

        acc = AccountData(
            account_id=account_id,
            price_usd=price_usd,
            mythic_champions=mythics,
            legendary_champions=legendaries,
            **res,
        )
        return acc

    except Exception as exc:
        logger.error(f"raid-cheap.com: failed to fetch {account_id}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Public API (mirrors parser.py interface)
# ---------------------------------------------------------------------------

async def fetch_raidcheap_accounts(only_new_ids: set[str] | None = None) -> list[AccountData]:
    """Fetch all accounts from raid-cheap.com with full details."""
    id_list = await fetch_raidcheap_list()
    accounts: list[AccountData] = []

    for account_id, detail_url, price in id_list:
        if only_new_ids is not None and account_id not in only_new_ids:
            accounts.append(AccountData(account_id=account_id, price_usd=price))
            continue
        acc = await fetch_raidcheap_account(account_id, detail_url, price)
        if acc:
            accounts.append(acc)

    return accounts
