"""
Raid Cheap → G2G publishing bot.

Source: raid-cheap.com only (Chinese RSL account marketplace).

Usage:
  python main.py               # continuous loop
  python main.py --once        # single cycle then exit
  python main.py --parse-only  # show accounts without posting (debug)
  python main.py --debug       # verbose logging
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

from formatter import format_description, format_title
from g2g_bot import G2GBot, load_lot_pairs, _save_pairs_atomic
from parser_raidcheap import RaidCheapScraper, fetch_raidcheap_list
from parser_raidcheap import fetch_raidcheap_account as fetch_detail

PUBLISHED_FILE = Path("published_ids.json")
CONFIG_FILE = Path("config.json")
GAME = "Raid: Shadow Legends"

logger = logging.getLogger(__name__)

# ── G2G dropdown option sets for RSL ─────────────────────────────────────────
_HERO_VALUES = {
    "300+", "250+", "200+", "150+", "100+", "80+",
    "50+", "30+", "20+", "10", "9 or below",
}
_MYTH_VALUES = {"10+", "6+", "5 or below"}


# ---------------------------------------------------------------------------
# Config / persistence helpers
# ---------------------------------------------------------------------------

def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler("bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_published() -> dict[str, str]:
    if not PUBLISHED_FILE.exists():
        return {}
    with open(PUBLISHED_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_published(data: dict[str, str]) -> None:
    with open(PUBLISHED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# lot_pairs.json helpers (supplement G2GBot's internal tracking)
# ---------------------------------------------------------------------------

def get_g2g_id_from_lot_pairs(funpay_id: str) -> Optional[str]:
    pairs = load_lot_pairs()
    for p in pairs.get(GAME, []):
        if p.get("funpay_id") == funpay_id:
            return p.get("g2g_id")
    return None


def remove_from_lot_pairs(funpay_id: str) -> None:
    pairs = load_lot_pairs()
    if GAME in pairs:
        pairs[GAME] = [p for p in pairs[GAME] if p.get("funpay_id") != funpay_id]
        _save_pairs_atomic(pairs)


# ---------------------------------------------------------------------------
# Hero / myth level mapping (for G2G RSL dropdown values)
# ---------------------------------------------------------------------------

def get_hero_level(legendary_count: int) -> str:
    if legendary_count >= 300: return "300+"
    if legendary_count >= 250: return "250+"
    if legendary_count >= 200: return "200+"
    if legendary_count >= 150: return "150+"
    if legendary_count >= 100: return "100+"
    if legendary_count >= 80:  return "80+"
    if legendary_count >= 50:  return "50+"
    if legendary_count >= 30:  return "30+"
    if legendary_count >= 20:  return "20+"
    if legendary_count >= 10:  return "10"
    return "9 or below"


def get_myth_level(mythic_count: int) -> str:
    if mythic_count >= 10: return "10+"
    if mythic_count >= 6:  return "6+"
    return "5 or below"


# ---------------------------------------------------------------------------
# RSL game_handler — fills hero_level and myth_level dropdowns on G2G form
# ---------------------------------------------------------------------------

def make_rsl_game_handler(hero_level: str, myth_level: str):
    """Returns a game_handler closure that fills RSL-specific G2G dropdowns."""

    async def handler(page, bot):
        await asyncio.sleep(0.5)
        btns = await page.query_selector_all("button.g-btn-select")
        hero_set = False
        myth_set = False

        for btn in btns:
            if hero_set and myth_set:
                break

            btn_text = (await btn.inner_text()).strip()
            # Skip the delivery-time button (starts with "0 hour")
            if btn_text.startswith("0") and "hour" in btn_text.lower():
                continue

            try:
                await btn.click()
                await asyncio.sleep(1.0)

                items = await page.query_selector_all(
                    ".q-virtual-scroll__content .q-item"
                )
                texts = [(item, (await item.inner_text()).strip()) for item in items]
                text_vals = {t[1] for t in texts}

                is_hero = bool(text_vals & _HERO_VALUES)
                is_myth = bool(text_vals & _MYTH_VALUES)

                target: Optional[str] = None
                is_for_hero = False
                if not hero_set and is_hero:
                    target = hero_level
                    is_for_hero = True
                elif not myth_set and is_myth:
                    target = myth_level
                    is_for_hero = False

                if target:
                    selected = False
                    for item, text in texts:
                        if text == target:
                            await item.click()
                            await asyncio.sleep(0.5)
                            if is_for_hero:
                                hero_set = True
                            else:
                                myth_set = True
                            kind = "hero_level" if is_for_hero else "myth_level"
                            logger.info(f"RSL: {kind} = '{target}' OK")
                            selected = True
                            break
                    if not selected:
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.3)
                else:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)

            except Exception as e:
                logger.warning(f"RSL handler: dropdown error: {e}")

        if not hero_set:
            logger.warning(f"RSL: could not set hero_level='{hero_level}'")
        if not myth_set:
            logger.warning(f"RSL: could not set myth_level='{myth_level}'")
        return True

    return handler


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

async def run_cycle(
    config: dict,
    g2g: G2GBot,
    scraper: RaidCheapScraper,
    first_lot: bool,
) -> bool:
    """Run one scan+publish cycle. Returns updated first_lot flag."""
    logger.info("=== Scan cycle ===")

    try:
        all_current = await fetch_raidcheap_list(scraper)
    except Exception as exc:
        logger.error(f"Failed to fetch account list: {exc}")
        return first_lot

    if not all_current:
        logger.warning("No accounts found on raid-cheap.com")
        return first_lot

    all_current.sort(key=lambda x: x[2])  # cheapest first
    logger.info(
        f"Live: {len(all_current)} accounts | "
        f"Cheapest: ${all_current[0][2]:.2f}"
    )

    current_ids = {t[0] for t in all_current}
    published = load_published()
    published_ids = set(published.keys())

    new_ids = current_ids - published_ids
    removed_ids = published_ids - current_ids
    logger.info(f"New: {len(new_ids)}  |  Removed: {len(removed_ids)}")

    # ── Publish new accounts (cheapest first) ────────────────────────────────
    new_sorted = [t for t in all_current if t[0] in new_ids]
    for account_id, detail_url, price in new_sorted:
        logger.info(f"Publishing {account_id}  ${price:.2f} …")
        try:
            acc = await fetch_detail(account_id, detail_url, price)
            if acc is None:
                logger.error(f"Could not load data for {account_id} — skip")
                continue

            title = format_title(acc)
            description = format_description(acc)
            hero_level = get_hero_level(len(acc.legendary_champions))
            myth_level = get_myth_level(len(acc.mythic_champions))

            ok = await g2g.create_lot(
                title=title,
                description=description,
                price=f"{price:.2f}",
                brief_description=format_title(acc),
                hero_level=hero_level,
                myth_level=myth_level,
                photos=None,
                first_lot=first_lot,
                funpay_id=account_id,
                funpay_url=detail_url,
                game=GAME,
                game_handler=make_rsl_game_handler(hero_level, myth_level),
                funpay_price=price,
            )

            if ok:
                lot_id = get_g2g_id_from_lot_pairs(account_id)
                if lot_id:
                    published[account_id] = lot_id
                    save_published(published)
                    logger.info(f"✓ {account_id} → lot {lot_id}  ${price:.2f}")
                else:
                    logger.warning(
                        f"✓ Published {account_id} but lot ID not found in "
                        f"lot_pairs.json yet — will pick it up next cycle"
                    )
                first_lot = False
            else:
                logger.error(f"✗ Failed to publish {account_id}")
                first_lot = True  # form state unknown after failure

        except Exception as exc:
            logger.error(f"Error publishing {account_id}: {exc}", exc_info=True)
            first_lot = True

    # ── Deactivate removed accounts ───────────────────────────────────────────
    for account_id in list(removed_ids):
        lot_id = published.get(account_id)
        if not lot_id:
            published.pop(account_id, None)
            save_published(published)
            remove_from_lot_pairs(account_id)
            continue

        logger.info(f"Deactivating lot {lot_id} (account {account_id} gone) …")
        try:
            ok = await g2g.delete_lot(lot_id)
            if ok:
                published.pop(account_id)
                save_published(published)
                remove_from_lot_pairs(account_id)
                logger.info(f"✓ Deactivated lot {lot_id}")
            else:
                logger.error(f"✗ Could not deactivate lot {lot_id}")
        except Exception as exc:
            logger.error(f"Error deactivating {lot_id}: {exc}", exc_info=True)

        # delete_lot navigates to the Manage page — form needs re-opening
        first_lot = True

    return first_lot


async def main() -> None:
    ap = argparse.ArgumentParser(description="Raid Cheap → G2G bot")
    ap.add_argument("--once", action="store_true", help="Single cycle then exit")
    ap.add_argument("--parse-only", action="store_true", help="Print accounts, no posting")
    ap.add_argument("--debug", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    _setup_logging(args.debug)
    config = load_config()

    if not PUBLISHED_FILE.exists():
        save_published({})

    profile_dir = config.get("browser_profile_dir", "./browser_profile")
    headless    = config.get("headless", False)

    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = Path(profile_dir) / lock_name
        if lock.exists():
            try:
                lock.unlink()
                logger.info(f"Removed stale browser lock: {lock}")
            except OSError as e:
                logger.warning(f"Could not remove {lock}: {e}")

    proxy_cfg    = config.get("proxy")
    proxy_kwargs = {}
    if proxy_cfg and proxy_cfg.get("server"):
        proxy_kwargs["proxy"] = {
            "server":   proxy_cfg["server"],
            "username": proxy_cfg.get("username", ""),
            "password": proxy_cfg.get("password", ""),
        }
        logger.info(f"G2G: using proxy {proxy_cfg['server']}")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            **proxy_kwargs,
        )

        async with RaidCheapScraper(context) as scraper:

            if args.parse_only:
                items = await fetch_raidcheap_list(scraper)
                items.sort(key=lambda x: x[2])
                print(f"\nFound {len(items)} accounts (cheapest first):\n")
                for account_id, detail_url, price in items:
                    acc = await fetch_detail(account_id, detail_url, price)
                    if not acc:
                        print(f"  {account_id}  ${price:.2f}  ← details unavailable")
                        continue
                    print(f"{'─' * 65}")
                    print(f"  ID    : {account_id}  |  Price: ${price:.2f}")
                    print(f"  Mythic: {', '.join(acc.mythic_champions) or '—'}")
                    print(f"  Legs  : {', '.join(acc.legendary_champions[:5]) or '—'}")
                    print(f"  Hero level  : {get_hero_level(len(acc.legendary_champions))}")
                    print(f"  Myth level  : {get_myth_level(len(acc.mythic_champions))}")
                    print(f"  TITLE : {format_title(acc)}")
                await context.close()
                return

            g2g = G2GBot(context)

            if not await g2g.is_logged_in():
                logger.info("G2G: not logged in — open the browser and log in manually")
                await g2g.wait_for_manual_login()

            logger.info("Bot ready. Monitoring raid-cheap.com …")
            first_lot = True

            if args.once:
                await run_cycle(config, g2g, scraper, first_lot)
                await context.close()
                return

            while True:
                first_lot = await run_cycle(config, g2g, scraper, first_lot)
                interval = config.get("check_interval_minutes", 10) * 60
                logger.info(f"Next scan in {interval // 60} min …")
                await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
