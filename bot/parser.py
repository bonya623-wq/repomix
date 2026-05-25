"""
Parser for raid.bar/pr — Raid Shadow Legends account marketplace.

If the site HTML changes, update selectors in _extract_* functions and rerun
  python main.py --parse-only --debug
to see what's being fetched.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

BASE_URL = "https://raid.bar/pr"
SITE_ROOT = "https://raid.bar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class AccountData:
    account_id: str
    price_usd: float
    mythic_champions: list[str] = field(default_factory=list)
    legendary_champions: list[str] = field(default_factory=list)
    silver: Optional[float] = None          # in millions, e.g. 7.6
    gems: Optional[int] = None
    energy: Optional[float] = None          # raw value, e.g. 53900
    cb_keys: Optional[int] = None
    shards: dict[str, int] = field(default_factory=dict)   # {"Epic": 4}
    tomes: dict[str, int] = field(default_factory=dict)    # {"Rare": 27, ...}
    brews: Optional[int] = None
    chickens: dict[str, int] = field(default_factory=dict) # {"6★": 6, ...}
    account_age_days: Optional[int] = None
    total_heroes: Optional[int] = None


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def _parse_number(text: str) -> Optional[float]:
    """Parse '7.6M', '53.9K', '7,696' → float. Returns None on failure."""
    if not text:
        return None
    text = text.strip().replace(",", "").replace(" ", "")
    try:
        if text[-1].lower() == "m":
            return float(text[:-1])
        if text[-1].lower() == "k":
            return float(text[:-1]) * 1_000
        return float(re.sub(r"[^\d.]", "", text))
    except (ValueError, IndexError):
        return None


def _parse_silver_millions(text: str) -> Optional[float]:
    """Return silver in millions (handles both '7.6M' and raw '7600000')."""
    val = _parse_number(text)
    if val is None:
        return None
    return val if val < 1_000_000 else val / 1_000_000


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------

def _find_section(soup: BeautifulSoup, *keywords: str) -> Optional[Tag]:
    """Return the first tag whose text starts with any of the given keywords."""
    kw_lower = [k.lower() for k in keywords]
    for tag in soup.find_all(["section", "div", "ul", "h2", "h3", "h4", "p"]):
        text = tag.get_text(separator=" ", strip=True).lower()
        if any(text.startswith(k) for k in kw_lower):
            return tag
    return None


def _text_of_items(section: Tag) -> list[str]:
    """Extract text from all <li> or <span class=*name*> children."""
    items = section.find_all(["li", "span"])
    return [i.get_text(strip=True) for i in items if i.get_text(strip=True)]


def parse_account_page(html: str, account_id: str, price_usd: float) -> AccountData:
    """Parse a single account detail page into AccountData."""
    soup = BeautifulSoup(html, "lxml")
    acc = AccountData(account_id=account_id, price_usd=price_usd)
    page_text = soup.get_text(separator="\n")

    # ---- Champions ----
    # Try dedicated rarity sections first
    mythic_sec = _find_section(soup, "mythic")
    if mythic_sec:
        acc.mythic_champions = _text_of_items(mythic_sec)

    leg_sec = _find_section(soup, "legendary")
    if leg_sec:
        # Exclude mythics if the legendary section accidentally includes them
        acc.legendary_champions = [
            n for n in _text_of_items(leg_sec)
            if n not in acc.mythic_champions
        ]

    # Fallback: regex over page text for champion names listed after rarity labels
    if not acc.mythic_champions:
        m = re.search(r"Mythic[:\s]+(.+?)(?=Legendary|Epic|$)", page_text, re.I | re.S)
        if m:
            acc.mythic_champions = [
                c.strip() for c in re.split(r"[,\n•·]", m.group(1)) if c.strip()
            ]

    if not acc.legendary_champions:
        m = re.search(r"Legendary[:\s]+(.+?)(?=Epic|Resources|$)", page_text, re.I | re.S)
        if m:
            acc.legendary_champions = [
                c.strip() for c in re.split(r"[,\n•·]", m.group(1)) if c.strip()
            ]

    # ---- Resources ----
    def _re(pattern: str) -> Optional[re.Match]:
        return re.search(pattern, page_text, re.IGNORECASE)

    m = _re(r"Silver[:\s]+([0-9.,]+\s*[MmKk]?)")
    if m:
        acc.silver = _parse_silver_millions(m.group(1))

    m = _re(r"Gems?[:\s]+([0-9,]+)")
    if m:
        v = _parse_number(m.group(1))
        acc.gems = int(v) if v is not None else None

    m = _re(r"Energy[:\s]+([0-9.,]+\s*[MmKk]?)")
    if m:
        acc.energy = _parse_number(m.group(1))

    m = _re(r"CB\s*Keys?[:\s]+([0-9]+)")
    if m:
        acc.cb_keys = int(m.group(1))

    m = _re(r"Brews?[:\s]+([0-9]+)")
    if m:
        acc.brews = int(m.group(1))

    # Tomes: "27 Rare / 53 Epic / 11 Legendary"
    for rarity in ("Rare", "Epic", "Legendary"):
        m = re.search(rf"(\d+)\s+{rarity}\s+(?:Tome|tome)", page_text, re.I)
        if m:
            acc.tomes[rarity] = int(m.group(1))
    if not acc.tomes:
        m = _re(r"Tomes?[:\s]+([\d\w /]+)")
        if m:
            for part in m.group(1).split("/"):
                part = part.strip()
                nm = re.match(r"(\d+)\s+(\w+)", part)
                if nm:
                    acc.tomes[nm.group(2).capitalize()] = int(nm.group(1))

    # Shards: "4 Epic Shard" or just "Epic: 4"
    for stype in ("Ancient", "Void", "Sacred", "Epic", "Legendary"):
        m = re.search(rf"(\d+)\s+{stype}(?:\s+Shard)?", page_text, re.I)
        if m:
            acc.shards[stype] = int(m.group(1))

    # Chickens: "6★ ×6" or "6* x24"
    for cm in re.finditer(r"([2-6])[★*]\s*[×xX](\d+)", page_text):
        acc.chickens[f"{cm.group(1)}★"] = int(cm.group(2))

    # Account age
    m = _re(r"Account\s*Age[:\s]+(\d+)")
    if not m:
        m = _re(r"(\d+)\s+days?\s+old")
    if m:
        acc.account_age_days = int(m.group(1))

    # Total heroes
    m = _re(r"(?:Total\s+)?Heroes?[:\s]+(\d+)")
    if m:
        acc.total_heroes = int(m.group(1))

    logger.debug(
        f"Parsed {account_id}: {len(acc.mythic_champions)} mythics, "
        f"{len(acc.legendary_champions)} legs, ${price_usd:.2f}"
    )
    return acc


# ---------------------------------------------------------------------------
# Listing-page scraper
# ---------------------------------------------------------------------------

def _extract_price(element: Tag) -> float:
    """Search element and ancestors for a USD price string."""
    candidates = [element]
    parent = element.parent
    for _ in range(4):
        if parent:
            candidates.append(parent)
            parent = parent.parent

    for tag in candidates:
        if tag is None:
            continue
        text = tag.get_text()
        m = re.search(
            r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)"
            r"|([0-9]+(?:\.[0-9]{1,2})?)\s*(?:USD|\$)",
            text,
        )
        if m:
            try:
                return float(m.group(1) or m.group(2))
            except ValueError:
                pass
    return 0.0


async def _get_html(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


async def fetch_account_list() -> list[tuple[str, str, float]]:
    """
    Fetch (account_id, detail_url, price_usd) for every account on raid.bar/pr.

    Strategy:
    1. Find all <a href="/pr/<id>"> links → extract ID + price from context.
    2. Fallback: find [data-id] cards.
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        html = await _get_html(client, BASE_URL)

    soup = BeautifulSoup(html, "lxml")
    results: dict[str, tuple[str, float]] = {}  # account_id → (url, price)

    # Strategy 1: anchor links matching /pr/<alphanum>
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        m = re.search(r"/pr/([A-Za-z0-9]{5,})", href)
        if m:
            aid = m.group(1)
            if aid not in results:
                full_url = href if href.startswith("http") else SITE_ROOT + href
                price = _extract_price(a)
                results[aid] = (full_url, price)

    # Strategy 2: data-id attributes (many Vue/React SPAs use these)
    if not results:
        for card in soup.find_all(attrs={"data-id": True}):
            aid = card["data-id"]
            if re.match(r"[A-Za-z0-9]{5,}", aid):
                price = _extract_price(card)
                url = f"{SITE_ROOT}/pr/{aid}"
                results[aid] = (url, price)

    if not results:
        logger.warning(
            "No account listings found on raid.bar/pr. "
            "The page structure may have changed.\n"
            f"First 2 000 chars of HTML:\n{html[:2000]}"
        )

    logger.info(f"Listing page: {len(results)} accounts found")
    return [(aid, url, price) for aid, (url, price) in results.items()]


async def fetch_accounts(only_new_ids: set[str] | None = None) -> list[AccountData]:
    """
    Fetch all accounts from raid.bar/pr.

    Pass `only_new_ids` to limit detail-page fetches to a subset of IDs
    (saves HTTP requests when you only need new accounts' full data).
    Returns all found accounts (with only basic data for skipped ones if
    only_new_ids is given — don't pass it if you need full data for all).
    """
    id_list = await fetch_account_list()
    accounts: list[AccountData] = []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=HEADERS) as client:
        for account_id, detail_url, price in id_list:
            if only_new_ids is not None and account_id not in only_new_ids:
                # Return a stub so callers know this account still exists
                accounts.append(AccountData(account_id=account_id, price_usd=price))
                continue
            try:
                html = await _get_html(client, detail_url)
                acc = parse_account_page(html, account_id, price)
                accounts.append(acc)
            except Exception as e:
                logger.error(f"Failed to fetch detail for {account_id}: {e}")

    return accounts
