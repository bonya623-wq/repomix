"""
Raid Shadow Legends (raidmmo.com) → G2G publishing bot.

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
import games.raid as raid_game

PUBLISHED_FILE = Path("published_ids.json")
CONFIG_FILE = Path("config.json")
GAME = "Raid: Shadow Legends"

logger = logging.getLogger(__name__)

# Дропдауны RSL на G2G (индексы соответствуют порядку кнопок на странице)
RAID_DROPDOWNS = [
    {"index": 0, "value": "Android"},
    {"index": 1, "value": "{hero_level}"},
    {"index": 2, "value": "{myth_level}"},
]

# Конфиг игры (передаётся в raid_game.fill_form)
RAID_GAME_CFG = {
    "name": GAME,
    "g2g_dropdowns": RAID_DROPDOWNS,
}


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


def make_rsl_game_handler(hero_level: str, myth_level: str):
    """
    Game handler для RSL: заполняет дропдауны Android / hero_level / myth_level.

    Логика взята из games/raid.py — индексированные дропдауны из конфига.
    _select_dropdown ждёт появления кнопок перед кликом (исправление ошибки
    "не может найти кнопки").
    """
    game_cfg = dict(RAID_GAME_CFG)

    async def handler(page, bot):
        return await raid_game.fill_form(
            page=page,
            g2g_bot=bot,
            game_cfg=game_cfg,
            lot=None,
            hero_level=hero_level,
            myth_level=myth_level,
        )

    return handler


async def run_cycle(
    config: dict,
    g2g: G2GBot,
    scraper: RaidCheapScraper,
    first_lot: bool,
) -> bool:
    logger.info("=== Scan cycle ===")

    try:
        all_current = await fetch_raidcheap_list(scraper)
    except Exception as exc:
        logger.error(f"Failed to fetch account list: {exc}")
        return first_lot

    if not all_current:
        logger.warning("No accounts found on raidmmo.com")
        return first_lot

    all_current.sort(key=lambda x: x[2])
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
                        f"lot_pairs.json yet"
                    )
                first_lot = False
            else:
                logger.error(f"✗ Failed to publish {account_id}")
                first_lot = True

        except Exception as exc:
            logger.error(f"Error publishing {account_id}: {exc}", exc_info=True)
            first_lot = True

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
    ap = argparse.ArgumentParser(description="RaidMMO → G2G bot")
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

            logger.info("Bot ready. Monitoring raidmmo.com …")
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
