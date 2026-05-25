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
import subprocess
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

# Dropdown indices for RSL on G2G (order matches buttons on the page)
_RSL_DROPDOWNS = [
    {"index": 0, "value": "Android"},
    {"index": 1, "value": "{hero_level}"},
    {"index": 2, "value": "{myth_level}"},
]


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


def get_hero_level(legendary_count: int) -> str:
    # Values must match G2G dropdown exactly: 300+, 250+, 200+, 100+, 50+, 10+, 9 or below
    if legendary_count >= 300: return "300+"
    if legendary_count >= 250: return "250+"
    if legendary_count >= 200: return "200+"
    if legendary_count >= 100: return "100+"
    if legendary_count >= 50:  return "50+"
    if legendary_count >= 10:  return "10+"
    return "9 or below"


def get_myth_level(mythic_count: int) -> str:
    if mythic_count >= 10: return "10+"
    if mythic_count >= 6:  return "6+"
    return "5 or below"


def make_rsl_game_handler(hero_level: str, myth_level: str):
    """Fill RSL dropdowns by explicit index (Android / hero_level / myth_level)."""
    async def handler(page, bot):
        for dd in _RSL_DROPDOWNS:
            idx = dd["index"]
            val = dd["value"]
            if val == "{hero_level}":
                val = hero_level
            elif val == "{myth_level}":
                val = myth_level
            await bot._select_dropdown(page, idx, val)
        return True

    return handler


async def _publish_account(
    account_id: str,
    detail_url: str,
    price: float,
    g2g: G2GBot,
    published: dict,
    first_lot: bool,
) -> bool:
    """Publish one account to G2G. Returns updated first_lot flag."""
    try:
        acc = await fetch_detail(account_id, detail_url, price)
        if acc is None:
            logger.error(f"Could not load data for {account_id} — skip")
            return first_lot

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
                logger.warning(f"✓ Published {account_id} but lot ID not found yet")
            return False  # first_lot = False after first success
        else:
            logger.error(f"✗ Failed to publish {account_id}")
            return True  # reset first_lot on failure

    except Exception as exc:
        logger.error(f"Error publishing {account_id}: {exc}", exc_info=True)
        return True


async def run_cycle(
    config: dict,
    g2g: G2GBot,
    scraper: RaidCheapScraper,
    first_lot: bool,
) -> bool:
    logger.info("=== Scan cycle ===")

    lots_per_champion = config.get("lots_per_champion", 10)
    max_champions    = config.get("max_champions", 20)

    # Navigate once and build rarity map
    try:
        await scraper.setup_page()
        champion_count = await scraper.get_champion_count()
    except Exception as exc:
        logger.error(f"Failed to init scraper: {exc}")
        return first_lot

    logger.info(
        f"Champions on page: {champion_count} | "
        f"checking first {min(champion_count, max_champions)} | "
        f"up to {lots_per_champion} lots each"
    )

    published    = load_published()
    all_seen_ids: set[str] = set()

    for champ_idx in range(min(champion_count, max_champions)):
        try:
            accounts, champ_name = await scraper.fetch_for_champion(champ_idx)
        except Exception as exc:
            logger.error(f"Champion [{champ_idx}]: fetch failed — {exc}")
            continue

        if not accounts:
            continue

        for aid, _, _ in accounts:
            all_seen_ids.add(aid)

        new_accounts = [
            (aid, url, price) for aid, url, price in accounts
            if aid not in published
        ]
        new_accounts.sort(key=lambda x: x[2])

        if not new_accounts:
            logger.info(f"[{champ_name}]: no new accounts")
            continue

        to_publish = new_accounts[:lots_per_champion]
        logger.info(
            f"[{champ_name}]: {len(new_accounts)} new — "
            f"publishing {len(to_publish)}"
        )

        for account_id, detail_url, price in to_publish:
            logger.info(f"  Publishing {account_id}  ${price:.2f} …")
            first_lot = await _publish_account(
                account_id, detail_url, price, g2g, published, first_lot
            )

    # Remove lots whose accounts are no longer on the site
    removed_ids = set(published.keys()) - all_seen_ids
    logger.info(f"Removed from site: {len(removed_ids)}")

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

    if sys.platform == "win32":
        profile_abs = str(Path(profile_dir).resolve())
        try:
            result = subprocess.run(
                ["wmic", "process", "where",
                 f"name='chrome.exe' and commandline like '%{profile_abs}%'",
                 "delete"],
                capture_output=True, timeout=10,
            )
            logger.debug(f"Chrome kill result: {result.returncode}")
        except Exception as e:
            logger.warning(f"Could not kill Chrome processes: {e}")
        await asyncio.sleep(1)

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