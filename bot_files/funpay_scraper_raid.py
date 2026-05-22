import logging
import re
import asyncio
import aiohttp
import random
import json as _json
from typing import Optional
from dataclasses import dataclass, field
from pathlib import Path
from playwright.async_api import BrowserContext, Page, async_playwright

logger = logging.getLogger("funpay")

POSTIMAGES_COOKIES_FILE = "cookies_postimages.json"


def parse_proxy(proxy_str: str) -> Optional[dict]:
    """Парсим прокси из строки ip:port:login:password."""
    if not proxy_str:
        return None
    parts = proxy_str.strip().split(":")
    if len(parts) == 4:
        ip, port, login, password = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": login,
            "password": password,
        }
    elif len(parts) == 2:
        ip, port = parts
        return {"server": f"http://{ip}:{port}"}
    return None


def is_blacklisted(
    title: str,
    description: str,
    blacklist: list,
    whitelist: list = None,
    detailed_description: str = "",
) -> tuple[bool, str]:
    # Проверяем title + short description + detailed description
    text = (title + " " + description + " " + (detailed_description or "")).lower()
    whitelist = whitelist or []
    for word in blacklist:
        if word.lower() in text:
            whitelisted = any(allowed.lower() in text for allowed in whitelist)
            if not whitelisted:
                return True, word
    return False, ""


@dataclass
class FunPayLot:
    price: float
    title: str          # заголовок карточки с FunPay
    description: str    # Short description со страницы лота
    href: str = ""
    lot_id: str = ""
    level: int = 0
    m_heroes: int = 0
    l_heroes: int = 0
    photos: list = field(default_factory=list)
    detailed_description: str = ""  # Detailed description со страницы лота
    region: str = ""    # data-f-region с карточки лота (Europe / Asia / America / TW, HC, MO)
    bdo_class: str = "" # data-f-class с карточки лота (EN: Guardian, Ninja...)
    server: str = ""    # Server param-item со страницы лота (напр. "(EU) Usurper", "(EU)")
    rank: int = 0       # Rank param-item со страницы лота (33, 28...)
    platform: str = ""  # Platform param-item со страницы лота (PC, PS4, Xbox One)
    char_class: str = "" # Class param-item со страницы лота (Dragonknight, Ranger...)


# Mapping игры -> hex галереи на postimages
# Открой postimages.org/gallery/XXXXX и скопируй XXXXX из URL
POSTIMAGES_GALLERY_HEX = {
    "raid":      "YGFKmWd",
    "wow":       "4GzgRHG",
    "zenless":   "qnThCC8",
    "eve":       "ZSGfj5P",
    "throne":    "VL18dTT",
    "black":     "0yV08VV",
    "summoners": "MqQH3TR",
    "rbl":       "14stMdJ",
    "warframe":   "mbMXTds",
    "drakensang": "GvdF4Jm",
    "diablo":     "9mMTnHy",
}


