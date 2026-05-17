# price_checker.py
# Модуль проверки и обновления цен WoW лотов.
# Вызывается из main.py через меню (кнопка p).

import asyncio
import json
import logging
import re as _re
from pathlib import Path
from playwright.async_api import Page, BrowserContext

import aiohttp

logger = logging.getLogger("price_checker")

LOT_PAIRS_FILE       = "lot_pairs.json"
WOW_GAME_NAME        = "WOW Classic Era / Seasonal / TBC Anniversary"
RAID_GAME_NAME       = "Raid: Shadow Legends"
RAID_LOTS_URL        = "https://funpay.com/en/lots/566/"
WOW_LOTS_URL         = "https://funpay.com/en/lots/492/"
PRICE_DIFF_THRESHOLD = 15.0
BASE                 = "https://www.g2g.com"

_FP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Пауза между HTTP-запросами к страницам листинга (защита от 429)
_LISTING_PAGE_DELAY  = 2.0
# Пауза перед браузерным fallback-визитом на индивидуальный лот
_BROWSER_VISIT_DELAY = 3.0


def load_pairs() -> dict:
    p = Path(LOT_PAIRS_FILE)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_pairs(data: dict):
    with open(LOT_PAIRS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_price_multiplier(price: float, tiers: list) -> float:
    for tier in tiers:
        if tier["min"] <= price < tier["max"]:
            return tier["multiplier"]
    return tiers[-1]["multiplier"] if tiers else 1.5


def _extract_price_digits(text: str) -> float:
    """Извлекает число из строки цены вида '104.70 $' или '270.61 USD'."""
    if not text:
        return 0.0
    clean = "".join(c for c in text if c.isdigit() or c in ".,")
    try:
        return float(clean.replace(",", "."))
    except Exception:
        return 0.0


async def _fetch_all_listing_prices(lots_url: str) -> dict[str, float]:
    """
    Загружает все страницы листинга FunPay через HTTP (без браузера).
    Возвращает {lot_id: price}. Намного быстрее чем 305 отдельных визитов.
    """
    prices: dict[str, float] = {}

    async with aiohttp.ClientSession(headers=_FP_HEADERS) as session:
        for page_num in range(1, 50):  # макс 50 страниц
            url = lots_url if page_num == 1 else f"{lots_url}?page={page_num}"
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=20),
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"  Листинг стр.{page_num}: HTTP {resp.status}")
                        break
                    html = await resp.text(errors="ignore")
            except Exception as e:
                logger.warning(f"  Листинг стр.{page_num}: {e}")
                break

            # Разбиваем на блоки по tc-item, ищем lot_id + цену в каждом
            chunks = html.split("tc-item")
            found_this_page = 0
            for chunk in chunks[1:]:
                m_id = _re.search(r'offer\?id=(\d+)', chunk)
                if not m_id:
                    continue

                price = 0.0

                # Ищем цену только внутри блока tc-price (не во всём chunk —
                # иначе data-s самого a.tc-item содержит ID лота, а не цену)
                tc_pos = chunk.find("tc-price")
                if tc_pos >= 0:
                    price_region = chunk[tc_pos:tc_pos + 300]

                    # Способ 1: data-s атрибут внутри tc-price
                    m_pr = _re.search(r'data-s="([\d.]+)"', price_region)
                    if m_pr:
                        try:
                            price = float(m_pr.group(1))
                        except ValueError:
                            pass

                    # Способ 2: текст внутри tc-price
                    if not price:
                        m_pr2 = _re.search(r'tc-price[^>]*>(.*?)</div>', price_region, _re.DOTALL)
                        if m_pr2:
                            m_num = _re.search(r'(\d+\.\d{2})', m_pr2.group(1))
                            if m_num:
                                try:
                                    price = float(m_num.group(1))
                                except ValueError:
                                    pass

                # Санитарная проверка: реальные цены аккаунтов в пределах $0.5–$9999
                if 0.5 <= price <= 9999.0:
                    prices[m_id.group(1)] = price
                    found_this_page += 1

            logger.info(f"  Листинг стр.{page_num}: +{found_this_page} цен ({len(prices)} всего)")

            if found_this_page == 0:
                break  # больше страниц нет

            await asyncio.sleep(_LISTING_PAGE_DELAY)

    return prices


