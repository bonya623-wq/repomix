"""
games/raid.py — Raid: Shadow Legends
Dropdowns only: Android, hero_level, myth_level.
Delivery (9 hours) is handled in g2g_bot._create_lot_inner after Manual delivery.
"""

import logging

logger = logging.getLogger("main")


async def fill_form(page, g2g_bot, game_cfg: dict, lot, hero_level: str, myth_level: str):
    """Fill Raid dropdowns: Android, hero_level, myth_level."""
    raw_dropdowns = game_cfg.get("g2g_dropdowns", [])
    for dd in raw_dropdowns:
        idx = dd.get("index", 0)
        val = dd.get("value", "")
        if val == "{hero_level}":
            val = hero_level
        elif val == "{myth_level}":
            val = myth_level
        if val and val != "9 hours":
            await g2g_bot._select_dropdown(page, idx, val)