async def upload_to_postimages(image_path: str, context, game: str = "") -> Optional[str]:
    """
    Загружаем фото на Postimages через браузер.
    Если передан game — открываем страницу нужной галереи и грузим туда.
    """
    page = await context.new_page()
    try:
        # Загружаем куки для авторизации
        cookies_file = Path(POSTIMAGES_COOKIES_FILE)
        if cookies_file.exists():
            try:
                cookies = _json.loads(cookies_file.read_text(encoding="utf-8"))
                await context.add_cookies(cookies)
            except Exception as e:
                logger.warning(f"Postimages: ошибка загрузки cookies: {e}")

        await asyncio.sleep(random.uniform(1, 2))

        # Определяем в какую галерею грузить
        gallery_hex = None
        game_lower = game.lower()
        for key, hex_val in POSTIMAGES_GALLERY_HEX.items():
            if key in game_lower:
                gallery_hex = hex_val
                break

        # Всегда открываем главную — там работает #ddinput
        logger.info(f"Postimages: открываем главную страницу...")
        await page.goto("https://postimages.org/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(1, 2))

        page_content = await page.content()
        if "заблокированы" in page_content or "blocked" in page_content.lower():
            logger.warning("Postimages: заблокировано")
            return None

        # Если нужна галерея — выбираем её через select/dropdown на главной
        if gallery_hex:
            # Логируем все options чтобы понять структуру dropdown
            all_options = await page.evaluate("""
                () => {
                    const sel = document.querySelector('select[name="gallery"], select#gallery, select');
                    if (!sel) return 'NO SELECT FOUND';
                    return Array.from(sel.options).map(o => o.value + '|' + o.text).join(', ');
                }
            """)
            logger.info(f"Postimages: dropdown options -> {all_options}")

            selected = await page.evaluate(f"""
                () => {{
                    const sel = document.querySelector('select[name="gallery"], select#gallery');
                    if (sel) {{
                        for (const opt of sel.options) {{
                            if (opt.value === '{gallery_hex}' || opt.text.toLowerCase().includes('{gallery_hex}')) {{
                                sel.value = opt.value;
                                sel.dispatchEvent(new Event('change'));
                                return opt.value + '|' + opt.text;
                            }}
                        }}
                    }}
                    return null;
                }}
            """)
            if selected:
                logger.info(f"Postimages: галерея выбрана -> {selected}")
            else:
                logger.warning(f"Postimages: галерея {gallery_hex} не найдена в dropdown")

        logger.info(f"Postimages: загружаем {image_path}...")
        async with page.expect_file_chooser() as fc_info:
            await page.click("#ddinput")
        file_chooser = await fc_info.value
        await file_chooser.set_files(image_path)

        # Адаптивный polling: сначала часто, потом реже.
        # Обычно фото появляется за 1-4 сек → не ждём 30 сек зря.
        _poll_delays = [0.5, 0.5, 1.0, 1.0, 1.5, 2.0, 2.0, 2.0, 3.0, 3.0]
        _elapsed = 0.0
        for i, delay in enumerate(_poll_delays):
            await asyncio.sleep(delay)
            _elapsed += delay
            url = await page.evaluate("""
                () => {
                    const inputs = document.querySelectorAll('input')
                    for(const inp of inputs) {
                        if(inp.value && inp.value.includes('i.postimg.cc')) return inp.value
                    }
                    return null
                }
            """)
            if url:
                logger.info(f"Postimages: Direct link -> {url} (за {_elapsed:.1f}с)")
                return url

            content = await page.content()
            if "заблокированы" in content or "blocked" in content.lower():
                logger.warning("Postimages: обнаружена блокировка")
                return None

            logger.info(f"Postimages: ждём... ({i+1}/{len(_poll_delays)}, {_elapsed:.1f}с)")

        logger.warning("Postimages: ссылка не получена")
        return None

    except Exception as e:
        logger.warning(f"Postimages: ошибка: {e}")
        return None
    finally:
        await page.close()


async def upload_to_imgur(image_path: str, imgur_proxy: str = "") -> Optional[str]:
    """Загружаем фото на Imgur через браузер с прокси."""
    proxy = parse_proxy(imgur_proxy)

    async with async_playwright() as pw:
        launch_args = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        if proxy:
            launch_args["proxy"] = proxy

        browser = await pw.chromium.launch(**launch_args)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
        )

        # Загружаем cookies Imgur если есть
        imgur_cookies_file = Path("cookies_imgur.json")
        if imgur_cookies_file.exists():
            try:
                cookies = _json.loads(imgur_cookies_file.read_text(encoding="utf-8"))
                await context.add_cookies(cookies)
                logger.info("Imgur: cookies загружены OK")
            except Exception as e:
                logger.warning(f"Imgur: ошибка загрузки cookies: {e}")

        page = await context.new_page()
        try:
            logger.info(f"Imgur: открываем страницу загрузки...")
            await page.goto("https://imgur.com/upload", wait_until="domcontentloaded", timeout=40000)
            await asyncio.sleep(3)

            # Загружаем файл через file input
            logger.info(f"Imgur: загружаем {image_path}...")
            file_input = await page.query_selector("input[type='file']")
            if not file_input:
                logger.warning("Imgur: file input не найден")
                return None

            await file_input.set_input_files(image_path)
            await asyncio.sleep(5)

            # Ищем прямую ссылку на фото
            for i in range(15):
                await asyncio.sleep(2)
                url = await page.evaluate("""
                    () => {
                        const imgs = document.querySelectorAll('img[src*="i.imgur.com"]')
                        for(const img of imgs) {
                            if(img.src && img.src.includes('i.imgur.com') && !img.src.includes('favicon'))
                                return img.src
                        }
                        return null
                    }
                """)
                if url:
                    logger.info(f"Imgur: Direct link -> {url}")
                    return url
                logger.info(f"Imgur: ждём... ({i+1}/15)")

            logger.warning("Imgur: ссылка не получена")
            return None

        except Exception as e:
            logger.warning(f"Imgur: ошибка: {e}")
            return None
        finally:
            await page.close()
            await context.close()
            await browser.close()


