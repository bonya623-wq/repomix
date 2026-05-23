"""
games/diablo_game.py — Diablo Immortal
G2G form filling:
  Region → EU
  Server → Akeba / Ammuit / El' Druin / etc.
  Level  → 60 / 55+ / 50+ / ... / 29 or below
  Class  → Barbarian / Blood Knight / Crusader / etc.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def _pick_dropdown(page, label_text: str, value: str, timeout: int = 8000) -> bool:
    try:
        try:
            await page.wait_for_selector(".q-inner-loading", state="hidden", timeout=5000)
        except Exception:
            pass

        opened = False

        # Method 1: find form-group by label text
        groups = await page.query_selector_all(".r-form-group, .form-group")
        for group in groups:
            label = await group.query_selector("label, .form-label")
            if not label:
                continue
            label_txt = (await label.inner_text()).strip().lower()
            if label_text.lower() in label_txt:
                btn = await group.query_selector("button.g-btn-select, button[class*='select']")
                if btn:
                    await btn.click()
                    opened = True
                    break

        # Method 2: fallback — first unfilled "Please select"
        if not opened:
            btns = await page.query_selector_all("button.g-btn-select")
            for btn in btns:
                txt = (await btn.inner_text()).strip().lower()
                if "please select" in txt:
                    await btn.click()
                    opened = True
                    break

        if not opened:
            logger.warning(f"DI: дропдаун '{label_text}' не найден")
            return False

        try:
            await page.wait_for_selector(
                ".q-virtual-scroll__content .q-item, .q-item--dense",
                timeout=timeout,
            )
        except Exception:
            pass
        await asyncio.sleep(0.3)

        # Use filter input if available
        filt = await page.query_selector("input[placeholder='Type to filter']")
        if filt:
            await filt.click()
            await filt.fill(value)
            await asyncio.sleep(0.8)

        items = await page.query_selector_all(
            ".q-virtual-scroll__content .q-item, .q-item--dense"
        )
        v_lower = value.lower()
        item_texts = [(item, (await item.inner_text()).strip()) for item in items]

        # Pass 1: exact match
        for item, t in item_texts:
            if t.lower() == v_lower:
                await item.click()
                logger.info(f"DI: '{label_text}' = '{t}' выбрано (точное)")
                await asyncio.sleep(0.5)
                return True

        # Pass 2: partial match (shortest)
        partial = [(item, t) for item, t in item_texts if v_lower in t.lower()]
        if partial:
            item, t = min(partial, key=lambda x: len(x[1]))
            await item.click()
            logger.info(f"DI: '{label_text}' = '{t}' выбрано (частичное)")
            await asyncio.sleep(0.5)
            return True

        logger.error(f"DI: значение '{value}' не найдено в '{label_text}'")
        return False

    except Exception as e:
        logger.error(f"DI _pick_dropdown '{label_text}': {e}")
        return False


async def fill_form(page, bot, game_cfg: dict, di_params: dict) -> bool:
    """
    Fills G2G dropdowns for Diablo Immortal.

    di_params:
      region     — "EU"
      server     — "Akeba", "El' Druin", etc.
      level_tier — "60", "55+", "50+", ...
      di_class   — "Barbarian", "Blood Knight", etc.
    """
    server     = di_params.get("server", "")
    level_tier = di_params.get("level_tier", "")
    di_class   = di_params.get("di_class", "")

    # Region уже выбран g2g_bot._select_region до вызова game_handler

    if server:
        ok = await _pick_dropdown(page, "server", server)
        if not ok:
            logger.warning(f"DI: Server '{server}' не выбран — продолжаем")

    if level_tier:
        ok = await _pick_dropdown(page, "level", level_tier)
        if not ok:
            logger.warning(f"DI: Level '{level_tier}' не выбран — продолжаем")

    if di_class:
        ok = await _pick_dropdown(page, "class", di_class)
        if not ok:
            logger.warning(f"DI: Class '{di_class}' не выбран — продолжаем")

    return True
