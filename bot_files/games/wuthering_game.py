"""
games/wuthering_game.py — Wuthering Waves
G2G form:
  Platform   → PC
  Server     → EU
  Union Level→ 80 / 70+ / 50+ / 30+ / 10+ / 9 or below
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
                    await asyncio.sleep(1.2)
                    opened = True
                    break

        # Method 2: fallback — Union Level is LAST "Please select"
        # Order: Platform(0) → Server(1) → Union Level(2)
        if not opened:
            btns = await page.query_selector_all("button.g-btn-select")
            please = [btn for btn in btns
                      if "please select" in (await btn.inner_text()).strip().lower()]
            if please:
                # Always click the LAST remaining "Please select" = Union Level
                await please[-1].click()
                await asyncio.sleep(1.2)
                opened = True

        if not opened:
            logger.warning(f"WW: дропдаун '{label_text}' не найден")
            return False

        try:
            await page.wait_for_selector(
                ".q-virtual-scroll__content .q-item, .q-item--dense",
                timeout=timeout,
            )
        except Exception:
            pass
        await asyncio.sleep(0.5)

        # No filter input — directly pick from visible items
        items = await page.query_selector_all(
            ".q-virtual-scroll__content .q-item, .q-item--dense"
        )
        v_lower = value.lower()
        item_texts = [(item, (await item.inner_text()).strip()) for item in items]

        for item, t in item_texts:
            if t.lower() == v_lower:
                await item.click()
                logger.info(f"WW: '{label_text}' = '{t}' выбрано (точное)")
                await asyncio.sleep(0.5)
                return True

        partial = [(item, t) for item, t in item_texts if v_lower in t.lower()]
        if partial:
            item, t = min(partial, key=lambda x: len(x[1]))
            await item.click()
            logger.info(f"WW: '{label_text}' = '{t}' выбрано (частичное)")
            await asyncio.sleep(0.5)
            return True

        logger.error(f"WW: значение '{value}' не найдено в '{label_text}'")
        return False

    except Exception as e:
        logger.error(f"WW _pick_dropdown '{label_text}': {e}")
        return False


async def fill_form(page, bot, game_cfg: dict, ww_params: dict) -> bool:
    """
    Fills G2G dropdowns for Wuthering Waves.

    ww_params:
      union_level — "80", "70+", "50+", "30+", "10+", "9 or below"
    """
    union_level = ww_params.get("union_level", "")

    # Platform and Server are set via g2g_dropdowns in config
    # Only Union Level needs dynamic selection
    if union_level:
        ok = await _pick_dropdown(page, "union level", union_level)
        if not ok:
            logger.warning(f"WW: Union Level '{union_level}' не выбран — продолжаем")

    return True