class FunPayScraper:
    def __init__(self, context: BrowserContext, imgur_proxy: str = ""):
        self.context = context
        self.imgur_proxy = imgur_proxy
        self._currency_set = False

    async def _set_language_and_currency(self, page: Page):
        if self._currency_set:
            return
        try:
            lang_btn = await page.query_selector(".menu-item-langs")
            lang_text = (await lang_btn.inner_text()).strip() if lang_btn else ""

            if "english" not in lang_text.lower():
                logger.info("FunPay: переключаем язык на English...")
                await lang_btn.click()
                await asyncio.sleep(1)
                await page.click("a.menu-item-lang:has-text('English')")
                await asyncio.sleep(2)
                logger.info("FunPay: язык -> English OK")
            else:
                logger.info("FunPay: язык уже English OK")

            cur_btn = await page.query_selector(".menu-item-currencies")
            cur_text = (await cur_btn.inner_text()).strip() if cur_btn else ""

            if "usd" not in cur_text.lower():
                logger.info("FunPay: переключаем валюту на USD...")
                await cur_btn.click()
                await asyncio.sleep(1)
                await page.click("a.menu-item-currency:has-text('USD')")
                await asyncio.sleep(2)
                logger.info("FunPay: валюта -> USD OK")
            else:
                logger.info("FunPay: валюта уже USD OK")

            self._currency_set = True
        except Exception as e:
            logger.warning(f"FunPay: ошибка настройки: {e}")
            self._currency_set = True

    async def check_lot_sold(self, funpay_url: str) -> bool:
        page = await self.context.new_page()
        try:
            logger.info(f"FunPay: проверяем лот -> {funpay_url}")
            await page.goto(funpay_url, wait_until="domcontentloaded", timeout=40000)
            # Ждём появления контента или ошибки — не фиксированные 2 сек
            try:
                await page.wait_for_selector("body", timeout=3000)
            except Exception:
                pass

            content = await page.content()
            sold_markers = [
                "Offer not found", "offer has expired", "been deleted",
                "never existed", "Предложение не найдено", "устарело",
                "было удалено", "не существовало",
            ]

            for marker in sold_markers:
                if marker.lower() in content.lower():
                    logger.info(f"FunPay: лот ПРОДАН (маркер: '{marker}')")
                    return True

            title = await page.title()
            if "not found" in title.lower() or "404" in title:
                logger.info("FunPay: лот ПРОДАН (404)")
                return True

            logger.info("FunPay: лот активен")
            return False

        except Exception as e:
            logger.warning(f"FunPay: ошибка проверки: {e}")
            return False
        finally:
            await page.close()

    async def _upload_photo_aiohttp(self, image_path: str, game: str = "") -> Optional[str]:
        """Быстрая загрузка фото на Postimages через aiohttp (без браузера)."""
        gallery_hex = None
        game_lower = game.lower()
        for key, hex_val in POSTIMAGES_GALLERY_HEX.items():
            if key in game_lower:
                gallery_hex = hex_val
                break
        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()
            ext = Path(image_path).suffix.lstrip(".") or "jpg"
            content_type = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://postimages.org/web",
                "Origin": "https://postimages.org",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }
            cookies = {
                "GUESTKEY": "muidn p85O9xYA3li4",
                "SESSIONKEY": "a99ef415574e36289d27e44853c09e5927a5d4ce077da68ca8cb4de8457c2f09",
            }
            endpoint = (
                f"https://postimages.org/json/rr/{gallery_hex}"
                if gallery_hex else
                "https://postimages.org/web"
            )
            async with aiohttp.ClientSession(headers=headers, cookies=cookies) as session:
                form = aiohttp.FormData()
                form.add_field("file", img_bytes, filename=f"upload.{ext}", content_type=content_type)
                form.add_field("resize", "0")
                form.add_field("expire", "0")
                async with session.post(
                    endpoint, data=form,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    html = await resp.text()
            patterns = [
                r'"url"\s*:\s*"(https?://i\.postimg\.cc/[^"]+)"',
                r'(https?://i\.postimg\.cc/[^\s"\'<>]+)',
            ]
            for pat in patterns:
                m = re.search(pat, html, re.IGNORECASE)
                if m:
                    link = m.group(1)
                    logger.info(f"Postimages aiohttp: OK -> {link}")
                    return link
            logger.warning(f"Postimages aiohttp: ссылка не найдена ({html[:200]})")
            return None
        except Exception as e:
            logger.warning(f"Postimages aiohttp: ошибка ({e})")
            return None

    async def _upload_photo(self, image_path: str, game: str = "") -> Optional[str]:
        """Браузер (Postimages), при неудаче — Imgur."""
        url = await upload_to_postimages(image_path, self.context, game=game)
        if url:
            return url

        logger.info("Postimages не сработал — пробуем Imgur...")
        url = await upload_to_imgur(image_path, self.imgur_proxy)
        if url:
            logger.info(f"Imgur: загружено -> {url}")
            return url

        logger.warning("Все сервисы не сработали — фото пропускаем")
        return None

    async def _get_lot_details(self, lot_url: str):
        """Получает описание ТОЛЬКО из блока 'Short description' (вне .row) и ссылки на фото."""
        page = await self.context.new_page()
        try:
            await page.goto(lot_url, wait_until="domcontentloaded", timeout=40000)
            # Ждём конкретный элемент вместо фиксированного sleep
            try:
                await page.wait_for_selector(".param-item", timeout=5000)
            except Exception:
                pass  # страница без .param-item тоже валидна

            # Ищем блок Short description, явно исключая .param-item внутри .row
            # (там находятся Mythical champions, Legendary champions, Account level — брать ЗАПРЕЩЕНО)
            result = await page.evaluate("""
                () => {
                    let short_desc = '';
                    let detailed_desc = '';
                    let server = '';
                    let rank_val = '';
                    let platform_val = '';
                    let class_val = '';
                    const items = document.querySelectorAll('.param-item');
                    for (const item of items) {
                        const h5 = item.querySelector('h5');
                        if (!h5) continue;
                        const key = h5.innerText.trim().toLowerCase();
                        const div = item.querySelector('div');
                        const val = div ? div.innerText.trim() : '';
                        if (key === 'short description') short_desc = val;
                        if (key === 'detailed description') detailed_desc = val;
                        if (key === 'server') server = val;
                        if (key === 'rank') rank_val = val;
                        if (key === 'platform') platform_val = val;
                        if (key === 'class') class_val = val;
                    }
                    return {short: short_desc, detailed: detailed_desc, server: server, rank: rank_val, platform: platform_val, char_class: class_val};
                }
            """)
            description = result.get('short', '') or ''
            detailed_description = result.get('detailed', '') or ''
            lot_server = result.get('server', '') or ''
            lot_rank = int(result.get('rank', '') or 0) if str(result.get('rank', '') or '').isdigit() else 0
            lot_platform = result.get('platform', '') or ''
            lot_char_class = result.get('char_class', '') or ''

            # Фото
            photo_els = await page.query_selector_all(".attachments-thumb")
            photo_urls = []
            for el in photo_els[:4]:
                href = await el.get_attribute("href")
                if href:
                    photo_urls.append(href)

            logger.info(f"FunPay: Short ({len(description)} симв.) | Detailed ({len(detailed_description)} симв.) | фото: {len(photo_urls)} | server: {lot_server!r} | rank: {lot_rank} | platform: {lot_platform!r} | class: {lot_char_class!r}")
            return description, detailed_description, photo_urls, lot_server, lot_rank, lot_platform, lot_char_class

        except Exception as e:
            logger.warning(f"FunPay: ошибка деталей лота: {e}")
            return "", "", [], ""
        finally:
            await page.close()

    async def _download_photos(self, photo_urls: list, lot_id: str) -> list:
        import shutil
        tmp_dir = Path(f"images/tmp/{lot_id}")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        paths = []
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            for i, url in enumerate(photo_urls):
                try:
                    async with session.get(
                        url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status == 200:
                            ext = url.split(".")[-1].split("?")[0] or "jpg"
                            path = tmp_dir / f"photo_{i}.{ext}"
                            with open(path, "wb") as f:
                                f.write(await resp.read())
                            paths.append(str(path))
                            logger.info(f"FunPay: скачано фото {i+1} -> {path}")
                except Exception as e:
                    logger.debug(f"Ошибка скачивания фото: {e}")

        return paths

    async def get_best_lot(
        self,
        lots_url: str,
        min_seller_reviews: int = 0,
        require_auto_delivery: bool = False,
        min_price_usd: float = 0.0,
        max_price_usd: float = 999999.0,
        used_lot_ids: set = None,
        title_blacklist: list = None,
        builtin_blacklist: list = None,
        blacklist_whitelist: list = None,
        seller_blacklist: list = None,
        game: str = "",
        region_filter: str = "",
        server_whitelist: list = None,
        funpay_tab: str = "",
        skip_photos: bool = False,
    ) -> Optional[FunPayLot]:
        page = await self.context.new_page()

        full_blacklist = (title_blacklist or []) + (builtin_blacklist or [])
        whitelist = blacklist_whitelist or []
        seller_blacklist = [s.lower() for s in (seller_blacklist or [])]

        try:
            logger.info(f"FunPay: открываем {lots_url}")
            await page.goto(lots_url, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_selector("a.tc-item", timeout=5000)
            except Exception:
                pass

            _was_currency_set = self._currency_set
            await self._set_language_and_currency(page)

            # Перезагружаем только если только что сменили язык/валюту
            if not _was_currency_set:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_selector("a.tc-item", timeout=5000)
                except Exception:
                    pass

            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 1500)")
                await asyncio.sleep(0.3)
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.3)

            # Фильтр по региону (например "Europe" для ZZZ)
            if region_filter:
                try:
                    await page.select_option("select[name='f-region']", region_filter)
                    # Ждём обновления листинга после смены региона
                    try:
                        await page.wait_for_selector("a.tc-item", timeout=3000)
                    except Exception:
                        await asyncio.sleep(1)
                    logger.info(f"FunPay: регион → '{region_filter}'")
                except Exception as _re:
                    logger.warning(f"FunPay: регион '{region_filter}' не выбран: {_re}")

            # Переключаемся на нужную вкладку (например "Accounts" для Roblox)
            if funpay_tab:
                try:
                    tab_el = (
                        await page.query_selector(f'button.btn:has-text("{funpay_tab}")') or
                        await page.query_selector(f'a.pill-btn:has-text("{funpay_tab}")')
                    )
                    if tab_el:
                        await tab_el.click()
                        # FunPay фильтрует через JS без сетевых запросов — ждём перерисовку DOM
                        await asyncio.sleep(2)
                        logger.info(f"FunPay: нажата вкладка '{funpay_tab}'")
                    else:
                        logger.warning(f"FunPay: вкладка '{funpay_tab}' не найдена")
                except Exception as _te:
                    logger.warning(f"FunPay: ошибка при выборе вкладки '{funpay_tab}': {_te}")

            # Включаем фильтр "Automatic delivery" на странице FunPay
            if require_auto_delivery:
                try:
                    # Кликаем по label, а не по скрытому input
                    auto_label = await page.query_selector('label:has(input[name="auto"])')
                    auto_input = await page.query_selector('input[name="auto"]')
                    if auto_input:
                        is_checked = await auto_input.is_checked()
                        if not is_checked:
                            if auto_label:
                                await auto_label.click()
                            else:
                                await auto_input.click()
                            await asyncio.sleep(2)
                            logger.info("FunPay: чекбокс 'Automatic delivery' включён")
                        else:
                            logger.info("FunPay: чекбокс 'Automatic delivery' уже включён")
                    else:
                        logger.warning("FunPay: чекбокс 'Automatic delivery' не найден")
                except Exception as _ae:
                    logger.warning(f"FunPay: ошибка при включении 'Automatic delivery': {_ae}")

            items = await page.query_selector_all("a.tc-item")
            logger.info(f"FunPay: найдено карточек: {len(items)}")

            used_lot_ids = used_lot_ids or set()

            for item in items:
                try:
                    # Цена
                    price_el = await item.query_selector(".tc-price")
                    if not price_el:
                        continue
                    data_s = await price_el.get_attribute("data-s")
                    try:
                        price = float(data_s) if data_s else None
                    except ValueError:
                        price = None
                    if price is None or price <= 0:
                        continue
                    if price < min_price_usd or price > max_price_usd:
                        continue

                    # Ссылка и ID
                    lot_href = await item.get_attribute("href") or ""
                    lot_id = ""
                    if "id=" in lot_href:
                        lot_id = lot_href.split("id=")[-1].split("&")[0]

                    # Используем href как запасной ключ когда lot_id пустой
                    _check_id = lot_id or lot_href
                    if _check_id and _check_id in used_lot_ids:
                        continue

                    # Ник продавца
                    if seller_blacklist:
                        seller_el = await item.query_selector(".media-user-name")
                        seller = (await seller_el.inner_text()).strip().lower() if seller_el else ""
                        if seller and seller in seller_blacklist:
                            logger.info(f"FunPay: пропускаем продавца '{seller}'")
                            continue

                    # Отзывы
                    if min_seller_reviews > 0:
                        reviews_el = await item.query_selector(".rating-mini-count")
                        reviews = 0
                        if reviews_el:
                            rt = await reviews_el.inner_text()
                            m = re.search(r"\d+", rt.replace(" ", "").replace(",", ""))
                            reviews = int(m.group(0)) if m else 0
                        if reviews < min_seller_reviews:
                            continue

                    # Автодоставка
                    if require_auto_delivery:
                        html = await item.inner_html()
                        if not any(x in html.lower() for x in ["auto", "autodelivery"]):
                            continue

                    # Название
                    title_el = await item.query_selector(".tc-desc-text")
                    title = (await title_el.inner_text()).strip() if title_el else ""

                    # Страховка: FunPay добавляет ", Категория" в конец названия.
                    # Если вкладка задана — пропускаем лоты не той категории.
                    if funpay_tab and title:
                        if f", {funpay_tab}" not in title:
                            logger.debug(f"FunPay: пропускаем (не {funpay_tab}): {title[:50]}")
                            continue

                    # Быстрая проверка title
                    if full_blacklist:
                        blocked, word = is_blacklisted(title, "", full_blacklist, whitelist)
                        if blocked:
                            logger.info(f"FunPay: пропускаем (blacklist: '{word}') -> {title[:50]}")
                            continue

                    # Data атрибуты
                    level     = int(await item.get_attribute("data-f-level") or 0)
                    m_heroes  = int(await item.get_attribute("data-f-mhero") or 0)
                    l_heroes  = int(await item.get_attribute("data-f-lhero") or 0)
                    bdo_class = (await item.get_attribute("data-f-class") or "").strip()
                    card_server = (await item.get_attribute("data-f-server") or "").strip()

                    # Фильтр по серверу на уровне карточки (до загрузки страницы лота)
                    if server_whitelist and card_server:
                        srv_lower = card_server.lower().strip()
                        # Точное совпадение ИЛИ вхождение (для TL: "usurper" in "(eu) usurper")
                        if not any(s.lower() == srv_lower or s.lower() in srv_lower for s in server_whitelist):
                            logger.info(
                                f"FunPay: сервер карточки '{card_server}' не в whitelist — пропускаем {lot_id}"
                            )
                            continue

                    # Описание и фото
                    description = title
                    detailed_description = ""
                    photo_urls = []
                    lot_server = ""
                    lot_rank = 0
                    lot_platform = ""
                    lot_char_class = ""
                    if lot_href:
                        lot_url = lot_href if lot_href.startswith("http") else f"https://funpay.com{lot_href}"
                        description, detailed_description, photo_urls, lot_server, lot_rank, lot_platform, lot_char_class = await self._get_lot_details(lot_url)
                        description = description or title

                    # Полная проверка title + short description + detailed description
                    if full_blacklist:
                        blocked, word = is_blacklisted(
                            title, description, full_blacklist, whitelist,
                            detailed_description=detailed_description,
                        )
                        if blocked:
                            logger.info(
                                f"FunPay: пропускаем (blacklist desc: '{word}') -> {title[:50]}"
                            )
                            continue

                    # Фильтр по серверу со страницы лота (для TL где card_server пустой)
                    if server_whitelist and not card_server and lot_server:
                        srv_lower = lot_server.lower()
                        if not any(s.lower() in srv_lower for s in server_whitelist):
                            logger.info(f"FunPay: сервер лота '{lot_server}' не в whitelist — пропускаем {lot_id}")
                            continue

                    logger.info(f"FunPay: лот -> '{title[:50]}' | ${price:.2f} | lvl:{level} class:{bdo_class or '-'}")

                    # Загружаем фото (максимум 2)
                    photo_links = []
                    if skip_photos:
                        logger.info("FunPay: фото пропускаем (skip_photos=True)")
                    elif photo_urls:
                        photo_urls = photo_urls[:2]
                        logger.info(f"FunPay: найдено фото, берём {len(photo_urls)} - загружаем...")
                        local_photos = await self._download_photos(photo_urls, lot_id)
                        for path in local_photos:
                            url = await self._upload_photo(path, game=game)
                            if url:
                                photo_links.append(url)
                        logger.info(f"Фото загружено: {len(photo_links)}")
                    else:
                        logger.info("FunPay: фото не найдены - пропускаем Media")

                    return FunPayLot(
                        price=price,
                        title=title or "Raid Account",
                        description=description or title,
                        href=lot_href,
                        lot_id=lot_id,
                        level=level,
                        m_heroes=m_heroes,
                        l_heroes=l_heroes,
                        photos=photo_links,
                        detailed_description=detailed_description,
                        bdo_class=bdo_class,
                        server=lot_server or card_server,
                        rank=lot_rank,
                        platform=lot_platform,
                        char_class=lot_char_class,
                    )

                except Exception as e:
                    logger.debug(f"Ошибка парсинга карточки: {e}")
                    continue

            logger.warning("FunPay: подходящих лотов не найдено")
            return None

        except Exception as e:
            logger.error(f"FunPay: ошибка: {e}")
            return None
        finally:
            await page.close()