async def _safe_goto(page: Page, url: str, retries: int = 3) -> bool:
    """page.goto с повтором при сетевых ошибках."""
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            return True
        except Exception as e:
            err = str(e)
            is_net = any(m in err for m in (
                "ERR_CONNECTION", "ERR_NETWORK", "ERR_TIMED_OUT",
                "ERR_NAME_NOT_RESOLVED", "Timeout", "net::"
            ))
            if not is_net or attempt == retries - 1:
                raise
            delay = 5 * (attempt + 1)
            logger.warning(f"  Сетевая ошибка, повтор через {delay}с [{attempt+1}/{retries}]")
            await asyncio.sleep(delay)
    return False


async def get_funpay_price(page: Page, funpay_url: str,
                           lots_url: str = WOW_LOTS_URL,
                           cached_prices: dict | None = None) -> float:
    """
    Возвращает цену лота FunPay.
    Сначала проверяет кеш листинга (быстро, без запроса).
    Если нет — открывает страницу лота напрямую (браузерный fallback).
    Возвращает -1.0 если лот продан.
    """
    if "id=" not in funpay_url:
        return 0.0
    lot_id = funpay_url.split("id=")[-1].split("&")[0]

    # ── Способ 1: кеш листинга (без запроса) ──────────────────────────────
    if cached_prices is not None and lot_id in cached_prices:
        price = cached_prices[lot_id]
        logger.info(f"  Цена с листинга: ${price:.2f}")
        return price

    # ── Способ 2: индивидуальная страница лота (браузер, медленно) ─────────
    logger.info(f"  Лот {lot_id} не на листинге → проверяем страницу лота")
    await asyncio.sleep(_BROWSER_VISIT_DELAY)

    try:
        await _safe_goto(page, funpay_url)
        await asyncio.sleep(2)

        content = await page.content()
        for marker in ["Offer not found", "offer has expired", "been deleted",
                       "never existed", "Предложение не найдено"]:
            if marker.lower() in content.lower():
                return -1.0

        result = await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('.tc-price')) {
                    const ds = el.getAttribute('data-s');
                    if (ds) { const v = parseFloat(ds); if (v > 0.5 && v < 100000) return ds; }
                    const text = (el.innerText || el.textContent || '').trim();
                    const m = text.match(/(\\d+(?:[.,]\\d{1,2})?)/);
                    if (m) { const v = parseFloat(m[1].replace(',', '.')); if (v > 0.5 && v < 100000) return m[1]; }
                }
                for (const el of document.querySelectorAll('[data-s]')) {
                    const cls = ((el.className || '') + ' ' +
                                 (el.parentElement ? el.parentElement.className || '' : '')).toLowerCase();
                    if (!cls.includes('price')) continue;
                    const ds = el.getAttribute('data-s');
                    const v = parseFloat(ds);
                    if (v > 0.5 && v < 100000) return ds;
                }
                const btns = document.querySelectorAll(
                    'button, .btn, .btn-buy, .form-control, h1, h2, h3, strong, span, .payment-method-balance'
                );
                for (const el of btns) {
                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text || text.length > 50) continue;
                    const m = text.match(/(\\d+(?:[.,]\\d{1,2})?)\\s*(?:\\$|USD|usd)/i)
                          || text.match(/[\\$]\\s*(\\d+(?:[.,]\\d{1,2})?)/);
                    if (m) { const v = parseFloat(m[1].replace(',', '.')); if (v > 0.5 && v < 100000) return m[1]; }
                }
                return null;
            }
        """)

        if result:
            price = _extract_price_digits(str(result))
            if price > 0:
                logger.info(f"  Цена со страницы лота: ${price:.2f}")
                return price

        logger.warning(f"  Цена не найдена на странице лота")
        return 0.0

    except Exception as e:
        logger.warning(f"  Ошибка получения цены: {e}")
        return 0.0


async def update_g2g_price(page: Page, g2g_id: str, new_price: float) -> bool:
    """Обновляет цену лота на G2G по его ID."""
    try:
        await page.goto(
            f"{BASE}/offers/list?cat_id=5830014a-b974-45c6-9672-b51e83112fb7&status=live",
            wait_until="domcontentloaded", timeout=40000,
        )
        await asyncio.sleep(3)

        try:
            await page.wait_for_selector(
                "input[placeholder='Search title or offer number']", timeout=15000
            )
        except Exception:
            logger.warning("  Поле поиска не появилось")
            return False

        search = await page.query_selector("input[placeholder='Search title or offer number']")
        if not search:
            logger.warning("  Поле поиска не найдено")
            return False

        await search.click()
        await page.keyboard.press("Control+a")
        await search.fill(g2g_id)
        await asyncio.sleep(4)

        try:
            await page.wait_for_selector("tbody tr", timeout=8000)
        except Exception:
            logger.warning("  Строка таблицы не появилась")
            return False
        await asyncio.sleep(1)

        price_link = await page.query_selector("tbody tr .q-gutter-xs .base-hyperlink")
        if not price_link:
            spans = await page.query_selector_all("tbody tr span.base-hyperlink.cursor-pointer")
            for span in spans:
                text = (await span.inner_text()).strip()
                import re as _re2
                if _re2.match(r'^\d{2,6}\.\d{2}$', text):
                    price_link = span
                    break
        if not price_link:
            logger.warning(f"  Ссылка на цену не найдена для {g2g_id}")
            return False

        price_text = (await price_link.inner_text()).strip()
        logger.info(f"  Нашли цену: '{price_text}' — кликаем")
        await price_link.click()

        try:
            await page.wait_for_selector(".q-dialog:not(.q-dialog--seamless)", timeout=8000)
            logger.info("  Диалог Price setting открылся")
        except Exception:
            logger.warning("  Диалог не появился")
            return False
        await asyncio.sleep(1)

        price_input = None
        for sel in [".q-dialog input.q-field__native", ".q-dialog input[type='text']"]:
            price_input = await page.query_selector(sel)
            if price_input:
                break

        if not price_input:
            logger.warning("  Поле ввода цены не найдено в диалоге")
            return False

        price_str = str(round(new_price, 2))
        await page.evaluate("""
            (args) => {
                const el = args.el;
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeSetter.call(el, args.value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """, {"el": price_input, "value": price_str})
        logger.info(f"  Ввели цену: {price_str}")
        await asyncio.sleep(0.5)

        update_btn = None
        for btn in await page.query_selector_all(".q-dialog button"):
            if (await btn.inner_text()).strip() == "Update":
                update_btn = btn
                break

        if not update_btn:
            logger.warning("  Кнопка Update не найдена в диалоге")
            return False

        logger.info("  Нажимаем Update")
        await page.evaluate("(el) => el.click()", update_btn)
        await asyncio.sleep(3)

        ok_btn = None
        for btn in await page.query_selector_all(".q-dialog button"):
            if (await btn.inner_text()).strip() == "Ok":
                ok_btn = btn
                break

        if not ok_btn:
            logger.warning("  Кнопка Ok не найдена в диалоге")
            return False

        logger.info("  Нажимаем Ok")
        await page.evaluate("(el) => el.click()", ok_btn)
        await asyncio.sleep(1)
        logger.info(f"  Цена успешно обновлена: ${new_price:.2f}")
        return True

    except Exception as e:
        logger.error(f"  Ошибка обновления G2G: {e}")
        return False


async def run_price_check(context: BrowserContext, tiers: list,
                          lots_url: str = WOW_LOTS_URL,
                          game_name: str = WOW_GAME_NAME,
                          fp_factor: float = 1.0):
    """Универсальная проверка цен — работает для WoW и Raid."""
    pairs_dict = load_pairs()
    pairs      = pairs_dict.get(game_name, [])

    if not pairs:
        logger.info(f"Нет лотов для [{game_name}]")
        return

    logger.info(f"\n{'='*60}")
    logger.info(f"ПРОВЕРКА ЦЕН | {game_name}")
    logger.info(f"{len(pairs)} лотов | порог ${PRICE_DIFF_THRESHOLD:.0f}")
    logger.info(f"{'='*60}\n")

    # ── Шаг 1a: загружаем листинг одним HTTP-запросом ────────────────────
    logger.info("── Шаг 1a: загружаем листинг FunPay (HTTP, все страницы)...")
    cached_prices = await _fetch_all_listing_prices(lots_url)
    logger.info(f"  Итого в кеше: {len(cached_prices)} цен\n")

    fp_page  = await context.new_page()
    g2g_page = await context.new_page()

    to_update = []
    migrated = skipped = updated = from_cache = from_browser = 0

    # ── Шаг 1b: проверяем все цены ───────────────────────────────────────
    logger.info("── Шаг 1b: сверяем цены...\n")

    for i, pair in enumerate(pairs):
        fp_id      = pair["funpay_id"]
        title      = pair.get("title", "")[:55]
        funpay_url = pair.get("funpay_url") or f"https://funpay.com/en/lots/offer?id={fp_id}"
        stored_fp  = float(pair.get("funpay_price", 0))
        stored_g2g = float(pair.get("g2g_price", 0))

        logger.info(f"[{i+1}/{len(pairs)}] {title}")

        current_fp = await get_funpay_price(fp_page, funpay_url, lots_url, cached_prices)

        # Статистика источника
        lot_id = funpay_url.split("id=")[-1].split("&")[0] if "id=" in funpay_url else ""
        if lot_id in cached_prices:
            from_cache += 1
        else:
            from_browser += 1

        if current_fp == -1.0:
            logger.info(f"  Лот продан - пропускаем")
            skipped += 1
            continue

        if current_fp <= 0:
            logger.warning(f"  Цена не найдена - пропускаем")
            skipped += 1
            continue

        if stored_fp <= 0:
            effective_fp = round(current_fp * fp_factor, 2)
            new_g2g = round(effective_fp * get_price_multiplier(effective_fp, tiers), 2)
            pair["funpay_price"] = current_fp
            pair["g2g_price"]    = new_g2g
            logger.info(f"  Миграция: FP=${current_fp:.2f} x{fp_factor}=${effective_fp:.2f} G2G=${new_g2g:.2f} (G2G не трогаем)")
            migrated += 1
            continue

        diff      = abs(current_fp - stored_fp)
        direction = "v" if current_fp < stored_fp else "^"
        logger.info(f"  FP: ${stored_fp:.2f} {direction} ${current_fp:.2f} | diff=${diff:.2f}")

        if diff < PRICE_DIFF_THRESHOLD:
            logger.info(f"  OK изменение < ${PRICE_DIFF_THRESHOLD:.0f} - пропускаем")
            skipped += 1
            continue

        effective_fp = round(current_fp * fp_factor, 2)
        new_g2g = round(effective_fp * get_price_multiplier(effective_fp, tiers), 2)
        logger.info(f"  Нужно обновить: ${stored_g2g:.2f} -> ${new_g2g:.2f} (FP x{fp_factor}=${effective_fp:.2f})")
        to_update.append((pair, current_fp, new_g2g))

    await fp_page.close()

    logger.info(f"\n  Источник цен: кеш={from_cache} | браузер={from_browser}")

    # ── Шаг 2: обновляем цены на G2G ─────────────────────────────────────
    if to_update:
        logger.info(f"\n── Шаг 2: обновляем {len(to_update)} лотов на G2G...\n")
        for pair, current_fp, new_g2g in to_update:
            g2g_id = pair["g2g_id"]
            title  = pair.get("title", "")[:55]
            logger.info(f"Обновляем: {title}")
            ok = await update_g2g_price(g2g_page, g2g_id, new_g2g)
            if ok:
                pair["funpay_price"] = current_fp
                pair["g2g_price"]    = new_g2g
                updated += 1
            else:
                logger.warning(f"  Не удалось обновить")
    else:
        logger.info(f"\n── Шаг 2: нет лотов для обновления")

    await g2g_page.close()

    if migrated > 0 or updated > 0:
        pairs_dict[game_name] = pairs
        save_pairs(pairs_dict)
        logger.info("lot_pairs.json сохранён")

    logger.info(f"\n{'='*60}")
    logger.info(f"ГОТОВО | Обновлено: {updated} | Мигрировано: {migrated} | Пропущено: {skipped}")
    logger.info(f"{'='*60}")


async def run_price_check_wow(context: BrowserContext, tiers: list, fp_factor: float = 1.0):
    await run_price_check(context, tiers, lots_url=WOW_LOTS_URL, game_name=WOW_GAME_NAME, fp_factor=fp_factor)


async def run_price_check_raid(context: BrowserContext, tiers: list, fp_factor: float = 1.0):
    await run_price_check(context, tiers, lots_url=RAID_LOTS_URL, game_name=RAID_GAME_NAME, fp_factor=fp_factor)
