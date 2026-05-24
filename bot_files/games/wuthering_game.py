"""
games/wuthering_game.py — Wuthering Waves
G2G form order:
  [0] Platform   → PC
  [1] Server     → EU
  [2] Union Level→ 80 / 70+ / 50+ / 30+ / 10+ / 9 or below
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def _pick_by_index(page, btn_index: int, value: str, label: str, timeout: int = 8000) -> bool:
    """Click the Nth g-btn-select button and pick a value from the dropdown."""
    try:
        try:
            await page.wait_for_selector(".q-inner-loading", state="hidden", timeout=5000)
        except Exception:
            pass

        btns = await page.query_selector_all("button.g-btn-select")
        if btn_index >= len(btns):
            logger.warning(f"WW: кнопка [{label}] (index={btn_index}) не найдена, всего кнопок: {len(btns)}")
            return False

        await btns[btn_index].click()
        await asyncio.sleep(1.2)

        try:
            await page.wait_for_selector(
                ".q-virtual-scroll__content .q-item, .q-item--dense",
                timeout=timeout,
            )
        except Exception:
            pass
        await asyncio.sleep(0.5)

        items = await page.query_selector_all(
            ".q-virtual-scroll__content .q-item, .q-item--dense"
        )
        v_lower = value.lower()
        item_texts = [(item, (await item.inner_text()).strip()) for item in items]

        # Exact match
        for item, t in item_texts:
            if t.lower() == v_lower:
                await item.click()
                logger.info(f"WW: [{label}] = '{t}' выбрано (точное)")
                await asyncio.sleep(0.5)
                return True

        # Partial match (shortest)
        partial = [(item, t) for item, t in item_texts if v_lower in t.lower()]
        if partial:
            item, t = min(partial, key=lambda x: len(x[1]))
            await item.click()
            logger.info(f"WW: [{label}] = '{t}' выбрано (частичное)")
            await asyncio.sleep(0.5)
            return True

        logger.error(f"WW: значение '{value}' не найдено в [{label}]")
        return False

    except Exception as e:
        logger.error(f"WW _pick_by_index [{label}] index={btn_index}: {e}")
        return False


async def fill_form(page, bot, game_cfg: dict, ww_params: dict) -> bool:
    """
    Fills G2G dropdowns for Wuthering Waves by button index:
      0 → Platform  = PC
      1 → Server    = EU
      2 → Union Level (dynamic)
    """
    union_level = ww_params.get("union_level", "")

    if not await _pick_by_index(page, 0, "PC",  "Platform"):
        return False
    if not await _pick_by_index(page, 1, "EU",  "Server"):
        return False
    if union_level:
        if not await _pick_by_index(page, 2, union_level, "Union Level"):
            return False

    return True
