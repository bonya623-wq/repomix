import logging
import re
import asyncio
import json
import os
import aiohttp
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# FIXED: TargetClosedError is NOT a public export in all Playwright versions.
# We detect a closed-page crash by inspecting the exception message instead.
from playwright.async_api import BrowserContext, Page

logger = logging.getLogger("g2g")

BASE = "https://www.g2g.com"
LOT_PAIRS_FILE = "lot_pairs.json"

# G2G API credentials
G2G_API_KEY = "KWYWITTGSXEQUAVEUWYJ68AQPFHIUBQ5"
G2G_API_BASE = "https://sls.g2g.com"  # verify with G2G docs if endpoint differs


# ---------------------------------------------------------------------------
# FIXED: detect "page was closed" without importing TargetClosedError
# ---------------------------------------------------------------------------

def _is_target_closed(e: Exception) -> bool:
    """
    Returns True when Playwright raises because the page/context/browser was closed.
    Works across all Playwright versions regardless of whether TargetClosedError
    is exported from playwright.async_api or not.
    """
    msg = str(e).lower()
    return (
        "target page, context or browser has been closed" in msg
        or "target closed" in msg
        or "browser has been closed" in msg
        or "connection closed" in msg
        or "page closed" in msg
    )


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def clean_description(text: str) -> str:
    """
    Лёгкая очистка для описания — убираем только эмодзи.
    Сохраняем переносы строк, ~ разделители и форматирование.
    """
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"
        "\U00002500-\U00002BEF"
        "\U00010000-\U0010FFFF"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)
    # Убираем только управляющие символы кроме \n и \r
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'\n{4,}', '\n\n\n', text)  # макс 3 пустые строки подряд
    return text.strip()


def clean_text(text: str) -> str:
    """Очищает текст от смайлов, спецсимволов и всего, что не любит G2G."""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"
        "\U00002500-\U00002BEF"
        "\U00010000-\U0010FFFF"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)
    # Дополнительные символы которые не попадают в диапазоны выше
    text = re.sub(r'[\u2000-\u27FF\u2B00-\u2BFF\u3000-\u303F\uFE00-\uFEFF]', '', text)
    text = re.sub(r'[!]+', '!', text)
    # Оставляем только буквы, цифры, пробелы и базовую пунктуацию
    text = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ ,.'\"!?:/|+()\-]", '', text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# lot_pairs.json — atomic save + corruption recovery
# ---------------------------------------------------------------------------

def _try_repair_json(text):
    import re as _re
    # Fix 1: missing comma between }{
    text = _re.sub(r'}(\s*\n\s*){', r'},\1{', text)
    # Fix 2: trailing comma before ] or }
    text = _re.sub(r',\s*\]', ']', text)
    text = _re.sub(r',\s*\}', '}', text)
    # Fix 3: incomplete game array at end: '"Game": [\n}' -> '"Game": []\n}'
    text = _re.sub(r'("[^"]+":\s*\[)\s*\}\s*$', r'\1]\n}', text, flags=_re.DOTALL)
    # Fix 4: missing ] before final } when brackets unbalanced
    stripped = text.rstrip()
    if (stripped.endswith('}') and ']}' not in stripped[-30:]
            and stripped.count('[') > stripped.count(']')):
        last = stripped.rfind('}', 0, len(stripped)-1)
        if last > 0 and stripped[last-1:last] != ']':
            text = stripped[:last] + '\n  ]\n}'
    return text

def load_lot_pairs() -> dict:
    p = Path(LOT_PAIRS_FILE)
    if not p.exists():
        return {}

    raw_bytes = p.read_bytes()

    # Try multiple encodings — Windows often saves with BOM or cp1251
    encodings = ["utf-8-sig", "utf-8", "cp1251", "latin-1"]

    for enc in encodings:
        try:
            text = raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue

        # First try as-is
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        # Try with auto-repair
        try:
            repaired = _try_repair_json(text)
            data = json.loads(repaired)
            if isinstance(data, dict):
                logger.warning("lot_pairs.json: исправлены ошибки JSON (пропущенные запятые/скобки) — сохраняем исправленную версию")
                _save_pairs_atomic(data)
                return data
        except json.JSONDecodeError:
            continue

    # All attempts failed — truly unreadable
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    corrupt_name = f"{LOT_PAIRS_FILE}.corrupt_{stamp}"
    try:
        p.rename(corrupt_name)
        logger.error(f"lot_pairs.json не читается — переименован в {corrupt_name}")
    except Exception as rename_err:
        logger.error(f"Не удалось переименовать повреждённый файл: {rename_err}")
    return {}


def _save_pairs_atomic(pairs: dict):
    """Write to .tmp then atomically replace — safe against mid-write crashes."""
    tmp_path = LOT_PAIRS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(pairs, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, LOT_PAIRS_FILE)


