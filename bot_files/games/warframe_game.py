"""
games/warframe_game.py — Warframe
G2G form filling:
  Server → PC / PS4 / Xbox One
  Rank   → MR tier (36, 35, ..., 26 - 30, ...)
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def _pick_dropdown(page, label_text: str, value: str, timeout: int = 8000) -> bool:
    """Универсальный выбор дропдауна G2G по тексту лейбла."""
    try:
        try:
            await page.wait_for_selector(".q-inner-loading", state="hidden", timeout=5000)
        except Exception:
            pass

        opened = False

        # Метод 1: через form-group с лейблом
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

        # Метод 2: fallback — первый незаполненный Please select
        if not opened:
            btns = await page.query_selector_all("button.g-btn-select")
            for btn in btns:
                txt = (await btn.inner_text()).strip().lower()
                if "please select" in txt:
                    await btn.click()
                    opened = True
                    break

        if not opened:
            logger.warning(f"WF: дропдаун '{label_text}' не найден")
            return False

        try:
            await page.wait_for_selector(
                ".q-virtual-scroll__content .q-item, .q-item--dense",
                timeout=timeout,
            )
        except Exception:
            pass
        await asyncio.sleep(0.3)

        # Фильтр если есть
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

        # Pass 1: точное совпадение
        for item, t in item_texts:
            if t.lower() == v_lower:
                await item.click()
                logger.info(f"WF: '{label_text}' = '{t}' выбрано (точное)")
                await asyncio.sleep(0.5)
                return True

        # Pass 2: частичное совпадение
        partial = [(item, t) for item, t in item_texts if v_lower in t.lower()]
        if partial:
            item, t = min(partial, key=lambda x: len(x[1]))
            await item.click()
            logger.info(f"WF: '{label_text}' = '{t}' выбрано (частичное)")
            await asyncio.sleep(0.5)
            return True

        logger.error(f"WF: значение '{value}' не найдено в '{label_text}'")
        return False

    except Exception as e:
        logger.error(f"WF _pick_dropdown '{label_text}': {e}")
        return False


async def fill_form(page, bot, game_cfg: dict, wf_params: dict) -> bool:
    """
    Заполняет дропдауны G2G формы для Warframe.

    wf_params:
      platform — "PC", "PS4", "Xbox One"
      rank     — "33", "26 - 30" и т.д. (из warframe_rank_tier)
    """
    platform = wf_params.get("platform", "PC")
    rank     = wf_params.get("rank", "")

    # ── 1. Server (платформа) ─────────────────────────────────────────────────
    ok = await _pick_dropdown(page, "server", platform)
    if not ok:
        logger.warning(f"WF: Server '{platform}' не выбран — продолжаем")

    # ── 2. Rank ───────────────────────────────────────────────────────────────
    if rank:
        ok = await _pick_dropdown(page, "rank", rank)
        if not ok:
            logger.warning(f"WF: Rank '{rank}' не выбран — продолжаем")

    return True