def save_lot_pair(
    funpay_id: str,
    g2g_id: str,
    title: str,
    funpay_url: str,
    game: str = "",
    funpay_price: float = 0.0,
    g2g_price: float = 0.0,
):
    pairs = load_lot_pairs()
    if game not in pairs:
        pairs[game] = []
    for p in pairs[game]:
        if p["funpay_id"] == funpay_id:
            logger.warning(f"Дубль! funpay_id={funpay_id} уже есть в lot_pairs — пропускаем")
            return
        if p["g2g_id"] == g2g_id:
            logger.warning(f"Дубль! g2g_id={g2g_id} уже есть в lot_pairs — пропускаем")
            return
    pairs[game].append({
        "funpay_id":    funpay_id,
        "g2g_id":       g2g_id,
        "title":        title,
        "funpay_url":   funpay_url,
        "funpay_price": round(funpay_price, 2),
        "g2g_price":    round(g2g_price, 2),
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    _save_pairs_atomic(pairs)
    logger.info(
        f"Пара сохранена: FP={funpay_id} G2G={g2g_id} "
        f"fp_price=${funpay_price:.2f} g2g_price=${g2g_price:.2f} game={game}"
    )


def clear_game_pairs(game: str):
    pairs = load_lot_pairs()
    if game in pairs:
        pairs[game] = []
        _save_pairs_atomic(pairs)
        logger.info(f"Очищены пары для игры: {game}")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Lot:
    lot_id: str
    title: str
    price: float


# ---------------------------------------------------------------------------
# G2GBot
# ---------------------------------------------------------------------------

class G2GBot:
    def __init__(self, context: BrowserContext):
        self.context = context
        self._page: Optional[Page] = None

    # -----------------------------------------------------------------------
    # Page management
    # -----------------------------------------------------------------------




    # -----------------------------------------------------------------------
    # Delete lot — page-closed retry + role-based buttons
    # -----------------------------------------------------------------------




    # -----------------------------------------------------------------------
    # Photo upload — postimages only, 5 retries, skip photos that fail
    # G2G supported hosts: 500px, Dropbox, Flickr, Imgur, Postimages
    # -----------------------------------------------------------------------

    # Несколько аккаунтов postimages — ротация если один заблокируют
    _POSTIMAGES_ACCOUNTS = [
        {
            "GUESTKEY":   "muidn p85O9xYA3li4",
            "SESSIONKEY": "a99ef415574e36289d27e44853c09e5927a5d4ce077da68ca8cb4de8457c2f09",
        },
        {
            "GUESTKEY":   "b8b4536a217e633c15daee1eab905b76",
            "SESSIONKEY": "75a7129abda4d92d810c95e286adc11be98b0d67a08c301eaea5d30a82f85c9b",
        },
    ]

    # Imgur аккаунт — fallback если postimages не работает
    _IMGUR_COOKIES = {
        "is_authed":       "1",
        "user_id":         "196164003",
        "IMGURUIDJAFO":    "c5b8fd27459e28f4bafa32e171597834b06f67b3d0a851ee36777280eb64bd72",
        "postpagebeta":    "1",
        "frontpagebetav2": "1",
        "m_section":       "hot",
        "m_sort":          "time",
    }
    _IMGUR_CLIENT_ID = "546c25a59c58ad7"  # public client id для anonymous upload
    _POSTIMAGES_ACCOUNT_IDX = 0  # текущий аккаунт

    @property
    def _POSTIMAGES_COOKIES(self):
        idx = self._POSTIMAGES_ACCOUNT_IDX % len(self._POSTIMAGES_ACCOUNTS)
        return self._POSTIMAGES_ACCOUNTS[idx]

    def _rotate_postimages_account(self):
        self._POSTIMAGES_ACCOUNT_IDX = (self._POSTIMAGES_ACCOUNT_IDX + 1) % len(self._POSTIMAGES_ACCOUNTS)
        logger.info(f"postimages: переключились на аккаунт #{self._POSTIMAGES_ACCOUNT_IDX}")



    async def _upload_one_to_imgur(
        self,
        session: aiohttp.ClientSession,
        image_url: str,
    ) -> Optional[str]:
        """Upload image to Imgur via API. Returns direct link or None."""
        try:
            headers = {
                "Authorization": f"Client-ID {self._IMGUR_CLIENT_ID}",
                "User-Agent": "Mozilla/5.0",
            }
            form = aiohttp.FormData()
            form.add_field("image", image_url)
            form.add_field("type", "url")

            async with session.post(
                "https://api.imgur.com/3/image",
                data=form,
                headers=headers,
                cookies=self._IMGUR_COOKIES,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if data.get("success") and data.get("data", {}).get("link"):
                    link = data["data"]["link"]
                    logger.info(f"Imgur: OK -> {link}")
                    return link
            return None
        except Exception as e:
            logger.warning(f"Imgur: ошибка ({e})")
            return None

    # -----------------------------------------------------------------------
    # Navigation — fixed sleeps replaced with explicit waits
    # -----------------------------------------------------------------------


    # -----------------------------------------------------------------------
    # Region selector (WOW Classic and other games that need it)
    # -----------------------------------------------------------------------




    async def _fill_wow_fields(self, page, wow_params: dict):
        server_g2g = wow_params.get("server_g2g", "")
        wow_class  = wow_params.get("wow_class", "")
        race       = wow_params.get("race", "")
        level      = wow_params.get("level", "")
        faction    = wow_params.get("faction", "")
        country    = wow_params.get("country", "Ukraine")

        logger.info(
            f"G2G WOW fields: server={server_g2g!r} class={wow_class!r} "
            f"race={race!r} level={level!r}"
        )

        async def _wait_no_loading():
            # Wait for q-inner-loading spinner to disappear before clicking
            try:
                await page.wait_for_selector(
                    ".q-inner-loading", state="hidden", timeout=10000
                )
            except Exception:
                pass
            await asyncio.sleep(0.3)

        async def _open_next_please_select():
            await _wait_no_loading()
            btns = await page.query_selector_all("button.g-btn-select")
            for btn in btns:
                if "please select" in (await btn.inner_text()).strip().lower():
                    await btn.click()
                    await asyncio.sleep(1.5)  # пауза после открытия дропдауна
                    return True
            return False

        async def _pick_item(search_term, faction_hint=""):
            if not search_term:
                return ""
            # Ждём появления дропдауна — список должен появиться
            try:
                await page.wait_for_selector(
                    ".q-virtual-scroll__content .q-item, .q-item--dense",
                    timeout=8000
                )
            except Exception:
                pass
            await asyncio.sleep(0.3)

            filt = await page.query_selector("input[placeholder='Type to filter']")
            if filt:
                await filt.click()
                await filt.fill(search_term)
                # Ждём пока список отфильтруется
                await asyncio.sleep(1.0)

            items = await page.query_selector_all(
                ".q-virtual-scroll__content .q-item, .q-item--dense"
            )
            t_lower = search_term.lower()
            h_lower = faction_hint.lower()
            for item in items:
                t = (await item.inner_text()).strip()
                if t.lower() == t_lower:
                    await item.click()
                    return t
            for item in items:
                t = (await item.inner_text()).strip().lower()
                if t_lower in t and (not h_lower or h_lower in t):
                    await item.click()
                    return t
            for item in items:
                t = (await item.inner_text()).strip().lower()
                if t_lower in t:
                    await item.click()
                    return t
            return ""

        # Server
        if server_g2g and "[EU" in server_g2g:
            server_name  = server_g2g.split("[")[0].strip()
            faction_hint = "" if faction.lower() == "any" else faction
            if await _open_next_please_select():
                chosen = await _pick_item(server_name, faction_hint)
                if chosen:
                    logger.info(f"G2G WOW Server: '{chosen}'")
                else:
                    logger.warning(f"G2G WOW Server not found: '{server_g2g}' — пропускаем лот")
                    return False  # сигнал пропустить лот
            await asyncio.sleep(2.0)

        # Class
        if wow_class:
            if await _open_next_please_select():
                chosen = await _pick_item(wow_class)
                logger.info(f"G2G WOW Class: '{chosen}'")
            await asyncio.sleep(2.0)

        # Race
        if race:
            if await _open_next_please_select():
                chosen = await _pick_item(race)
                logger.info(f"G2G WOW Race: '{chosen}'")
            await asyncio.sleep(2.0)

        # Level
        if level:
            if await _open_next_please_select():
                chosen = await _pick_item(level)
                logger.info(f"G2G WOW Level: '{chosen}'")
            await asyncio.sleep(2.0)

        # Country
        if country:
            if await _open_next_please_select():
                chosen = await _pick_item(country)
                logger.info(f"G2G WOW Country: '{chosen}'")
            await asyncio.sleep(0.5)

    async def _get_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = await self.context.new_page()
        return self._page

    async def is_logged_in(self) -> bool:
        page = await self._get_page()
        token = await page.evaluate("() => localStorage.getItem('accessToken')")
        return token is not None

    async def wait_for_manual_login(self):
        page = await self._get_page()
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=40000)
        logger.info("G2G: залогинься вручную в браузере...")
        await page.wait_for_url(
            lambda url: "login" not in url and "g2g.com" in url,
            timeout=300000,
        )
        await asyncio.sleep(2)
        logger.info(f"G2G: авторизация успешна! URL: {page.url}")

    # -----------------------------------------------------------------------
    # Delete lot — page-closed retry + role-based buttons
    # -----------------------------------------------------------------------

    async def delete_lot(self, g2g_id: str) -> bool:
        """Retry once if the page was closed unexpectedly."""
        for attempt in range(2):
            try:
                return await self._delete_lot_inner(g2g_id)
            except Exception as e:
                if _is_target_closed(e):
                    logger.warning(
                        f"G2G delete: страница закрыта (попытка {attempt + 1}/2) — пересоздаём..."
                    )
                    self._page = None
                    if attempt == 1:
                        logger.error("G2G delete: страница закрылась дважды — сдаёмся")
                        return False
                else:
                    logger.error(f"G2G delete: неожиданная ошибка: {e}")
                    return False
        return False

    async def _delete_lot_inner(self, g2g_id: str) -> bool:
        """Удаление лота — логика из оригинального рабочего кода."""
        page = await self._get_page()
        try:
            logger.info(f"G2G: удаляем лот {g2g_id}...")

            await page.goto(
                f"{BASE}/offers/list?cat_id=5830014a-b974-45c6-9672-b51e83112fb7&status=live",
                wait_until="domcontentloaded",
                timeout=40000,
            )
            await asyncio.sleep(3)

            await page.wait_for_selector(
                "input[placeholder='Search title or offer number']",
                timeout=15000,
            )
            search_input = await page.query_selector(
                "input[placeholder='Search title or offer number']"
            )
            if search_input:
                await search_input.fill(g2g_id)
                logger.info(f"G2G: вводим ID в поиск -> {g2g_id}")
                await asyncio.sleep(3)  # ждём загрузки результатов поиска

            # Кнопка три точки — класс g-btn-round, иконка more_vert
            # Пробуем по классу, затем по иконке, затем последняя кнопка в строке
            more_btn = (
                await page.query_selector("tbody tr button.g-btn-round") or
                await page.query_selector("tbody tr button:has(.material-icons)")
            )
            if not more_btn:
                # Fallback: последняя кнопка в первой строке
                btns = await page.query_selector_all("tbody tr button")
                more_btn = btns[-1] if btns else None

            if more_btn:
                await more_btn.click()
                logger.info("G2G: three-dots нажат")
                await asyncio.sleep(2)  # ждём появления меню
            else:
                logger.warning("G2G: кнопка three-dots не найдена — лот уже удалён с G2G")
                return True  # уже удалён — убираем из базы

            # Ищем Remove по всем .q-item на странице
            remove_btn = None
            for item in await page.query_selector_all(".q-item"):
                if (await item.inner_text()).strip() == "Remove":
                    remove_btn = item
                    break

            if not remove_btn:
                logger.warning("G2G: кнопка Remove не найдена — лот уже удалён с G2G")
                return True  # уже удалён — убираем из базы

            await remove_btn.click()
            logger.info("G2G: Remove нажат")
            await asyncio.sleep(2)  # ждём диалог подтверждения

            # Ищем Confirm среди всех кнопок
            for btn in await page.query_selector_all("button"):
                if (await btn.inner_text()).strip() == "Confirm":
                    await btn.click()
                    await asyncio.sleep(3)
                    logger.info(f"G2G: лот {g2g_id} удалён OK")
                    return True

            logger.warning("G2G: кнопка Confirm не найдена")
            return False

        except Exception as e:
            logger.error(f"G2G: ошибка удаления лота {g2g_id}: {e}")
            return False


    # -----------------------------------------------------------------------
    # Photo upload — postimages only, 5 retries, skip photos that fail
    # G2G supported hosts: 500px, Dropbox, Flickr, Imgur, Postimages
    # -----------------------------------------------------------------------

    _POSTIMAGES_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://postimages.org/web",
        "Origin":  "https://postimages.org",
    }

    # ── Per-game PostImages gallery mapping ──────────────────────────────────
    # Ключ — подстрока имени игры (lowercase)
    # Значение — hex-идентификатор галереи на PostImages
    #
    # КАК ПОЛУЧИТЬ HEX:
    #   1. Зайди на postimages.org под своим аккаунтом
    #   2. Открой нужную галерею (raid / wow)
    #   3. В URL будет: postimages.org/user/gallery/ВОТ_ЭТО_И_ЕСТЬ_HEX
    #   4. Вставь hex сюда вместо "ВСТАВЬ_HEX_ГАЛЕРЕИ_RAID" / "ВСТАВЬ_HEX_ГАЛЕРЕИ_WOW"
    _GAME_GALLERY_HEX_MAP: dict = {
        "raid":    "0P1JnB9",
        "wow":     "4GzgRHG",
        "zenless": "qnThCC8",
        "eve":     "ZSGfj5P",
    }

    def _get_gallery_hex_for_game(self, game: str):
        """Вернуть hex галереи PostImages для данной игры или None."""
        game_lower = game.lower()
        for key, hex_val in self._GAME_GALLERY_HEX_MAP.items():
            if key in game_lower:
                # Если hex не заполнен — возвращаем None чтобы грузить без галереи
                if hex_val.startswith("ВСТАВЬ"):
                    logger.warning(
                        f"postimages: hex галереи для '{key}' не задан — "
                        f"загрузка будет без галереи. "
                        f"Открой postimages.org/user/gallery и скопируй hex из URL."
                    )
                    return None
                return hex_val
        return None

    async def _fetch_gallery_token(self, session, gallery_hex):
        """
        Получить upload-токен для галереи через GET /json/rr/{hex}.
        Postimages возвращает JSON с полем "token" для этой галереи.
        Этот токен затем передаётся в POST /json/rr/{hex} при загрузке.
        """
        try:
            async with session.get(
                f"https://postimages.org/json/rr/{gallery_hex}",
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                text = await resp.text()

            # Postimages возвращает: {"status":"OK","token":"...","..."}
            try:
                data = json.loads(text)
                token = data.get("token") or data.get("data", {}).get("token")
                if token:
                    logger.info(f"postimages: gallery token получен hex={gallery_hex}")
                    return token
            except Exception:
                pass

            # Fallback: regex если не JSON
            m = re.search(r'"token"\s*:\s*"([^"]+)"', text)
            if m:
                logger.info(f"postimages: gallery token (regex) hex={gallery_hex}")
                return m.group(1)

            logger.warning(f"postimages: токен не найден для hex={gallery_hex}, ответ: {text[:200]}")
            return None
        except Exception as e:
            logger.warning(f"postimages: ошибка получения токена галереи ({e})")
            return None

    async def _fetch_postimages_token(self, session) -> Optional[str]:
        """GET postimages page to grab CSRF token if required."""
        try:
            async with session.get(
                "https://postimages.org/web",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                html = await resp.text()
                m = re.search(
                    r"""<input[^>]+name=["\'](?:token|_token|csrf)["\'][^>]+value=["\']([^"\']+)["\']""",
                    html,
                    re.IGNORECASE,
                )
                return m.group(1) if m else None
        except Exception as e:
            logger.warning(f"postimages: не удалось получить токен ({e})")
            return None

    async def _upload_one_to_postimages(
        self,
        session,
        image_url: str,
        token=None,
        gallery_hex=None,
    ) -> Optional[str]:
        """
        Скачиваем фото как байты и загружаем в postimages как файл (multipart).
        Загрузка по URL (url=...) не работает для галерей — postimages игнорирует gallery.
        Загрузка файлом работает стабильно и помещает фото в нужную галерею.
        """
        try:
            # Шаг 1: скачиваем оригинальное фото
            async with session.get(
                image_url,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as img_resp:
                if img_resp.status != 200:
                    logger.warning(f"postimages: не удалось скачать фото ({img_resp.status}): {image_url[:80]}")
                    return None
                img_bytes = await img_resp.read()
                content_type = img_resp.headers.get("Content-Type", "image/jpeg")

            # Определяем расширение из URL или Content-Type
            ext = "jpg"
            for e in ("png", "gif", "webp", "jpeg", "jpg"):
                if e in image_url.lower() or e in content_type.lower():
                    ext = "jpeg" if e == "jpeg" else e
                    break
            filename = f"upload.{ext}"

            # Шаг 2: загружаем как файл в postimages
            endpoint = (
                f"https://postimages.org/json/rr/{gallery_hex}"
                if gallery_hex else
                "https://postimages.org/web"
            )

            form = aiohttp.FormData()
            form.add_field(
                "file",
                img_bytes,
                filename=filename,
                content_type=content_type,
            )
            form.add_field("resize", "0")
            form.add_field("expire", "0")
            if token:
                form.add_field("token", token)

            upload_headers = {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://postimages.org/{'user/gallery/' + gallery_hex if gallery_hex else 'web'}",
            }

            async with session.post(
                endpoint,
                data=form,
                headers=upload_headers,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                html = await resp.text()

            # Парсим ссылку из ответа
            patterns = [
                r'"url"\s*:\s*"(https?://i\.postimg\.cc/[^"]+)"',
                r'(https?://i\.postimg\.cc/[^\s"\'<>]+)',
                r'value="(https?://[^"]*postimg[^"]*\.(?:jpg|jpeg|png|gif|webp))"',
            ]
            for pat in patterns:
                m = re.search(pat, html, re.IGNORECASE)
                if m:
                    link = m.group(1)
                    logger.info(f"postimages: загружено -> {link}")
                    return link

            logger.warning(f"postimages: ссылка не найдена в ответе: {html[:300]}")
            return None
        except Exception as e:
            logger.warning(f"postimages: ошибка одной попытки ({e})")
            return None

    # Domains that G2G whitelists — photos already on these hosts need no re-upload
    _G2G_ACCEPTED_HOSTS = (
        "postimg.cc",
        "postimages.org",
        "imgur.com",
        "i.imgur.com",
        "500px.com",
        "dropbox.com",
        "flickr.com",
        "staticflickr.com",
    )

    def _is_already_accepted(self, url: str) -> bool:
        """Return True if the URL is already hosted on a G2G-whitelisted domain."""
        url_lower = url.lower()
        return any(host in url_lower for host in self._G2G_ACCEPTED_HOSTS)

    async def upload_photos_to_postimages(self, photos: list, game: str = "") -> list:
        """
        Prepare photos for G2G:
        - Если передан game — фото загружаются в соответствующую галерею PostImages
          (raid → галерея "raid", wow → галерея "wow").
        - Если URL уже на принятом хосте — пропускаем загрузку.
        - Иначе — до 5 попыток, потом ротация аккаунта, потом Imgur как fallback.

        Returns only valid, G2G-accepted URLs.
        """
        if not photos:
            return []

        # Separate already-accepted URLs from ones that need uploading
        to_upload = [u for u in photos if not self._is_already_accepted(u)]
        accepted_count = len(photos) - len(to_upload)

        if accepted_count:
            logger.info(
                f"фото уже на принятом хосте: {accepted_count}/{len(photos)} — загрузка не нужна"
            )
        if not to_upload:
            return list(photos)  # all already accepted

        logger.info(f"postimages: нужно загрузить {len(to_upload)}/{len(photos)} фото...")

        async with aiohttp.ClientSession(
            headers=self._POSTIMAGES_HEADERS,
            cookies=self._POSTIMAGES_COOKIES,
        ) as session:
            # ── Определяем галерею для этой игры (по захардкоженному hex) ──────
            gallery_hex = None
            if game:
                gallery_hex = self._get_gallery_hex_for_game(game)
                if gallery_hex:
                    # Для галереи берём токен через gallery endpoint
                    token = await self._fetch_gallery_token(session, gallery_hex)
                    logger.info(f"postimages: используем галерею hex={gallery_hex} для игры '{game}'")
                else:
                    # hex не задан или игра не в маппинге — грузим без галереи
                    logger.warning(f"postimages: галерея для '{game}' не задана — грузим без галереи")
                    token = await self._fetch_postimages_token(session)
            else:
                token = await self._fetch_postimages_token(session)

            if token:
                logger.info(f"postimages: токен получен ({token[:20]}...)")
            else:
                logger.info("postimages: токен не требуется")

            async def _upload_with_retry(original_url: str) -> Optional[str]:
                delays = [2, 4, 8, 16, 32]
                for attempt, delay in enumerate(delays, 1):
                    logger.info(f"postimages: попытка {attempt}/5 — {original_url[:80]}")
                    result = await self._upload_one_to_postimages(
                        session, original_url, token, gallery_hex
                    )
                    if result:
                        logger.info(f"postimages: OK -> {result}")
                        return result
                    if attempt < len(delays):
                        logger.warning(
                            f"postimages: попытка {attempt}/5 неудача — ждём {delay}с..."
                        )
                        await asyncio.sleep(delay)
                # Все попытки провалились — пробуем следующий аккаунт
                if len(self._POSTIMAGES_ACCOUNTS) > 1:
                    self._rotate_postimages_account()
                    logger.warning("postimages: ротируем аккаунт и пробуем ещё раз...")
                    result = await self._upload_one_to_postimages(
                        session, original_url, token, gallery_hex
                    )
                    if result:
                        logger.info(f"postimages: OK после ротации -> {result}")
                        return result
                # Postimages не сработал — пробуем Imgur как fallback
                logger.warning("postimages: все попытки исчерпаны — пробуем Imgur...")
                imgur_result = await self._upload_one_to_imgur(session, original_url)
                if imgur_result:
                    return imgur_result
                logger.error(
                    f"postimages+imgur: фото не загружено — пропускаем: {original_url[:80]}"
                )
                return None

            tasks = [_upload_with_retry(url) for url in to_upload]
            upload_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge results back in original order, skipping failed uploads
        upload_iter = iter(upload_results)
        final = []
        for url in photos:
            if self._is_already_accepted(url):
                final.append(url)
            else:
                res = next(upload_iter)
                if isinstance(res, Exception) or not res:
                    logger.warning(f"postimages: фото пропущено: {url[:60]}")
                else:
                    final.append(res)

        logger.info(f"postimages: итого готово {len(final)}/{len(photos)} фото")
        return final

    # -----------------------------------------------------------------------
    # Navigation — fixed sleeps replaced with explicit waits
    # -----------------------------------------------------------------------

    async def _navigate_to_form(self, page: Page, game_name: str = "Raid: Shadow Legends", region: str = "", server_g2g: str = ""):
        """
        Navigate to the lot-creation form.
        Every step waits explicitly for the UI to respond — no fixed timeouts
        that race against slow page renders.
        """
        logger.info("G2G: открываем /offers/sell...")

        # Wait for full network quiet so the SPA has time to hydrate
        await page.goto(f"{BASE}/offers/sell", wait_until="networkidle", timeout=60000)
        logger.info("G2G: /offers/sell загружен (networkidle)")

        # --- Step 1: click "Accounts" tab ---
        # The tab may not be visible immediately after hydration, so we keep retrying
        for attempt in range(5):
            try:
                await page.wait_for_selector("text=Accounts", timeout=10000)
                await page.click("text=Accounts")
                logger.info("G2G: вкладка Accounts нажата")
                break
            except Exception:
                logger.warning(f"G2G: вкладка Accounts не найдена, попытка {attempt + 1}/5")
                await asyncio.sleep(2)
        else:
            raise Exception("G2G: вкладка Accounts не появилась после 5 попыток")

        # --- Step 2: wait for the game-selector dropdown button ---
        # This button renders AFTER "Accounts" triggers a panel load — give it plenty of time
        logger.info("G2G: ждём кнопку выбора игры (button.g-btn-select)...")
        await page.wait_for_selector("button.g-btn-select", timeout=30000)
        await page.click("button.g-btn-select")
        logger.info("G2G: кнопка выбора игры нажата")

        # --- Step 3: type game name in the filter ---
        search = await page.wait_for_selector("input[placeholder='Type to filter']", timeout=15000)
        await search.click()
        await search.type(game_name, delay=80)
        logger.info(f"G2G: набрали название игры '{game_name}'")

        # --- Step 4: wait for results and click the matching item ---
        # Сначала ждём появления любых элементов списка
        try:
            await page.wait_for_selector("div[class*='item']", timeout=15000)
        except Exception:
            logger.warning("G2G: список игр не появился за 15с")

        # Ищем нужную игру — крутимся пока не найдём (до 60 секунд)
        selected = False
        deadline = asyncio.get_event_loop().time() + 60

        while not selected:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break

            items = await page.query_selector_all("div[class*='item']")
            texts = [(item, (await item.inner_text()).strip()) for item in items]

            # Pass 1: exact match (case-insensitive)
            for item, txt in texts:
                if txt.lower() == game_name.lower():
                    await item.click()
                    logger.info(f"G2G: выбрано '{txt}' (точное)")
                    selected = True
                    break

            # Pass 2: partial match — only if exact not found
            if not selected:
                for item, txt in texts:
                    if game_name.lower() in txt.lower():
                        await item.click()
                        logger.info(f"G2G: выбрано '{txt}' (частичное)")
                        selected = True
                        break

            if not selected:
                logger.info(f"G2G: ждём '{game_name}' в списке... ({remaining:.0f}с)")
                await asyncio.sleep(2)

        if not selected:
            raise Exception(f"G2G: не удалось выбрать игру '{game_name}' за 60с")

        # --- Step 5: select region if required (e.g. WOW Classic) ---
        # After game selection some games show extra dropdowns (region, server, faction)
        # We handle region here; pass region="" to skip
        if region:
            await self._select_region(page, region)

        # Сервер выбирается в _fill_wow_fields, не здесь


        # --- Step 6: click Continue and wait for the form ---
        await asyncio.sleep(0.3)
        await page.click("text=Continue")

        # Wait for loading spinner to disappear after Continue
        try:
            await page.wait_for_selector(".q-inner-loading", timeout=5000)
            await page.wait_for_selector(".q-inner-loading", state="hidden", timeout=30000)
            logger.info("G2G: спиннер загрузки исчез")
        except Exception:
            pass

        logger.info("G2G: ждём загрузки формы (Offer title)...")
        await page.wait_for_selector("input[placeholder='Offer title']", timeout=30000)
        logger.info(f"G2G: форма открыта | URL: {page.url}")

    # -----------------------------------------------------------------------
    # Region selector (WOW Classic and other games that need it)
    # -----------------------------------------------------------------------

    async def _select_region(self, page: Page, region: str):
        """
        Click the 'Select region' dropdown and pick the given region (e.g. 'EU').
        Button HTML: <button ...><div class="text-font-2nd ...">Select region</div></button>
        """
        logger.info(f"G2G: выбираем регион '{region}'...")

        # Find the region button by its inner text
        region_btn = None
        for attempt in range(5):
            btns = await page.query_selector_all("button.g-btn-select")
            for btn in btns:
                txt = (await btn.inner_text()).strip()
                if "Select region" in txt or "region" in txt.lower():
                    region_btn = btn
                    break
            if region_btn:
                break
            logger.warning(f"G2G: кнопка Select region не найдена, попытка {attempt + 1}/5")
            await asyncio.sleep(1.5)

        if not region_btn:
            logger.warning("G2G: кнопка Select region не найдена — пропускаем")
            return

        await region_btn.click()
        logger.info("G2G: Select region нажата")

        # Wait for dropdown items to appear
        await page.wait_for_selector("div[class*='item']", timeout=10000)
        await asyncio.sleep(0.5)

        # Click the matching region
        selected = False
        for attempt in range(3):
            items = await page.query_selector_all("div[class*='item'], .q-item")
            for item in items:
                txt = (await item.inner_text()).strip()
                if txt.strip().upper() == region.strip().upper() or region.upper() in txt.upper():
                    await item.click()
                    logger.info(f"G2G: регион выбран → '{txt}'")
                    selected = True
                    break
            if selected:
                break
            logger.warning(f"G2G: регион '{region}' не найден, попытка {attempt + 1}/3")
            await asyncio.sleep(1)

        if not selected:
            logger.warning(f"G2G: не удалось выбрать регион '{region}'")

    async def _select_server(self, page: Page, server_g2g: str):
        """
        Выбирает сервер в дропдауне G2G.
        server_g2g — строка вида 'Firemaw [EU] - Alliance'

        Алгоритм:
        1. Кликаем кнопку "Please select"
        2. Вводим имя сервера в "Type to filter"
        3. Ждём и кликаем совпадающий элемент
        """
        if not server_g2g:
            logger.info("G2G: сервер не указан — пропускаем")
            return

        # Пропускаем не-EU серверы (FR, DE и т.д.)
        if "[EU" not in server_g2g:
            logger.info(f"G2G: сервер '{server_g2g}' не EU — пропускаем")
            return

        logger.info(f"G2G: выбираем сервер '{server_g2g}'...")

        server_name = server_g2g.split("[")[0].strip()   # "Firemaw"
        faction     = server_g2g.split("- ")[-1].strip() if "- " in server_g2g else ""

        # ── Шаг 1: кликаем кнопку "Please select" ────────────────────────
        server_btn = None
        for attempt in range(5):
            btns = await page.query_selector_all("button.g-btn-select")
            for btn in btns:
                if "please select" in (await btn.inner_text()).strip().lower():
                    server_btn = btn
                    break
            if server_btn:
                break
            await asyncio.sleep(1.0)

        if not server_btn:
            logger.warning("G2G: кнопка 'Please select' (сервер) не найдена")
            return

        await server_btn.click()
        logger.info("G2G: дропдаун сервера открыт")
        await asyncio.sleep(1.0)

        # ── Шаг 2: вводим в "Type to filter" ─────────────────────────────
        filter_input = await page.query_selector("input[placeholder='Type to filter']")
        if filter_input:
            await filter_input.click()
            await filter_input.fill(server_name)
            logger.info(f"G2G: фильтр сервера → '{server_name}'")
            await asyncio.sleep(1.0)
        else:
            logger.warning("G2G: поле 'Type to filter' не найдено")

        # ── Шаг 3: кликаем нужный элемент ────────────────────────────────
        selected = await page.evaluate("""
            (args) => {
                const {server_g2g, server_name, faction} = args;
                const items = document.querySelectorAll(
                    '.q-virtual-scroll__content .q-item, .q-item--dense'
                );
                for (const item of items) {
                    const txt = (item.innerText || '').trim();
                    if (txt === server_g2g) {
                        item.click(); return txt;
                    }
                    if (txt.toLowerCase().includes(server_name.toLowerCase()) &&
                        (!faction || txt.toLowerCase().includes(faction.toLowerCase()))) {
                        item.click(); return txt;
                    }
                }
                return null;
            }
        """, {"server_g2g": server_g2g, "server_name": server_name, "faction": faction})

        if selected:
            logger.info(f"G2G: сервер выбран → '{selected}'")
        else:
            logger.warning(f"G2G: сервер '{server_g2g}' не найден в списке")


    def _extract_id_from_row(self, row_text: str) -> Optional[str]:
        """Pull the first G2G offer ID (#Gxxx... or Gxxx...) out of a table row's text."""
        m = re.search(r"#?(G[A-Z0-9]+)", row_text)
        return m.group(1) if m else None

    # -----------------------------------------------------------------------
    # ID-extraction helpers — надёжные методы вместо поиска по title
    # -----------------------------------------------------------------------

    @staticmethod
    def _extract_g2g_id_from_url(url: str) -> Optional[str]:
        """
        Извлечь G2G offer ID из URL страницы.
        G2G часто ставит оффер ID прямо в URL после публикации:
          /offers/G1778202076076TT/edit  или  /offers/G1778202076076TT
        """
        m = re.search(r"/(G[A-Z0-9]{10,})(?:/|$|\?)", url)
        return m.group(1) if m else None

    async def _extract_g2g_id_from_page_content(
        self,
        page: Page,
        known_ids: set,
    ) -> Optional[str]:
        """
        Сканировать текстовое содержимое текущей страницы на паттерн G[A-Z0-9]{10,}.
        Возвращает первый найденный ID которого ещё нет в known_ids.
        Работает на любой странице G2G — success-message, edit, list и т.д.
        """
        try:
            ids: list = await page.evaluate("""
                () => {
                    const text = document.body.innerText || '';
                    const matches = text.match(/G[A-Z0-9]{10,}/g) || [];
                    return [...new Set(matches)];
                }
            """)
            for gid in ids:
                if gid not in known_ids:
                    logger.info(f"G2G: ID найден в тексте страницы -> {gid}")
                    return gid
        except Exception as e:
            logger.warning(f"G2G: ошибка сканирования текста страницы: {e}")
        return None

    @staticmethod
    def _title_similarity(a: str, b: str) -> float:
        """Доля общих слов между двумя строками. От 0.0 до 1.0."""
        a_words = set(re.findall(r"\w+", a.lower()))
        b_words = set(re.findall(r"\w+", b.lower()))
        if not a_words or not b_words:
            return 0.0
        return len(a_words & b_words) / max(len(a_words), len(b_words))

    # JS: читаем все строки таблицы → список {id, title, rowText}
    # rowText — весь текст строки без ID, используется как fallback для similarity
    # когда title не вычитывается из специфичных селекторов
    _JS_ROWS_ID_TITLE = """
        () => {
            // Aggressive whitespace cleanup — kills NBSP / ZWSP / soft hyphen / BOM
            const NORM = s => (s || '')
                .replace(/[\\u00A0\\u200B-\\u200D\\u2060\\uFEFF\\u00AD]/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();

            const rows = document.querySelectorAll('tbody tr');
            const result = [];
            for (const row of rows) {
                const idEl = row.querySelector('.text-font-2nd.text-body2');
                if (!idEl) continue;
                const rawId = NORM(idEl.innerText).replace('#', '');
                if (!/^G[A-Z0-9]{10,}/.test(rawId)) continue;

                // Try specific G2G title selectors first
                let title = '';
                const titleEl = (
                    row.querySelector('.text-subtitle1') ||
                    row.querySelector('[class*=\"offer-title\"]') ||
                    row.querySelector('td:first-child .text-body1') ||
                    row.querySelector('td:first-child span')
                );
                if (titleEl) title = NORM(titleEl.innerText);

                // Full row text without IDs — used as fallback for similarity
                const fullRowText = NORM(
                    (row.innerText || '')
                        .replace(new RegExp('#?' + rawId, 'g'), '')
                );

                // If title still empty, take the longest text fragment from row
                if (!title) {
                    const parts = (row.innerText || '')
                        .split(/[\\n\\t]/)
                        .map(NORM)
                        .filter(s => s.length > 5);
                    title = parts.reduce((a, b) => a.length >= b.length ? a : b, '');
                }

                result.push({id: rawId, title: title, rowText: fullRowText});
            }
            return result;
        }
    """

    async def _verify_id_in_manage(
        self,
        page: Page,
        g2g_id: str,
        title: str,
        wait_seconds: int = 20,
        trust_when_title_missing: bool = False,
    ) -> bool:
        """
        Вбить конкретный g2g_id в строку поиска Manage, дождаться строки
        и проверить что её title совпадает с нашим (similarity >= 0.4).

        trust_when_title_missing=True — если title не вычитался ИЛИ similarity 0,
        но строка единственная и ID получен из надёжного источника
        (URL после publish, href кнопки Manage offer) — доверяем.
        Это нужно потому что G2G иногда не отдаёт title в DOM сразу
        после публикации, а ID точный и пришёл от самого G2G.

        Возвращает True только при подтверждении.
        """
        try:
            search_input = await page.query_selector(
                "input[placeholder='Search title or offer number']"
            )
            if not search_input:
                return False

            await search_input.click()
            await page.keyboard.press("Control+a")
            await search_input.fill(g2g_id)
            logger.info(
                f"G2G verify: вбиваем ID={g2g_id} в поиск Manage "
                f"(trust_when_title_missing={trust_when_title_missing})..."
            )

            for i in range(max(1, wait_seconds // 3)):
                await asyncio.sleep(3)
                rows: list = await page.evaluate(self._JS_ROWS_ID_TITLE)
                # Берём только строку с этим самым ID
                matching = [r for r in rows if r["id"] == g2g_id]
                if not matching:
                    logger.info(
                        f"G2G verify: ID={g2g_id} ещё не появился "
                        f"({i+1}/{max(1, wait_seconds//3)})..."
                    )
                    continue

                row = matching[0]
                row_title = (row.get("title") or "").strip()
                row_text  = (row.get("rowText") or "").strip()

                # Считаем similarity по title, а если title пуст — по полному rowText
                sim_title = self._title_similarity(title, row_title) if row_title else 0.0
                sim_row   = self._title_similarity(title, row_text)  if row_text  else 0.0
                sim = max(sim_title, sim_row)

                logger.info(
                    f"G2G verify: ID найден, sim={sim:.2f} "
                    f"(title_sim={sim_title:.2f}, rowText_sim={sim_row:.2f}) "
                    f"| наш: '{title[:50]}' "
                    f"| title: '{row_title[:50]}' "
                    f"| rowText: '{row_text[:80]}'"
                )

                if sim >= 0.4:
                    logger.info(f"G2G verify: ✓ ID={g2g_id} подтверждён (sim={sim:.2f})")
                    return True

                # Title и rowText ничего не дали. Доверяем только если ID
                # пришёл из надёжного источника И в выдаче по точному ID
                # ровно одна строка (а это уже гарантировано matching=[row]).
                if trust_when_title_missing and not row_title and not row_text:
                    logger.info(
                        f"G2G verify: ✓ ID={g2g_id} найден, title и rowText пусты, "
                        f"но ID из надёжного источника — доверяем"
                    )
                    return True

                logger.warning(
                    f"G2G verify: ✗ ID={g2g_id} найден, но title не совпал "
                    f"(sim={sim:.2f}) — не берём!"
                )
                return False

            logger.warning(f"G2G verify: ID={g2g_id} не появился в Manage за {wait_seconds}с")
            return False
        except Exception as e:
            logger.error(f"G2G verify: ошибка: {e}")
            return False

    async def _find_lot_by_title_in_manage(
        self,
        page: Page,
        title: str,
        known_ids: set,
        wait_seconds: int = 30,
    ) -> Optional[str]:
        """
        МЕТОД 2 — поиск по title в Manage с прогрессивным укорачиванием запроса.

        Алгоритм:
          1. Берём первые 40 символов title как начальный запрос.
          2. Ищем совпадение (2 попытки на каждый запрос).
          3. Если ничего не нашли — удаляем последнее слово из запроса.
          4. Повторяем до тех пор пока запрос не станет короче 3 символов.

        Для каждой строки берём max(title_sim, rowText_sim) — устойчиво
        к багам разметки G2G когда title не читается из DOM.

        Если ровно одна новая строка и title+rowText оба пустые — доверяем
        поисковому фильтру G2G (он сам отфильтровал по нашему запросу).
        """
        # Строим список запросов: от полного к однословному
        # Начинаем с первых 40 символов, затем каждый раз обрезаем последнее слово
        base = title[:40].strip()
        if not base:
            return None

        queries: list = []
        current = base
        while len(current) >= 3:
            queries.append(current)
            # Удаляем последнее слово
            parts = current.rsplit(None, 1)   # rsplit по пробелу, 1 раз справа
            if len(parts) < 2:
                break                          # осталось одно слово — дальше некуда
            current = parts[0].rstrip(" ,|:-")  # убираем висящую пунктуацию

        if not queries:
            return None

        try:
            search_input = await page.query_selector(
                "input[placeholder='Search title or offer number']"
            )
            if not search_input:
                logger.warning("G2G title-search: поле поиска не найдено")
                return None

            for q_idx, search_query in enumerate(queries):
                logger.info(
                    f"G2G title-search: запрос {q_idx + 1}/{len(queries)} -> '{search_query}'"
                )
                await search_input.click()
                await page.keyboard.press("Control+a")
                await search_input.fill(search_query)

                # 5 попыток на каждый запрос (ждём 3с между ними)
                for attempt in range(5):
                    await asyncio.sleep(3)
                    rows: list = await page.evaluate(self._JS_ROWS_ID_TITLE)
                    new_rows = [r for r in rows if r["id"] not in known_ids]
                    logger.info(
                        f"G2G title-search: попытка {attempt + 1}/5, "
                        f"строк: {len(rows)}, новых: {len(new_rows)}"
                    )

                    # Ищем лучшее совпадение через similarity (title ИЛИ rowText)
                    best_id  = None
                    best_sim = 0.0
                    for row in new_rows:
                        row_title = (row.get("title") or "").strip()
                        row_text  = (row.get("rowText") or "").strip()
                        sim_title = self._title_similarity(title, row_title) if row_title else 0.0
                        sim_row   = self._title_similarity(title, row_text)  if row_text  else 0.0
                        sim = max(sim_title, sim_row)

                        # Дополнительная проверка: запрос является подстрокой rowText
                        # Это срабатывает когда G2G добавляет цену/кол-во к тексту строки
                        # и similarity падает из-за длины, но наш title там есть
                        query_lower = search_query.lower()
                        if len(query_lower) >= 4 and query_lower in row_text.lower():
                            sim = max(sim, 0.85)  # подстрока = сильный сигнал
                        elif len(query_lower) >= 4 and query_lower in row_title.lower():
                            sim = max(sim, 0.90)

                        logger.info(
                            f"G2G title-search: кандидат ID={row['id']} "
                            f"sim={sim:.2f} (title={sim_title:.2f}, row={sim_row:.2f}) "
                            f"title='{row_title[:50]}' rowText='{row_text[:80]}'"
                        )
                        if sim > best_sim:
                            best_sim = sim
                            best_id  = row["id"]

                    # Ровно одна новая строка — G2G сам отфильтровал по нашему
                    # уникальному краткому описанию. Если нашёл 1 результат —
                    # это наш лот с вероятностью ~99%. Доверяем безусловно.
                    if len(new_rows) == 1:
                        only = new_rows[0]
                        logger.info(
                            f"G2G title-search: ✓ единственная новая строка → "
                            f"ID={only['id']} sim={best_sim:.2f} (запрос='{search_query}')"
                        )
                        return only["id"]

                    # Несколько строк — нужен similarity чтобы выбрать правильную
                    if best_id and best_sim >= 0.4:
                        logger.info(
                            f"G2G title-search: ✓ найдено по запросу '{search_query}' → "
                            f"ID={best_id} sim={best_sim:.2f}"
                        )
                        return best_id

                logger.warning(
                    f"G2G title-search: запрос '{search_query}' ничего не дал — "
                    f"укорачиваем..."
                )

            logger.warning(
                f"G2G title-search: все {len(queries)} запросов исчерпаны — лот не найден"
            )
            return None
        except Exception as e:
            logger.error(f"G2G title-search: ошибка: {e}")
            return None

    async def _get_new_lot_id_via_api(
        self,
        known_ids: set,
        title: str = "",
        price: str = "",
    ) -> Optional[str]:
        """
        FALLBACK — G2G REST API.
        Call GET /api/v1/offers?status=live&sort=-created_at&limit=5.
        Return the first offer ID NOT in known_ids.
        """
        url = f"{G2G_API_BASE}/api/v1/offers"
        headers = {"Authorization": f"Bearer {G2G_API_KEY}"}
        params = {"status": "live", "sort": "-created_at", "limit": "5"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        offers = (
                            data.get("payload", {}).get("results")
                            or data.get("data", [])
                            or []
                        )
                        for offer in offers:
                            offer_id = offer.get("id") or offer.get("offer_id", "")
                            if offer_id and offer_id not in known_ids:
                                logger.info(f"G2G API fallback: новый лот ID={offer_id}")
                                return offer_id
                        logger.warning("G2G API: нет новых ID среди результатов")
                    else:
                        logger.warning(f"G2G API: статус {resp.status}")
        except Exception as e:
            logger.warning(f"G2G API: ошибка ({e})")
        return None

    async def _get_latest_lot_id(
        self,
        page: Page,
        known_ids: set,
        title: str = "",
        price: str = "",
    ) -> Optional[str]:
        """
        FALLBACK — HTML table scan (первый ID не в known_ids).
        Последний резерв после всех более надёжных методов.
        """
        _JS_ALL = (
            "() => {"
            "  return Array.from("
            "    document.querySelectorAll('tbody tr .text-font-2nd.text-body2')"
            "  ).map(el => el.innerText.trim().replace('#', ''))"
            "   .filter(t => t.startsWith('G'));"
            "}"
        )
        try:
            await page.wait_for_function(
                "() => document.querySelector('tbody tr .text-font-2nd.text-body2') !== null",
                timeout=8000,
            )
            visible_ids: list = await page.evaluate(_JS_ALL)
            for vid in visible_ids:
                if vid not in known_ids:
                    logger.info(f"G2G HTML fallback: новый лот ID -> {vid}")
                    return vid
            logger.warning("G2G HTML fallback: все видимые ID уже есть в known_ids")
        except Exception as e:
            logger.error(f"G2G HTML fallback: ошибка: {e}")
        return None

    async def _extract_g2g_id_from_manage_button(self, page: Page) -> Optional[str]:
        """
        Извлечь G2G offer ID из кнопки/ссылки 'Manage offer' на success-странице
        ДО клика по ней.
        Кнопка обычно лежит внутри <a href="/offers/GXXX/edit"> или имеет
        href / data-href / onclick с ID. Это самый надёжный источник:
        ссылку формирует сам G2G только что, опираясь на только что созданный лот.
        """
        try:
            href = await page.evaluate("""
                () => {
                    const cands = document.querySelectorAll('a, button');
                    for (const el of cands) {
                        const txt = (el.innerText || '').trim();
                        if (!/manage\\s*offer/i.test(txt) && txt.toLowerCase() !== 'manage') continue;

                        // Variant 1: element itself is <a> with href
                        if (el.tagName === 'A' && el.href) return el.href;

                        // Variant 2: closest <a> wrapper
                        const wrap = el.closest('a');
                        if (wrap && wrap.href) return wrap.href;

                        // Variant 3: data-href
                        if (el.dataset && el.dataset.href) return el.dataset.href;

                        // Variant 4: onclick string contains /offers/G...
                        const oc = el.getAttribute('onclick') || '';
                        const m = oc.match(/\\/offers\\/(G[A-Z0-9]{10,})/);
                        if (m) return '/offers/' + m[1] + '/edit';
                    }
                    return null;
                }
            """)
            if not href:
                return None
            gid = self._extract_g2g_id_from_url(href)
            if gid:
                logger.info(f"G2G: ID из href кнопки Manage -> {gid} (href={href[:120]})")
                return gid
        except Exception as e:
            logger.warning(f"G2G: ошибка чтения href Manage: {e}")
        return None

    async def _resolve_new_lot_id(
        self,
        page: Page,
        title: str,
        known_ids: set,
        price: str = "",
        trusted_ids: Optional[list] = None,
    ) -> Optional[str]:
        """
        Определение ID только что опубликованного лота.

        trusted_ids — кандидаты, полученные из НАДЁЖНЫХ источников ДО клика
        Manage:
            • href кнопки 'Manage offer' (формирует сам G2G для нашего лота)
            • URL после publish (G2G переадресует на /offers/GXXX/edit)
            • текст success-панели "Your offer has been published"
        Для них в _verify_id_in_manage включён trust_when_title_missing=True:
        если G2G после поиска по точному ID отдаёт строку с пустым title и
        пустым rowText (баг разметки), мы всё равно доверяем.

        Если trusted_ids не подтвердились, делаем поиск по title в Manage.
        Метод "взять самый новый ID из таблицы" больше не используется —
        он привязывал чужие лоты.
        """
        trusted_ids = list(trusted_ids or [])
        # Дедупликация + фильтр по known_ids
        clean_trusted: list = []
        for cid in trusted_ids:
            if cid and cid not in known_ids and cid not in clean_trusted:
                clean_trusted.append(cid)

        # ── МЕТОД 1 пропущен — сразу метод 2 ────────────────────────────────────

        # ── МЕТОД 2: поиск по title в Manage — повторяем до победного ────────
        attempt = 0
        while True:
            attempt += 1
            logger.info(f"G2G: [метод 2] поиск по title в Manage (попытка {attempt})...")
            lot_id = await self._find_lot_by_title_in_manage(page, title, known_ids, wait_seconds=30)
            if lot_id:
                logger.info(f"G2G: ✅ [метод 2] ID найден по title → {lot_id}")
                return lot_id
            logger.warning(f"G2G: [метод 2] ID не найден — повтор через 10 сек (попытка {attempt})...")
            await asyncio.sleep(10)


    # -----------------------------------------------------------------------
    # Create lot — page-closed retry + correct ID timing
    # -----------------------------------------------------------------------

    async def create_lot(
        self,
        title: str,
        description: str,
        price: str,
        brief_description: str = "",
        hero_level: str = "9 or below",
        myth_level: str = "5 or below",
        photos: list = None,
        first_lot: bool = False,
        funpay_id: str = "",
        funpay_url: str = "",
        game: str = "Raid: Shadow Legends",
        region: str = "",
        server_g2g: str = "",
        game_handler=None,
        funpay_price: float = 0.0,
    ) -> bool:
        """Retry once if the page was closed unexpectedly."""
        for attempt in range(2):
            try:
                return await self._create_lot_inner(
                    title=title,
                    description=description,
                    price=price,
                    brief_description=brief_description,
                    hero_level=hero_level,
                    myth_level=myth_level,
                    photos=photos,
                    first_lot=first_lot,
                    funpay_id=funpay_id,
                    funpay_url=funpay_url,
                    game=game,
                    region=region,
                    server_g2g=server_g2g,
                    game_handler=game_handler,
                    funpay_price=funpay_price,
                )
            except Exception as e:
                if _is_target_closed(e):
                    logger.warning(
                        f"G2G create: страница закрыта (попытка {attempt + 1}/2) — пересоздаём..."
                    )
                    self._page = None
                    if attempt == 1:
                        logger.error("G2G create: страница закрылась дважды — сдаёмся")
                        return False
                else:
                    logger.error(f"G2G create: неожиданная ошибка: {e}")
                    return False
        return False

    async def _create_lot_inner(
        self,
        title: str,
        description: str,
        price: str,
        brief_description: str = "",
        hero_level: str = "9 or below",
        myth_level: str = "5 or below",
        photos: list = None,
        first_lot: bool = False,
        funpay_id: str = "",
        funpay_url: str = "",
        game: str = "Raid: Shadow Legends",
        region: str = "",
        server_g2g: str = "",
        game_handler=None,
        funpay_price: float = 0.0,
    ) -> bool:
        page = await self._get_page()
        try:
            title = clean_text(title)
            # EVE: не трогаем описание — публикуем точно как на FunPay
            if "eve" not in game.lower():
                description = clean_description(description)
            logger.info(f"G2G: title -> {title[:60]}")

            # Collect known IDs BEFORE publishing so we can spot the new one afterwards
            known_pairs = load_lot_pairs()
            known_g2g_ids = {
                p["g2g_id"]
                for pairs_list in known_pairs.values()
                for p in pairs_list
            }

            if first_lot:
                await self._navigate_to_form(page, game, region=region, server_g2g=server_g2g)

            logger.info('G2G: ждём загрузки формы...')
            try:
                await page.wait_for_selector("input[placeholder='Offer title']", timeout=15000)
            except Exception:
                logger.warning("G2G: форма не загрузилась — повторно открываем...")
                await self._navigate_to_form(page, game, region=region, server_g2g=server_g2g)
                await page.wait_for_selector("input[placeholder='Offer title']", timeout=20000)

            # Title
            logger.info('G2G: заполняем title...')
            title_input = (
                await page.query_selector("input[placeholder='Offer title']") or
                await page.query_selector("input[placeholder*='title' i]")
            )
            if title_input:
                await title_input.click()
                await page.keyboard.press('Control+a')
                await title_input.fill(title)
                logger.info(f"G2G: title заполнен ({len(title)} симв.)")
            else:
                logger.warning("G2G: поле 'Offer title' не найдено")

            # Description — два поля: краткое (brief) и полное (full)
            logger.info('G2G: заполняем description...')
            textareas = await page.query_selector_all('textarea')
            if len(textareas) >= 2:
                # Два поля: [0]=краткое описание, [1]=полное описание
                # brief_description = AI текст или "" если AI недоступен
                # Не используем description[:200] как запасной — полное не трогаем
                brief_text = brief_description
                full_text  = description
                for ta, text in zip(textareas[:2], [brief_text, full_text]):
                    await ta.click()
                    await page.keyboard.press('Control+a')
                    await page.keyboard.press('Delete')
                    await ta.fill(text)
                    await asyncio.sleep(0.3)
                logger.info(f'G2G: краткое={brief_text[:60]!r} | полное={full_text[:60]!r}')
            elif textareas:
                # Одно поле — заполняем полным описанием (оригинальное поведение)
                await textareas[0].click()
                await page.keyboard.press('Control+a')
                await page.keyboard.press('Delete')
                await textareas[0].fill(description)
                await asyncio.sleep(0.3)
                logger.info('G2G: одно поле description — заполнено полным текстом')

            # Game-specific dropdowns (Android, hero_level, myth_level etc)
            # Game-specific dropdowns/delivery
            if game_handler:
                handler_result = await game_handler(page, self)
                if handler_result is False:
                    logger.warning("G2G: game_handler вернул False — пропускаем лот")
                    return False
            # Photos — upload to postimages, skip any that fail all retries
            if photos:
                logger.info(f"G2G: загружаем {len(photos)} фото на postimages.org...")
                uploaded_photos = await self.upload_photos_to_postimages(photos, game=game)
                if uploaded_photos:
                    logger.info(f"G2G: вставляем {len(uploaded_photos)} фото...")
                    for idx, photo_url in enumerate(uploaded_photos):
                        await page.wait_for_selector("input[placeholder='https://']", timeout=10000)
                        media_inputs = await page.query_selector_all("input[placeholder='https://']")
                        target = media_inputs[-1]
                        await target.click()
                        await page.keyboard.press("Control+a")
                        await target.fill(photo_url)
                        await asyncio.sleep(0.4)
                        logger.info(f"G2G: фото {idx + 1} вставлено -> {photo_url}")
                        if idx < len(uploaded_photos) - 1:
                            add_btn = await page.query_selector("button:has-text('Add media')")
                            if add_btn:
                                await add_btn.click()
                                await asyncio.sleep(0.8)
                                logger.info("G2G: Add media OK")
                else:
                    logger.warning("G2G: все фото не загружены — создаём лот без фото")
            else:
                logger.info("G2G: фото нет - пропускаем Media")

            # Price
            logger.info(f"G2G: вводим цену {price}...")
            price_set = False

            price_handle = await page.evaluate_handle("""
                () => {
                    const btns = document.querySelectorAll('button')
                    for (const btn of btns) {
                        if (btn.innerText.includes('EUR')) {
                            const parent = btn.closest('[class*="col"], [class*="row"]')?.parentElement
                            return parent?.querySelector('input[type="text"]')
                        }
                    }
                    return null
                }
            """)
            price_el = price_handle.as_element()
            if price_el:
                await price_el.click()
                await page.keyboard.press("Control+a")
                await price_el.fill(price)
                await asyncio.sleep(0.3)
                logger.info("G2G: цена введена OK")
                price_set = True

            if not price_set:
                logger.warning("G2G: способ 1 не сработал - пробуем fallback...")
                all_inputs = await page.query_selector_all("input[type='text']")
                for inp in all_inputs:
                    parent_text = await page.evaluate(
                        "(el) => el.closest('.row, .col')?.parentElement?.innerText || ''", inp
                    )
                    if "EUR" in parent_text:
                        await inp.click()
                        await page.keyboard.press("Control+a")
                        await inp.fill(price)
                        await asyncio.sleep(0.3)
                        logger.info("G2G: цена введена через fallback OK")
                        price_set = True
                        break

            if not price_set:
                logger.warning("G2G: поле цены не найдено!")

            # Manual delivery — ждём появления окна с временем перед выбором
            radios = await page.query_selector_all("[aria-label='Manual delivery']")
            if radios:
                await radios[0].click()
                # Ждём появления кнопки "0 hour" (окно доставки)
                try:
                    await page.wait_for_function("""
                        () => {
                            var btns = document.querySelectorAll('button.g-btn-select');
                            for (var i = 0; i < btns.length; i++) {
                                var txt = (btns[i].innerText || '').trim();
                                if (txt.startsWith('0') && txt.toLowerCase().includes('hour')) return true;
                            }
                            return false;
                        }
                    """, timeout=10000)
                except Exception:
                    await asyncio.sleep(2)

                # Теперь нажимаем 9 hours — окно уже появилось
                await self._set_delivery_hours(page, hours=9)

            # Publish
            logger.info("G2G: нажимаем Publish...")
            await page.click("text=Publish")
            await asyncio.sleep(4)

            await page.wait_for_selector(
                "text=Your offer has been published",
                timeout=45000,
            )
            logger.info("G2G: лот опубликован OK")

            # ── СРАЗУ после публикации захватываем все НАДЁЖНЫЕ источники ID
            # (URL, текст success-панели, href кнопки Manage offer) ────────────
            # ВАЖНО: мы делаем это ДО клика Manage — на success-странице видны
            # только данные нашего нового лота, чужих ID тут нет.
            url_after_publish = page.url
            logger.info(f"G2G: URL сразу после публикации -> {url_after_publish}")

            trusted_ids: list = []

            # Источник 1: ID из href кнопки Manage offer (формирует сам G2G)
            manage_href_id = await self._extract_g2g_id_from_manage_button(page)
            if manage_href_id and manage_href_id not in known_g2g_ids:
                trusted_ids.append(manage_href_id)

            # Источник 2: ID из URL (G2G переадресует на /offers/GXXX/edit)
            url_id = self._extract_g2g_id_from_url(url_after_publish)
            if url_id and url_id not in known_g2g_ids and url_id not in trusted_ids:
                trusted_ids.append(url_id)

            # Источник 3: ID из текста success-панели (ДО клика Manage)
            text_id = await self._extract_g2g_id_from_page_content(page, known_g2g_ids)
            if text_id and text_id not in trusted_ids:
                trusted_ids.append(text_id)

            # Один из этих ID почти наверняка наш — сохраним для fallback
            _early_id = trusted_ids[0] if trusted_ids else None
            logger.info(f"G2G: trusted ID до Manage = {trusted_ids}")

            # ── Click Manage offer — перехватываем все URL-переходы ──────────────
            # G2G может:
            # 1. Открыть /offers/GXXX/edit           → ID в URL
            # 2. Открыть /offers/list?...&offer_id=GXXX → ID в параметре
            # 3. Открыть /offers/list?sort=most_recent → ID не в URL (твой кейс)
            # Для случаев 1-2 перехватываем URL через page.on("framenavigated").
            # Для случая 3 — trusted_ids уже пустой, полагаемся на метод 2/3.
            logger.info("G2G: нажимаем Manage offer...")
            manage_clicked = False
            navigated_urls: list = []

            def _on_navigate(frame):
                if frame == page.main_frame:
                    navigated_urls.append(frame.url)

            page.on("framenavigated", _on_navigate)
            try:
                btns = await page.query_selector_all("button")
                for btn in btns:
                    txt = (await btn.inner_text()).strip()
                    if "Manage offer" in txt or txt == "Manage":
                        await btn.click()
                        logger.info("G2G: Manage offer нажат")
                        manage_clicked = True
                        break

                if not manage_clicked:
                    logger.warning("G2G: кнопка Manage offer не найдена")

                if manage_clicked:
                    try:
                        await page.wait_for_selector(
                            "input[placeholder='Search title or offer number']",
                            timeout=15000,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(4)
            finally:
                page.remove_listener("framenavigated", _on_navigate)

            # Обновляем страницу — G2G может ещё не показывать новый лот
            if manage_clicked:
                logger.info("G2G: обновляем Manage-страницу перед поиском ID...")
                try:
                    await page.reload(wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(3)
                    logger.info("G2G: Manage-страница обновлена")
                except Exception as _re:
                    logger.warning(f"G2G: reload не удался: {_re}")
                    await asyncio.sleep(2)

            logger.info(f"G2G: URL на Manage-странице -> {page.url}")
            logger.info(f"G2G: все URL при навигации Manage -> {navigated_urls}")

            # Проверяем ВСЕ URL которые промелькнули во время перехода —
            # G2G иногда делает промежуточный редирект через /offers/

            # Resolve the ID of the lot we just published.
            # Navigate to next form ONLY AFTER the ID is saved.
            if funpay_id and manage_clicked:
                g2g_id = await self._resolve_new_lot_id(
                    page,
                    title=title,
                    known_ids=known_g2g_ids,
                    price=price,
                    trusted_ids=trusted_ids,
                )
                if g2g_id:
                    save_lot_pair(
                        funpay_id, g2g_id, title, funpay_url, game,
                        funpay_price=funpay_price,
                        g2g_price=float(price),
                    )
                else:
                    logger.warning("G2G: ID лота не получен ни одним методом — пара не сохранена")
            elif funpay_id and not manage_clicked and _early_id:
                # Manage не открылся, но ID уже есть из надёжного источника — сохраняем
                logger.info(f"G2G: сохраняем ID полученный до Manage -> {_early_id}")
                save_lot_pair(
                    funpay_id, _early_id, title, funpay_url, game,
                    funpay_price=funpay_price,
                    g2g_price=float(price),
                )

            await self._navigate_to_form(page, game, region=region, server_g2g=server_g2g)

            return True

        except Exception as e:
            logger.error(f"G2G: ошибка публикации: {e}")
            return False

    # -----------------------------------------------------------------------
    # Dropdown helper
    # -----------------------------------------------------------------------

    async def update_lot_price(self, g2g_id: str, new_price: str) -> bool:
        """
        Обновляет цену существующего лота через Edit.
        Переходит на /offers/G.../edit, меняет цену, сохраняет.
        """
        page = await self._get_page()
        try:
            logger.info(f"G2G: обновляем цену лота {g2g_id} → ${new_price}")
            await page.goto(
                f"{BASE}/offers/{g2g_id}/edit",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)

            # Находим поле цены рядом с EUR
            price_handle = await page.evaluate_handle("""
                () => {
                    const btns = document.querySelectorAll('button');
                    for (const btn of btns) {
                        if (btn.innerText.includes('EUR')) {
                            const parent = btn.closest('[class*="col"], [class*="row"]')?.parentElement;
                            return parent?.querySelector('input[type="text"]');
                        }
                    }
                    return null;
                }
            """)
            price_el = price_handle.as_element()
            if not price_el:
                logger.warning(f"G2G: поле цены не найдено для {g2g_id}")
                return False

            await price_el.click()
            await page.keyboard.press("Control+a")
            await price_el.fill(new_price)
            await asyncio.sleep(0.5)

            # Нажимаем Save/Update
            for btn in await page.query_selector_all("button"):
                txt = (await btn.inner_text()).strip()
                if txt in ("Save", "Update", "Save changes"):
                    await btn.click()
                    await asyncio.sleep(2)
                    logger.info(f"G2G: цена {g2g_id} обновлена → ${new_price}")
                    return True

            logger.warning(f"G2G: кнопка Save не найдена для {g2g_id}")
            return False

        except Exception as e:
            logger.error(f"G2G: ошибка обновления цены {g2g_id}: {e}")
            return False

    async def _set_delivery_hours(self, page: Page, hours: int = 9):
        """
        Find '0 hour' delivery button by inner_text, click it, select '9 hours'.
        Uses inner_text() same as all methods in this file.
        Works regardless of how many buttons are on the page.
        """
        btns = await page.query_selector_all("button.g-btn-select")
        hour_btn = None
        for btn in btns:
            txt = (await btn.inner_text()).strip()
            if txt.startswith("0") and "hour" in txt.lower():
                hour_btn = btn
                break

        if not hour_btn:
            logger.warning("G2G Delivery: кнопка '0 hour' не найдена")
            return False

        await hour_btn.click()
        logger.info("G2G Delivery: dropdown часов открыт")
        await asyncio.sleep(1.5)

        target = f"{hours} hours" if hours > 1 else f"{hours} hour"
        for item in await page.query_selector_all(".q-virtual-scroll__content .q-item"):
            txt = (await item.inner_text()).strip()
            if txt.lower() == target.lower():
                await item.click()
                await asyncio.sleep(1)
                logger.info(f"G2G Delivery: '{target}' выбрано OK")
                return True

        logger.warning(f"G2G Delivery: '{target}' не найдено в списке")
        return False

    async def _select_dropdown(self, page: Page, btn_index: int, value: str):
        btns = await page.query_selector_all("button.g-btn-select")
        if btn_index >= len(btns):
            logger.warning(f"G2G: дропдаун {btn_index} не найден (всего {len(btns)} кнопок)")
            return
        await btns[btn_index].click()
        await asyncio.sleep(1.5)
        for item in await page.query_selector_all(".q-virtual-scroll__content .q-item"):
            if (await item.inner_text()).strip().lower() == value.lower():
                await item.click()
                await asyncio.sleep(1)
                logger.info(f"G2G: дропдаун {btn_index} -> '{value}' OK")
                return
        logger.warning(f"G2G: дропдаун {btn_index} - не нашли '{value}'")

    # -----------------------------------------------------------------------
    # Price parser
    # -----------------------------------------------------------------------

    def _parse_price(self, text: str) -> float:
        m = re.search(r"[\d]+[.,]?[\d]*", text.replace(" ", ""))
        if m:
            try:
                return float(m.group(0).replace(",", "."))
            except ValueError:
                pass
        return 0.0