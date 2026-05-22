import asyncio
import logging
import json
import re
import random
import aiohttp
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright
from funpay_scraper_raid import FunPayScraper, parse_proxy
from g2g_bot import G2GBot
from description_generator import generate_title_from_description
from price_checker import run_price_check_wow, run_price_check_raid, run_price_check
import games.raid as raid_game
import games.wow as wow_game
import games.zzz as zzz_game
import games.eve as eve_game
import games.tl as tl_game
import games.bdo as bdo_game
import games.sw as sw_game
import games.rbl as rbl_game
import games.warframe_game as wf_game
import warframe_slots
import games.drakensang_game as dso_game
import drakensang_slots
import games.diablo_game as di_game
import diablo_slots
from ai_brief import generate_brief_ai
import storage

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# Set by main() from config — used by run_pass for AI brief generation
_claude_api_key: str = ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            LOG_DIR / f"bot_{datetime.now():%Y%m%d_%H%M%S}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("main")

PROFILE_DIR    = "./browser_profile"
# used_lots файл определяется для каждой игры отдельно в run_pass
LOT_PAIRS_FILE = "lot_pairs.json"
CONFIG_FILE    = "config.json"

# Words that are NOT hero names — skipped when extracting heroes from title
_HERO_SKIP = {
    "Auto", "Delivery", "Account", "Level", "Legendary", "Mythical",
    "Mythic", "Epic", "Rare", "Power", "Arena", "Top", "Best",
    "Shadow", "Legends", "Raid", "Starting", "Starter", "Personal",
    "Email", "Password", "Champion", "Champions", "Heroes", "Hero",
    "Relics", "Relic", "Fast", "Instant", "Transfer", "Ready",
}


def load_config() -> dict:
    return json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))


def show_menu(games: list) -> str:
    print(f"\n{'='*40}")
    print(f"  G2G Bot")
    print(f"{'='*40}")
    for i, game in enumerate(games, 1):
        print(f"  {i}. {game['name']}")
    print(f"  0. Очистить лоты")
    print(f"  p. Проверить цены")
    print(f"  m. Мигрировать/проверить ID (диагностика)")
    print(f"{'='*40}")
    valid = [str(i) for i in range(len(games) + 1)] + ["p", "m"]
    while True:
        choice = input("Выбери номер: ").strip().lower()
        if choice in valid:
            return choice
        print("Неверный выбор, попробуй снова")


def get_hero_level(l_heroes: int) -> str:
    if l_heroes >= 300: return "300+"
    elif l_heroes >= 250: return "250+"
    elif l_heroes >= 200: return "200+"
    elif l_heroes >= 100: return "100+"
    elif l_heroes >= 50:  return "50+"
    elif l_heroes >= 10:  return "10+"
    else:                 return "9 or below"


def get_myth_level(m_heroes: int) -> str:
    if m_heroes >= 10: return "10+"
    elif m_heroes >= 6: return "6+"
    else:               return "5 or below"


def _extract_hero_names(title: str) -> list:
    """Extract Title-case hero names from lot title."""
    return [
        w for w in re.findall(r"\b([A-Z][a-zA-Z]{2,})\b", title or "")
        if w not in _HERO_SKIP
    ]


async def run_pass(funpay: FunPayScraper, g2g: G2GBot, game_cfg: dict, is_first_pass: bool, context=None):
    created = 0
    game_name = game_cfg["name"]

    # Build combined used_lots set ONCE before the loop
    used_lots = storage.load_used_lots(game_name)
    used_lots.update(storage.get_all_funpay_ids())
    logger.info(f"used_lots: {len(used_lots)} ID для [{game_name}]")

    for i in range(game_cfg["lots_per_pass"]):
        logger.info(f"\n--- Лот {i+1}/{game_cfg['lots_per_pass']} ---")

        lot = None
        for _fp_attempt in range(3):
            try:
                lot = await funpay.get_best_lot(
                    lots_url=game_cfg["funpay_url"],
                    min_seller_reviews=game_cfg.get("min_seller_reviews", 0),
                    require_auto_delivery=game_cfg.get("require_auto_delivery", False),
                    min_price_usd=game_cfg.get("min_funpay_price_usd", 0),
                    max_price_usd=game_cfg.get("max_funpay_price_usd", 999999),
                    used_lot_ids=used_lots,
                    title_blacklist=game_cfg.get("title_blacklist", []),
                    builtin_blacklist=game_cfg.get("builtin_blacklist", []),
                    blacklist_whitelist=game_cfg.get("blacklist_whitelist", []),
                    seller_blacklist=game_cfg.get("seller_blacklist", []),
                    game=game_cfg["name"],
                    region_filter=game_cfg.get("funpay_region_filter", ""),
                    server_whitelist=game_cfg.get("server_whitelist") or None,
                    funpay_tab=game_cfg.get("funpay_tab", ""),
                    skip_photos=game_cfg.get("is_roblox", False),
                )
                break
            except Exception as _e:
                logger.warning(f"FunPay: ошибка попытка {_fp_attempt+1}/3: {_e}")
                if _fp_attempt < 2:
                    await asyncio.sleep(15)

        if not lot:
            logger.warning("FunPay: лот не найден - пропускаем")
            break

        lot_url = lot.href if lot.href.startswith("http") else f"https://funpay.com{lot.href}"
        logger.info(f"FunPay: лот ID={lot.lot_id}")
        logger.info(f"FunPay: ссылка -> {lot_url}")
        logger.info(f"FunPay: цена ${lot.price:.2f} | leg:{lot.l_heroes} myth:{lot.m_heroes}")
        logger.info(f"FunPay: фото: {len(lot.photos)}")

        # --- Проверка builtin_blacklist по ПОЛНОЙ странице оффера ---
        # Всегда загружаем страницу явно — lot.detailed_description пустой,
        # скрапер не заходит на страницу каждого лота при get_best_lot().
        _bl_phrases = [p.lower() for p in game_cfg.get("builtin_blacklist", [])]
        if _bl_phrases:
            _wl_phrases = [p.lower() for p in game_cfg.get("blacklist_whitelist", [])]

            # Загружаем страницу оффера через браузер (JS-рендер + куки)
            logger.info(f"FunPay blacklist: загружаем {lot_url} (browser) ...")
            _page_text = await _funpay_fetch_offer_text_browser(context, lot_url)

            # ЗАЩИТА: если страница не загрузилась — пропускаем лот,
            # чтобы не опубликовать потенциально плохой лот без проверки.
            if not _page_text:
                logger.warning(
                    f"builtin_blacklist: СТОП — страница лота {lot.lot_id} "
                    f"не загрузилась, не можем проверить блэклист — пропускаем"
                )
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue

            # Объединяем ВСЁ: данные лота + текст страницы (JS-рендер)
            _full_text = " ".join([
                (lot.title or ""),
                (lot.description or ""),
                (lot.detailed_description or ""),
                _page_text,
            ]).lower()

            logger.info(
                f"FunPay blacklist: текст для проверки — {len(_full_text)} символов"
            )
            # Выводим первые 500 символов страницы для диагностики
            logger.info(
                f"FunPay blacklist: начало текста страницы -> "
                f"{_page_text[:500]!r}"
            )

            _wl_hit  = any(w in _full_text for w in _wl_phrases)
            _bl_match = None if _wl_hit else next(
                (p for p in _bl_phrases if p in _full_text), None
            )

            if _bl_match:
                logger.warning(
                    f"builtin_blacklist: СТОП — фраза '{_bl_match}' "
                    f"найдена в лоте {lot.lot_id} ({lot_url}) — пропускаем"
                )
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue
            else:
                logger.info(f"FunPay blacklist: лот {lot.lot_id} чист — продолжаем")

        # Price tier multiplier
        multiplier = game_cfg.get("price_multiplier", 2.0)
        for tier in game_cfg.get("price_tiers", []):
            if tier["min"] <= lot.price < tier["max"]:
                multiplier = tier["multiplier"]
                break

        # Если у нас скидка на FunPay — считаем от нашей цены, а не публичной
        fp_factor = game_cfg.get("funpay_price_factor", 1.0)
        effective_price = round(lot.price * fp_factor, 2)
        our_price = round(effective_price * multiplier, 2)
        logger.info(f"Цена G2G: ${lot.price:.2f} x {fp_factor} (скидка) x {multiplier} = ${our_price:.2f}")

        hero_level = get_hero_level(lot.l_heroes)
        myth_level = get_myth_level(lot.m_heroes)
        logger.info(f"Дропдауны: legendary={hero_level} mythical={myth_level}")

        # Game-specific params
        server_g2g = ""
        wow_params = {}
        zzz_params = {}
        zzz_level  = "9 or below"
        if game_cfg.get("region"):
            wow_params = await wow_game.get_lot_params(lot_url)
            server_g2g = wow_params.get("server_g2g", "")

            # Если сервер не поддерживается (FR/DE/не найден) — пропускаем лот
            if not server_g2g:
                logger.warning(f"WOW: сервер '{wow_params.get('server')}' не поддерживается — пропускаем")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue

            # Пропускаем лоты с уровнем выше 70
            lot_level = wow_params.get("level")
            if lot_level and int(lot_level) > 70:
                logger.warning(f"WOW: уровень {lot_level} > 70 — пропускаем")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue

        elif "zenless" in game_cfg["name"].lower():
            # lot.level = data-f-level с карточки FunPay (точный уровень)
            # НЕ парсим текст — "13 Lega" даёт ложное срабатывание
            level_raw = lot.level or 0
            zzz_level = zzz_game.get_zzz_level(level_raw) if level_raw > 0 else "9 or below"
            zzz_params = {
                "zzz_level":    zzz_level,
                "level_raw":    level_raw,
            }

            # Фильтр по data-f-region карточки FunPay — надёжнее текстового парсинга
            lot_region = lot.region
            if lot_region and lot_region.lower() != "europe":
                logger.warning(f"ZZZ: регион '{lot_region}' ≠ Europe — пропускаем лот")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue

            logger.info(f"ZZZ: zzz_level={zzz_level!r} | level_raw={level_raw} | region={lot_region or 'не указан→Europe'}")

        elif "eve" in game_cfg["name"].lower():
            # EVE Online — только Tranquility, SP парсим из текста
            logger.info(f"EVE: лот принят")

        elif "throne" in game_cfg["name"].lower() or "liberty" in game_cfg["name"].lower():
            # Throne and Liberty — фильтруем по серверу
            from tl_slots import parse_tl_server
            tl_server_raw = lot.server or ""
            tl_server_g2g = parse_tl_server(tl_server_raw)
            if not tl_server_g2g:
                logger.warning(
                    f"TL: сервер '{tl_server_raw}' не поддерживается G2G — пропускаем"
                )
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue
            server_g2g = tl_server_g2g
            logger.info(f"TL: сервер '{tl_server_raw}' → '{tl_server_g2g}'")

        elif "black desert" in game_cfg["name"].lower():
            # BDO — проверяем класс, пропускаем если не найден
            from bdo_slots import normalize_bdo_class, bdo_level_tier
            raw_class = lot.bdo_class or ""
            bdo_class_g2g = normalize_bdo_class(raw_class)
            if not bdo_class_g2g:
                logger.warning(f"BDO: класс '{raw_class}' не найден — пропускаем лот")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue
            bdo_level_tier_val = bdo_level_tier(lot.level or 0)
            logger.info(f"BDO: класс='{bdo_class_g2g}' уровень={lot.level} тир='{bdo_level_tier_val}'")

        elif "summoners" in game_cfg["name"].lower():
            # Summoners War — пропускаем не-EU лоты
            # FunPay scraper для SW заполняет lot.server (не lot.region)
            from sw_slots import is_eu_server
            sw_region = (lot.server or lot.region or "").strip()
            sw_check_text = sw_region or (lot.title or "")
            if not is_eu_server(sw_check_text):
                logger.warning(f"SW: регион '{sw_region or '?'}' / title не EU — пропускаем")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue
            logger.info(f"SW: EU лот принят (server={sw_region or 'из title'})")

        elif "warframe" in game_cfg["name"].lower():
            wf_rank     = lot.rank or 0
            wf_platform = lot.platform or "PC"
            logger.info(f"WF: rank={wf_rank} platform={wf_platform!r}")

        elif "drakensang" in game_cfg["name"].lower():
            dso_server_g2g = drakensang_slots.get_dso_server(lot.server or "")
            dso_class_g2g  = drakensang_slots.get_dso_class(lot.char_class or "")
            dso_level      = lot.level or 0
            if not dso_server_g2g:
                logger.warning(f"DSO: сервер '{lot.server}' не поддерживается — пропускаем")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue
            logger.info(f"DSO: server={dso_server_g2g!r} class={dso_class_g2g!r} level={dso_level}")

        elif "diablo" in game_cfg["name"].lower():
            di_server_g2g = diablo_slots.get_di_server(lot.server or "")
            di_class_g2g  = diablo_slots.get_di_class(lot.char_class or "")
            di_level      = lot.level or 0
            if not di_server_g2g:
                logger.warning(f"DI: сервер '{lot.server}' не поддерживается — пропускаем")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue
            logger.info(f"DI: server={di_server_g2g!r} class={di_class_g2g!r} level={di_level}")

        # Generate unique title — shuffle segments of lot.title (same as Raid)
        _orig_title = (lot.title or "").strip()
        _description = (lot.detailed_description or lot.description or "").strip()
        # Build extra_slots from wow_params (priority over regex)
        extra_slots = {}
        if game_cfg.get("region") and wow_params:
            if wow_params.get("level"):
                extra_slots["level"] = f"lvl {wow_params['level']}"
            if wow_params.get("wow_class"):
                extra_slots["wow_class"] = wow_params["wow_class"]
            if wow_params.get("server_g2g"):
                _srv = wow_params["server_g2g"].split("[")[0].strip()
                if _srv:
                    extra_slots["realm"] = _srv
            if wow_params.get("faction"):
                extra_slots["faction"] = wow_params["faction"]
            if wow_params.get("race"):
                extra_slots["race"] = wow_params["race"]
        new_title = generate_title_from_description(
            title_hint=_orig_title,
            description=_description,
            server=server_g2g or "",
            extra_slots=extra_slots,
            leg=lot.l_heroes,
            myth=lot.m_heroes,
            game=game_cfg["name"],
        )
        # ZZZ использует zzz_slots с level_override из data-f-level
        if "zenless" in game_cfg["name"].lower():
            try:
                from zzz_slots import generate_zzz_title
                new_title = generate_zzz_title(
                    title_hint=_orig_title,
                    description=_description,
                    level_override=zzz_params.get("level_raw", 0),
                    server_override=lot.region or "Europe",
                )
            except Exception as _e:
                logger.warning(f"ZZZ title failed: {_e}")
                new_title = _orig_title[:128]
        elif "eve" in game_cfg["name"].lower():
            new_title = _orig_title[:128]
        elif "throne" in game_cfg["name"].lower() or "liberty" in game_cfg["name"].lower():
            try:
                from tl_slots import generate_tl_title
                new_title = generate_tl_title(
                    title_hint=_orig_title,
                    description=_description,
                    server_g2g=server_g2g,
                )
            except Exception as _e:
                logger.warning(f"TL title failed: {_e}")
                new_title = _orig_title[:128]
        elif "black desert" in game_cfg["name"].lower():
            try:
                from bdo_slots import generate_bdo_title
                new_title = generate_bdo_title(
                    title=_orig_title,
                    description=_description,
                    level=lot.level or 0,
                    bdo_class=bdo_class_g2g,
                )
            except Exception as _e:
                logger.warning(f"BDO title failed: {_e}")
                new_title = _orig_title[:128]
        elif "summoners" in game_cfg["name"].lower():
            try:
                from sw_slots import generate_sw_title
                new_title = generate_sw_title(_orig_title, _description)
            except Exception as _e:
                logger.warning(f"SW title failed: {_e}")
                new_title = _orig_title[:128]
        elif game_cfg.get("is_roblox"):
            new_title = _orig_title[:128]
        logger.info(f"Title orig:  {_orig_title[:70]!r}")
        logger.info(f"Title new:   {new_title!r}")

        # Description
        # brief_description → краткое (textarea 1 на G2G)
        # description       → полное  (textarea 2 на G2G): точно как на FunPay
        description = lot.detailed_description or lot.description or lot.title or ""

        # Extra context per game for AI prompt
        _ai_extra = ""
        if game_cfg.get("region"):
            _ai_extra = f"Server: {server_g2g}, Level: {wow_params.get('level','')}, Class: {wow_params.get('wow_class','')}, Race: {wow_params.get('race','')}, Faction: {wow_params.get('faction','')}"
        elif "zenless" in game_cfg["name"].lower():
            _ai_extra = f"Level: {zzz_params.get('level_raw',0)}, Server: {lot.region or 'Europe'}"
        elif "warframe" in game_cfg["name"].lower():
            _ai_extra = f"MR: {lot.rank or 0}, Platform: {lot.platform or 'PC'}"
        elif "drakensang" in game_cfg["name"].lower():
            _ai_extra = f"Level: {lot.level or 0}, Class: {dso_class_g2g}, Server: {dso_server_g2g}"
        elif "diablo" in game_cfg["name"].lower():
            _ai_extra = f"Level: {di_level}, Class: {di_class_g2g}, Server: {di_server_g2g}"
        elif "black desert" in game_cfg["name"].lower():
            _ai_extra = f"Level: {lot.level or 0}, Class: {bdo_class_g2g}, Server: {server_g2g}"
        elif "eve" in game_cfg["name"].lower():
            _ai_extra = f"Server: {lot.server or ''}, Region: {lot.region or 'EU'}"
        elif "throne" in game_cfg["name"].lower() or "liberty" in game_cfg["name"].lower():
            _ai_extra = f"Server: {server_g2g}, Level: {lot.level or ''}"
        elif "summoners" in game_cfg["name"].lower():
            _ai_extra = f"Server: {lot.region or 'Global'}"
        elif "raid" in game_cfg["name"].lower():
            _ai_extra = f"Mythics: {lot.m_heroes or 0}, Legendaries: {lot.l_heroes or 0}"

        # Roblox — без brief, оставляем как есть
        if game_cfg.get("is_roblox"):
            brief_description = description[:200]
            logger.info(f"RBL full (FunPay): {description[:80]!r}")
        else:
            # 1. Пробуем AI
            _ai_source = f"{lot.title or ''}\n{lot.description or ''}\n{lot.detailed_description or ''}".strip()

            # Пропускаем лот если описание слишком короткое — нечего публиковать
            if len(_ai_source) < 50:
                logger.info(f"Пропускаем лот {lot.lot_id} — описание слишком короткое ({len(_ai_source)} симв.): {_ai_source!r}")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue

            brief_description = generate_brief_ai(
                game_name=game_cfg["name"],
                description=_ai_source,
                extra_context=_ai_extra,
                api_key=_claude_api_key,
            )

            # Если AI вернул слишком короткий результат — тоже пропускаем
            if brief_description and len(brief_description) < 20:
                logger.info(f"Пропускаем лот {lot.lot_id} — AI brief слишком короткий: {brief_description!r}")
                storage.save_used_lot(lot.lot_id, game_name)
                used_lots.add(lot.lot_id)
                continue

            # 2. Fallback на старую логику если AI недоступен
            if not brief_description:
                logger.info(f"AI brief недоступен — используем старую логику")
                if game_cfg.get("region"):
                    brief_description = ""
                elif "zenless" in game_cfg["name"].lower():
                    try:
                        from zzz_slots import generate_zzz_brief_description
                        brief_description = generate_zzz_brief_description(
                            title=lot.title or "",
                            description=f"{lot.description or ''} {lot.detailed_description or ''}".strip(),
                            level_override=zzz_params.get("level_raw", 0),
                            server_override=lot.region or "Europe",
                        )
                    except Exception as _e:
                        brief_description = (lot.title or "")[:200]
                elif "eve" in game_cfg["name"].lower():
                    try:
                        from eve_slots import generate_eve_brief_description
                        brief_description = generate_eve_brief_description(
                            title=lot.title or "",
                            description=f"{lot.description or ''} {lot.detailed_description or ''}".strip(),
                        )
                    except Exception:
                        brief_description = ""
                elif "throne" in game_cfg["name"].lower() or "liberty" in game_cfg["name"].lower():
                    try:
                        from tl_slots import generate_tl_brief_description
                        brief_description = generate_tl_brief_description(
                            title=lot.title or "",
                            description=f"{lot.description or ''} {lot.detailed_description or ''}".strip(),
                            server_g2g=server_g2g,
                        )
                    except Exception:
                        brief_description = ""
                elif "black desert" in game_cfg["name"].lower():
                    try:
                        from bdo_slots import generate_bdo_brief_description
                        brief_description = generate_bdo_brief_description(
                            title=lot.title or "",
                            description=f"{lot.description or ''} {lot.detailed_description or ''}".strip(),
                            level=lot.level or 0,
                            bdo_class=bdo_class_g2g,
                        )
                    except Exception:
                        brief_description = ""
                elif "summoners" in game_cfg["name"].lower():
                    try:
                        from sw_slots import generate_sw_title
                        brief_description = generate_sw_title(
                            title=lot.title or "",
                            description=f"{lot.description or ''} {lot.detailed_description or ''}".strip(),
                        )
                    except Exception:
                        brief_description = (lot.title or "")[:200]
                elif "warframe" in game_cfg["name"].lower():
                    try:
                        brief_description = warframe_slots.generate_warframe_brief(
                            rank=lot.rank or 0,
                            original_brief=lot.description or "",
                        )
                    except Exception:
                        brief_description = (lot.description or "")[:200]
                elif "drakensang" in game_cfg["name"].lower():
                    try:
                        brief_description = drakensang_slots.generate_dso_brief(
                            level=lot.level or 0,
                            dso_class=dso_class_g2g,
                            server=dso_server_g2g,
                            original_brief=lot.description or "",
                            detailed=lot.detailed_description or "",
                        )
                    except Exception:
                        brief_description = (lot.description or "")[:200]
                elif "diablo" in game_cfg["name"].lower():
                    # Fallback: class + level + server
                    _di_parts = []
                    if di_class_g2g:  _di_parts.append(di_class_g2g)
                    if di_level:      _di_parts.append(f"Lv{di_level}")
                    if di_server_g2g: _di_parts.append(di_server_g2g)
                    brief_description = " | ".join(_di_parts)
                else:
                    # Raid
                    from description_generator import generate_brief_description_raid
                    brief_description = generate_brief_description_raid(
                        title=lot.title,
                        description=lot.description,
                        detailed_description=lot.detailed_description,
                        leg=lot.l_heroes,
                        myth=lot.m_heroes,
                    ) or ""

            logger.info(f"Brief: {brief_description!r}")
            logger.info(f"Full (FunPay): {description[:80]!r}")

            # Если AI сгенерировал brief — используем его как Title (кроме Roblox)
            if brief_description and not game_cfg.get("is_roblox"):
                if len(brief_description) > 128:
                    # Режем по последнему " | " чтобы не обрывать слово посередине
                    _cut = brief_description[:128].rfind(" | ")
                    ai_title = brief_description[:_cut] if _cut > 0 else brief_description[:128]
                else:
                    ai_title = brief_description
                ai_title = ai_title.rstrip(" |+—")
                logger.info(f"Title (AI override): {ai_title!r}")
                new_title = ai_title
        # Build game_handler — function called during G2G form filling
        if game_cfg.get("region"):
            # WOW
            wow_form_params = {
                "server_g2g": server_g2g,
                "wow_class":  wow_params.get("wow_class", ""),
                "race":       wow_params.get("race", ""),
                "level":      wow_params.get("level", ""),
                "faction":    wow_params.get("faction", ""),
                "country":    "Ukraine",
            }
            async def game_handler(page, bot, _p=wow_form_params, _cfg=game_cfg):
                return await wow_game.fill_form(page, bot, _cfg, _p)
        elif "zenless" in game_cfg["name"].lower():
            # ZZZ
            async def game_handler(page, bot, _cfg=game_cfg, _zl=zzz_level):
                return await zzz_game.fill_form(page, bot, _cfg, _zl)
        elif "eve" in game_cfg["name"].lower():
            # EVE Online
            async def game_handler(page, bot, _cfg=game_cfg):
                return await eve_game.fill_form(page, bot, _cfg)
        elif "throne" in game_cfg["name"].lower() or "liberty" in game_cfg["name"].lower():
            # Throne and Liberty
            _tl_params = {"server_g2g": server_g2g}
            async def game_handler(page, bot, _cfg=game_cfg, _tp=_tl_params):
                return await tl_game.fill_form(page, bot, _cfg, _tp)
        elif "black desert" in game_cfg["name"].lower():
            _bdo_params = {
                "level_tier": bdo_level_tier_val,
                "bdo_class":  bdo_class_g2g,
            }
            async def game_handler(page, bot, _cfg=game_cfg, _bp=_bdo_params):
                return await bdo_game.fill_form(page, bot, _cfg, _bp)
        elif "summoners" in game_cfg["name"].lower():
            _sw_params = {
                "server": lot.region or "",
                "title":  lot.title or "",
            }
            async def game_handler(page, bot, _cfg=game_cfg, _sp=_sw_params):
                return await sw_game.fill_form(page, bot, _cfg, _sp)
        elif "warframe" in game_cfg["name"].lower():
            _wf_params = {
                "platform": wf_platform,
                "rank":     warframe_slots.warframe_rank_tier(wf_rank),
            }
            async def game_handler(page, bot, _cfg=game_cfg, _wp=_wf_params):
                return await wf_game.fill_form(page, bot, _cfg, _wp)
        elif "drakensang" in game_cfg["name"].lower():
            _dso_params = {
                "server":     dso_server_g2g,
                "level_tier": drakensang_slots.dso_level_tier(lot.level or 0),
                "dso_class":  dso_class_g2g,
            }
            async def game_handler(page, bot, _cfg=game_cfg, _dp=_dso_params):
                return await dso_game.fill_form(page, bot, _cfg, _dp)
        elif "diablo" in game_cfg["name"].lower():
            _di_params = {
                "region":     "EU",
                "server":     di_server_g2g,
                "level_tier": diablo_slots.di_level_tier(di_level),
                "di_class":   di_class_g2g,
            }
            async def game_handler(page, bot, _cfg=game_cfg, _dip=_di_params):
                return await di_game.fill_form(page, bot, _cfg, _dip)
        elif game_cfg.get("is_roblox"):
            _rbl_params = {"g2g_game_name": game_cfg.get("g2g_game_name", "")}
            async def game_handler(page, bot, _cfg=game_cfg, _rp=_rbl_params):
                return await rbl_game.fill_form(page, bot, _cfg, _rp)
        else:
            # Raid (or any game with g2g_dropdowns config)
            async def game_handler(page, bot, _cfg=game_cfg, _hl=hero_level, _ml=myth_level):
                return await raid_game.fill_form(page, bot, _cfg, None, _hl, _ml)

        photos_for_lot = _build_photo_list(lot.photos, game_cfg["name"])
        # RBL G2G form shows only Title+Description — no photo URL input
        if game_cfg.get("is_roblox"):
            photos_for_lot = []
        logger.info(f"Фото для лота: {len(photos_for_lot)} (галерея + лот)")

        ok = False
        for _attempt in range(3):
            ok = await g2g.create_lot(
                title=new_title,
                description=description,
                price=str(our_price),
                brief_description=brief_description,
                hero_level=hero_level,
                myth_level=myth_level,
                photos=photos_for_lot,
                first_lot=(i == 0 and is_first_pass),
                funpay_id=lot.lot_id,
                funpay_url=lot_url,
                game=game_cfg.get("g2g_brand", game_cfg["name"]),
                region=(
                    "EU" if ("throne" in game_cfg["name"].lower() or "liberty" in game_cfg["name"].lower() or "diablo" in game_cfg["name"].lower())
                    else game_cfg.get("region", "")
                ),
                server_g2g=server_g2g,
                game_handler=game_handler,
                funpay_price=lot.price,
            )
            if ok is None:
                # game_handler вернул False — лот пропускаем без повтора
                _id_to_save = lot.lot_id or lot.href
                if _id_to_save:
                    storage.save_used_lot(_id_to_save, game_name)
                    used_lots.add(_id_to_save)
                break
            if ok:
                break
            if _attempt < 2:
                logger.warning(f"G2G: ошибка публикации, попытка {_attempt + 1}/3 — повтор через 30с...")
                await asyncio.sleep(30)

        if ok:
            created += 1
            _id_to_save = lot.lot_id or lot.href
            if _id_to_save:
                storage.save_used_lot(_id_to_save, game_name)
                used_lots.add(_id_to_save)
            logger.info(f"G2G: Лот опубликован OK ({created}/{game_cfg['lots_per_pass']}) | {new_title[:40]} | ${our_price:.2f}")
            _stats_path = Path("stats.json")
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                _stats = {}
                if _stats_path.exists():
                    _stats = json.loads(_stats_path.read_text(encoding="utf-8"))
                if today not in _stats:
                    _stats[today] = {}
                _stats[today][game_name] = _stats[today].get(game_name, 0) + 1
                # Оставляем только последние 7 дней
                if len(_stats) > 7:
                    for old_day in sorted(_stats.keys())[:-7]:
                        del _stats[old_day]
                _stats_tmp = _stats_path.with_suffix(".tmp")
                _stats_tmp.write_text(json.dumps(_stats, ensure_ascii=False, indent=2), encoding="utf-8")
                _stats_tmp.replace(_stats_path)
            except Exception as _e:
                logger.warning(f"stats: не удалось записать: {_e}")
        else:
            _id_to_save = lot.lot_id or lot.href
            if _id_to_save:
                storage.save_used_lot(_id_to_save, game_name)
                used_lots.add(_id_to_save)
            logger.error("G2G: ошибка публикации после 3 попыток — прерываем проход")
            break

        await asyncio.sleep(2)

    return created


async def _roblox_worker(worker_id: int, funpay: FunPayScraper, context, sub_cfgs: list, is_first_pass: bool):
    """Один воркер Roblox: обрабатывает назначенные под-игры последовательно."""
    g2g_worker = G2GBot(context)
    total = 0
    for sub_cfg in sub_cfgs:
        try:
            logger.info(f"RBL Worker {worker_id}: [{sub_cfg['name']}]")
            created = await run_pass(funpay, g2g_worker, sub_cfg, is_first_pass, context)
            total += created
            logger.info(f"RBL Worker {worker_id}: [{sub_cfg['name']}] → {created} лотов")
        except Exception as _e:
            logger.error(f"RBL Worker {worker_id}: ошибка [{sub_cfg['name']}]: {_e}", exc_info=True)
    return total


async def run_roblox_pass(funpay: FunPayScraper, g2g: G2GBot, roblox_cfg: dict, is_first_pass: bool, context):
    """
    Обрабатывает все включённые Roblox под-игры.
    workers=2 → 2 параллельных воркера (нечётные + чётные игры).
    workers=1 → последовательная обработка.
    """
    enabled = [rg for rg in roblox_cfg.get("roblox_games", []) if rg.get("enabled", True)]
    workers_count = roblox_cfg.get("workers", 1)

    # Строим sub_cfg для каждой под-игры (наследуем общие настройки от родителя)
    base = {k: v for k, v in roblox_cfg.items() if k not in ("roblox_games", "workers")}
    sub_cfgs = []
    for rg in enabled:
        sub_cfg = dict(base)
        sub_cfg["name"]          = rg["g2g_name"]   # ключ для lot_pairs / used_lots / stats
        sub_cfg["funpay_url"]    = rg["funpay_url"]
        sub_cfg["g2g_game_name"] = rg["g2g_name"]   # для rbl.py → дропдаун Games
        sub_cfg["is_roblox"]     = True
        sub_cfgs.append(sub_cfg)

    if not sub_cfgs:
        logger.warning("RBL: нет включённых под-игр")
        return 0

    if workers_count >= 2 and len(sub_cfgs) >= 2:
        w1 = sub_cfgs[::2]
        w2 = sub_cfgs[1::2]
        logger.info(f"RBL: 2 воркера — Worker1: {len(w1)} игр, Worker2: {len(w2)} игр")
        results = await asyncio.gather(
            _roblox_worker(1, funpay, context, w1, is_first_pass),
            _roblox_worker(2, funpay, context, w2, is_first_pass),
        )
        total = sum(results)
    else:
        total = 0
        for sub_cfg in sub_cfgs:
            total += await run_pass(funpay, g2g, sub_cfg, is_first_pass, context)

    logger.info(f"RBL: итого опубликовано {total} лотов")
    return total


# ── Per-game gallery images ───────────────────────────────────────────────────
# Each key is a lowercase substring of game_cfg["name"].
# Values are lists of local file paths or URLs to prepend/append to lot photos.
# Add your banner images here; missing files are silently skipped.
GAME_GALLERY: dict[str, list[str]] = {
    "raid": [
        # "banners/raid_banner1.jpg",
        # "banners/raid_banner2.jpg",
    ],
    "wow": [
        # "banners/wow_banner1.jpg",
    ],
}


def _build_photo_list(lot_photos: list, game_name: str) -> list:
    """
    Merge per-game gallery banners with the scraped lot photos.

    Strategy:
      - Game banners go FIRST (brand identity before lot screenshots).
      - Then scraped lot photos follow.
      - Only files that actually exist on disk are included (URLs are passed through).
      - Total capped at 2 images (keeps uploads fast).
    """
    gallery_key = next(
        (k for k in GAME_GALLERY if k in game_name.lower()),
        None,
    )
    banners: list[str] = []
    if gallery_key:
        for path in GAME_GALLERY[gallery_key]:
            if path.startswith("http"):
                banners.append(path)
            elif Path(path).exists():
                banners.append(path)
            else:
                logger.debug(f"gallery: файл не найден, пропускаем — {path}")

    combined = banners + (lot_photos or [])
    # Deduplicate preserving order
    seen: set = set()
    result: list = []
    for p in combined:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result[:2]


# Маркеры проданного лота на FunPay
_SOLD_MARKERS = [
    "Offer not found",
    "The offer has expired, been deleted, or never existed",
    "offer has expired",
    "lot-sold", "is-sold", "offer-sold",
    "не существует", "был продан",
    "already sold", "lot not found",
]

_FUNPAY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


async def _funpay_fetch_offer_text(lot_url: str) -> str:
    """
    Устаревший fallback через aiohttp — FunPay рендерит описание через JS,
    поэтому этот метод НЕ видит текст описания. Используй
    _funpay_fetch_offer_text_browser() вместо этого.
    Оставлен только как резервный вариант.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                lot_url,
                headers=_FUNPAY_HEADERS,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"FunPay offer fetch (aiohttp): статус {resp.status} для {lot_url}")
                    return ""
                html = await resp.text(errors="ignore")
                logger.info(f"FunPay offer fetch (aiohttp): загружено {len(html)} символов")
                return html.lower()
    except Exception as e:
        logger.warning(f"FunPay offer fetch (aiohttp): ошибка для {lot_url}: {e}")
        return ""


async def _funpay_fetch_offer_text_browser(context, lot_url: str) -> str:
    """
    Загружает страницу оффера FunPay через Playwright (с куки и JS-рендером).
    FunPay строит описание через JavaScript — aiohttp не видит его текст,
    поэтому используем браузер, у которого уже есть сессия.
    Возвращает весь видимый текст страницы lower-case.
    """
    page = None
    try:
        page = await context.new_page()
        await page.goto(lot_url, wait_until="domcontentloaded", timeout=30000)
        # Ждём появления блока с описанием лота
        try:
            await page.wait_for_selector(
                ".offer-description, .lot-description, .tc-desc, [class*='description']",
                timeout=8000,
            )
        except Exception:
            pass  # блок не найден — берём весь текст страницы
        text = await page.evaluate("() => document.body.innerText")
        logger.info(f"FunPay offer fetch (browser): загружено {len(text or '')} символов из {lot_url}")
        return (text or "").lower()
    except Exception as e:
        logger.warning(f"FunPay offer fetch (browser): ошибка для {lot_url}: {e}")
        return ""
    finally:
        if page:
            await page.close()


async def _funpay_check_sold(session: aiohttp.ClientSession, funpay_id: str) -> bool:
    """
    HTTP-проверка одного лота на FunPay.
    Возвращает True если лот продан/удалён.
    """
    url = f"https://funpay.com/en/lots/offer?id={funpay_id}"
    try:
        async with session.get(
            url,
            headers=_FUNPAY_HEADERS,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            # 404 — лот удалён
            if resp.status == 404:
                return True

            # Редирект не на эту страницу — лот снят
            if f"offer?id={funpay_id}" not in str(resp.url):
                return True

            # Ищем маркеры в теле страницы
            text = await resp.text(errors="ignore")
            text_lower = text.lower()
            for marker in _SOLD_MARKERS:
                if marker.lower() in text_lower:
                    return True

            return False

    except Exception as e:
        logger.warning(f"FunPay check ошибка для {funpay_id}: {e} — считаем активным")
        return False


_FP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

async def _check_lot_http(session: aiohttp.ClientSession, funpay_url: str,
                          retries: int = 3) -> tuple[bool, float]:
    """
    HTTP-проверка лота без браузера — возвращает (sold, offline_hours).
    FunPay рендерит статус продавца на сервере, aiohttp его видит.
    sold=True           → лот продан/удалён
    offline_hours=0.0   → продавец онлайн
    offline_hours=N     → продавец оффлайн N часов
    offline_hours=-1.0  → статус неизвестен (оставляем лот)
    offline_hours=999.0 → месяц/год назад
    """
    import re as _re
    for attempt in range(retries):
        try:
            async with session.get(
                funpay_url,
                headers=_FP_HEADERS,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 429:
                    wait = 60 * (attempt + 1)
                    logger.warning(f"check_lot_http: 429 Rate limit — ждём {wait}с ({attempt+1}/{retries})")
                    await asyncio.sleep(wait)
                    continue
                if resp.status == 404:
                    return True, -1.0
                if "offer?id=" in funpay_url and "offer?id=" not in str(resp.url):
                    return True, -1.0
                html = await resp.text(errors="ignore")

            html_lower = html.lower()

            # Проверка продан
            for marker in ["offer not found", "offer has expired", "been deleted",
                            "never existed", "предложение не найдено"]:
                if marker in html_lower:
                    return True, -1.0

            # Парсим статус продавца из сырого HTML
            m = _re.search(
                r'class="media-user-status[^"]*"[^>]*>\s*([^<]+?)\s*<',
                html, _re.IGNORECASE
            )
            if not m:
                return False, -1.0

            status = m.group(1).strip().lower()

            if "online" in status and "ago" not in status:
                return False, 0.0

            m2 = _re.search(r'(\d+)\s+hour', status)
            if m2:
                return False, float(m2.group(1))
            m2 = _re.search(r'(\d+)\s+week', status)
            if m2:
                return False, float(m2.group(1)) * 24 * 7
            m2 = _re.search(r'(\d+)\s+day', status)
            if m2:
                return False, float(m2.group(1)) * 24
            if "month" in status or "year" in status:
                return False, 999.0

            return False, -1.0

        except Exception as e:
            logger.warning(f"check_lot_http: ошибка {e}")
            return False, -1.0

    logger.warning(f"check_lot_http: исчерпаны попытки для {funpay_url}")
    return False, -1.0


async def run_cleanup(g2g: G2GBot, funpay: FunPayScraper, context=None,
                      offline_hours_threshold: float = 96.0):
    """
    Чистка лотов через HTTP (без браузера):
    - Лот ПРОДАН → удаляем с G2G
    - Продавец ОФФЛАЙН >= offline_hours_threshold ч (по умолчанию 96 ч = 4 дня)
      → удаляем с G2G + убираем из used_lots (бот сможет выложить снова)
    Запросы идут по одному с паузой 2-3с чтобы не получить 429.
    """
    pairs = storage.load_lot_pairs()
    if not pairs:
        logger.info("lot_pairs.json пуст - нечего проверять")
        return

    # ── Шаг 1: собираем все пары ───────────────────────────────────────────
    all_pairs = []
    for game_name, game_pairs in pairs.items():
        for pair in game_pairs:
            all_pairs.append((game_name, pair))

    total = len(all_pairs)
    logger.info(f"{'='*50}")
    logger.info(f"Чистка: {total} лотов (HTTP, по одному, порог оффлайна: {offline_hours_threshold:.0f}ч)")
    logger.info(f"Всего лотов в базе: {total}")
    logger.info(f"{'='*50}")

    # ── Шаг 2: последовательные HTTP-запросы с паузой 2-3с ────────────────
    results_raw: list[tuple[str, dict, bool, float]] = []
    _found_sold    = 0
    _found_offline = 0

    async with aiohttp.ClientSession() as session:
        for checked, (game_name, pair) in enumerate(all_pairs, 1):
            funpay_id = pair.get("funpay_id", "")
            url       = pair.get("funpay_url") or (
                f"https://funpay.com/en/lots/offer?id={funpay_id}" if funpay_id else ""
            )
            if not url:
                results_raw.append((game_name, pair, False, -1.0))
                continue
            sold, hours = await _check_lot_http(session, url)
            results_raw.append((game_name, pair, sold, hours))
            if sold:
                _found_sold += 1
            elif hours != -1.0 and hours >= offline_hours_threshold:
                _found_offline += 1
            if checked % 10 == 0 or checked == total:
                logger.info(
                    f"Прогресс: проверено {checked} из {total} | "
                    f"продано: {_found_sold} | не в сети 4+ дн.: {_found_offline}"
                )
            await asyncio.sleep(random.uniform(2.0, 3.0))

    # ── Раскладываем по корзинам ───────────────────────────────────────────
    to_delete_sold:    list[tuple[str, dict]] = []
    to_delete_offline: list[tuple[str, dict]] = []
    to_keep:           list[tuple[str, dict]] = []

    for game_name, pair, sold, hours in results_raw:
        title = pair.get("title", "")[:50]
        if sold:
            logger.info(f"  ❌ ПРОДАН          | {title}")
            to_delete_sold.append((game_name, pair))
        elif hours != -1.0 and hours >= offline_hours_threshold:
            label = f"{hours/24:.0f} дн." if hours >= 48 else f"{hours:.0f}ч"
            logger.info(f"  ❌ ОФЛАЙН {label:>6}  | {title}")
            to_delete_offline.append((game_name, pair))
        else:
            to_keep.append((game_name, pair))

    print(" " * 80)
    to_delete_all = to_delete_sold + to_delete_offline
    logger.info(f"{'='*50}")
    logger.info(f"Проверено: {total} | Продано: {len(to_delete_sold)} | "
                f"Оффлайн 4+ дн.: {len(to_delete_offline)} | Активных: {len(to_keep)}")
    logger.info(f"{'='*50}")

    if not to_delete_all:
        logger.info("Нет лотов для удаления с G2G")
        return

    # ── Шаг 3: удаляем с G2G ──────────────────────────────────────────────
    logger.info(f"Удаляем {len(to_delete_all)} лотов с G2G...")
    successfully_deleted: set = set()
    offline_g2g_ids = {p["g2g_id"] for _, p in to_delete_offline}

    for idx, (game_name, pair) in enumerate(to_delete_all, 1):
        g2g_id    = pair["g2g_id"]
        funpay_id = pair["funpay_id"]
        reason    = "оффлайн 4+ дн." if g2g_id in offline_g2g_ids else "продан"
        logger.info(f"  [{idx}/{len(to_delete_all)}] Удаляем G2G={g2g_id} ({reason}) FP={funpay_id}")
        deleted = await g2g.delete_lot(g2g_id)
        if deleted:
            successfully_deleted.add(g2g_id)
            logger.info(f"  G2G={g2g_id} удалён OK")
            if g2g_id in offline_g2g_ids and funpay_id:
                storage.remove_used_lot(funpay_id, game_name)
                logger.info(f"  FP={funpay_id} снят с used_lots → бот выложит снова")
        else:
            logger.warning(f"  G2G={g2g_id} не удалось удалить — оставляем в базе")
        await asyncio.sleep(1.5)

    # ── Шаг 4: обновляем lot_pairs.json ────────────────────────────────────
    new_pairs: dict = {}

    for game_name, pair in to_keep:
        new_pairs.setdefault(game_name, []).append(pair)

    for game_name, pair in to_delete_all:
        if pair["g2g_id"] not in successfully_deleted:
            new_pairs.setdefault(game_name, []).append(pair)

    storage._save_atomic(storage.LOT_PAIRS_FILE, new_pairs)

    logger.info(f"{'='*50}")
    logger.info(
        f"Итог: удалено с G2G {len(successfully_deleted)}/{len(to_delete_all)} "
        f"| осталось в базе {sum(len(v) for v in new_pairs.values())}"
    )
    logger.info(f"{'='*50}")


async def run_migrate_verify_ids(g2g: G2GBot):
    """
    Миграция/проверка существующих пар из lot_pairs.json.

    Для каждой пары:
      1. Открывает Manage-страницу G2G
      2. Ищет лот по сохранённому g2g_id (поиск по ID — 100% надёжен)
      3. Если лот найден — отмечает пару как проверенную
      4. Если НЕ найден — значит лот уже удалён/продан, помечает в лог

    Цель: убедиться что все сохранённые g2g_id реально существуют и доступны
    для управления. Не изменяет lot_pairs.json — только диагностика.
    """
    pairs = storage.load_lot_pairs()
    if not pairs:
        logger.info("Миграция: lot_pairs.json пуст — нечего проверять")
        return

    all_pairs = [
        (game_name, pair)
        for game_name, game_pairs in pairs.items()
        for pair in game_pairs
    ]
    total = len(all_pairs)
    logger.info(f"{'='*55}")
    logger.info(f"Миграция/проверка: всего пар в базе: {total}")
    logger.info(f"{'='*55}")

    page = await g2g._get_page()

    # Открываем Manage один раз
    try:
        await page.goto(
            "https://www.g2g.com/offers/list?cat_id=5830014a-b974-45c6-9672-b51e83112fb7&status=live",
            wait_until="domcontentloaded",
            timeout=40000,
        )
        await asyncio.sleep(3)
        await page.wait_for_selector(
            "input[placeholder='Search title or offer number']",
            timeout=15000,
        )
    except Exception as e:
        logger.error(f"Миграция: не удалось открыть Manage-страницу: {e}")
        return

    ok_count = 0
    missing_count = 0
    missing_pairs = []

    for idx, (game_name, pair) in enumerate(all_pairs, 1):
        g2g_id    = pair.get("g2g_id", "")
        funpay_id = pair.get("funpay_id", "")
        title     = pair.get("title", "")[:50]

        if not g2g_id:
            logger.warning(f"  [{idx}/{total}] FP={funpay_id}: g2g_id ПУСТОЙ — пропускаем")
            missing_count += 1
            missing_pairs.append((game_name, pair))
            continue

        logger.info(f"  [{idx}/{total}] Проверяем G2G={g2g_id} FP={funpay_id}")

        try:
            search_input = await page.query_selector(
                "input[placeholder='Search title or offer number']"
            )
            if not search_input:
                logger.warning(f"  [{idx}/{total}] Поле поиска не найдено — перезагружаем страницу")
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
                search_input = await page.query_selector(
                    "input[placeholder='Search title or offer number']"
                )

            if search_input:
                # Поиск по g2g_id — это ЕДИНСТВЕННО надёжный способ
                await search_input.click()
                await page.keyboard.press("Control+a")
                await search_input.fill(g2g_id)
                await asyncio.sleep(3)

                # Проверяем есть ли строка в таблице
                found_id = await page.evaluate("""
                    (gid) => {
                        const rows = document.querySelectorAll('tbody tr');
                        for (const row of rows) {
                            const idEl = row.querySelector('.text-font-2nd.text-body2');
                            if (idEl) {
                                const txt = idEl.innerText.trim().replace('#', '');
                                if (txt === gid) return true;
                            }
                        }
                        return false;
                    }
                """, g2g_id)

                if found_id:
                    logger.info(f"  [{idx}/{total}] ✓ G2G={g2g_id} НАЙДЕН")
                    ok_count += 1
                else:
                    logger.warning(f"  [{idx}/{total}] ✗ G2G={g2g_id} НЕ НАЙДЕН (продан/удалён?) | title: {title}")
                    missing_count += 1
                    missing_pairs.append((game_name, pair))

        except Exception as e:
            logger.error(f"  [{idx}/{total}] Ошибка проверки G2G={g2g_id}: {e}")

        await asyncio.sleep(1.5)  # не спамим G2G

    logger.info(f"{'='*55}")
    logger.info(f"Миграция завершена: ✓ найдено {ok_count} | ✗ не найдено {missing_count}")
    if missing_pairs:
        logger.info("Пары не найденные на G2G (продано или ID потерян):")
        for game_name, pair in missing_pairs:
            logger.info(
                f"  [{game_name}] FP={pair.get('funpay_id')} "
                f"G2G={pair.get('g2g_id')} | {pair.get('title','')[:60]}"
            )
    logger.info(f"{'='*55}")


async def run_price_check_menu(context, games):
    """
    Суб-меню выбора игры для проверки цен.
    Показывает только те игры, для которых есть price_tiers в конфиге.
    """
    # Фильтруем игры у которых есть tiers (иначе проверка не имеет смысла)
    eligible = [g for g in games if g.get("price_tiers")]
    if not eligible:
        logger.warning("price_check: ни одна игра не имеет price_tiers в конфиге")
        return

    print(f"\n{'='*40}")
    print(f"  Проверка цен")
    print(f"{'='*40}")
    for i, g in enumerate(eligible, 1):
        print(f"  {i}. {g['name']}")
    print(f"  0. Все игры")
    print(f"{'='*40}")

    valid_single = [str(i) for i in range(len(eligible) + 1)]
    while True:
        sub = input("Выбери игру (можно несколько через запятую, например 1,3,5): ").strip()
        # Поддержка списка: "1,3,5" — несколько игр
        if "," in sub:
            try:
                idxs = [int(x.strip()) for x in sub.split(",") if x.strip()]
                if all(1 <= i <= len(eligible) for i in idxs):
                    to_run = [eligible[i - 1] for i in idxs]
                    break
            except ValueError:
                pass
            print("Неверный формат, попробуй снова")
            continue
        # Одиночный выбор
        if sub in valid_single:
            to_run = eligible if sub == "0" else [eligible[int(sub) - 1]]
            break
        print("Неверный выбор, попробуй снова")

    for game_cfg in to_run:
        tiers     = game_cfg.get("price_tiers", [])
        game_name = game_cfg["name"]
        fp_factor = game_cfg.get("funpay_price_factor", 1.0)
        logger.info(f"price_check: запуск для [{game_name}] (fp_factor={fp_factor})")

        if "raid" in game_name.lower():
            await run_price_check_raid(context, tiers, fp_factor=fp_factor)
        elif "wow" in game_name.lower():
            await run_price_check_wow(context, tiers, fp_factor=fp_factor)
        else:
            # Универсальный вариант для любой другой игры
            lots_url = game_cfg.get("funpay_url", "")
            await run_price_check(context, tiers, lots_url=lots_url, game_name=game_name, fp_factor=fp_factor)

        logger.info(f"price_check: [{game_name}] завершено")

    logger.info("Проверка цен завершена!")


async def main():
    config = load_config()
    games  = [g for g in config["games"] if g.get("enabled", True)]

    # Миграция старых used_lots файлов (однократно)
    storage.migrate_legacy_used_lots()

    bot_proxy    = parse_proxy(config.get("bot_proxy", ""))
    imgur_proxy  = config.get("imgur_proxy", "")
    global _claude_api_key
    _claude_api_key = config.get("claude_api_key", "")
    if _claude_api_key:
        logger.info("AI brief: Claude API key загружен")

    if bot_proxy:
        logger.info(f"Bot proxy: {config.get('bot_proxy', '')}")
    if imgur_proxy:
        logger.info(f"Imgur proxy: {imgur_proxy}")

    choice = show_menu(games)

    async with async_playwright() as pw:
        launch_kwargs = {
            "user_data_dir": PROFILE_DIR,
            "headless": config.get("headless", False),
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1366, "height": 768},
        }

        if bot_proxy:
            launch_kwargs["proxy"] = bot_proxy

        context = await pw.chromium.launch_persistent_context(**launch_kwargs)

        funpay = FunPayScraper(context, imgur_proxy=imgur_proxy)
        g2g    = G2GBot(context)

        if choice == "0":
            logger.info("Запускаем чистку лотов...")
            await run_cleanup(g2g, funpay, context=context)
            logger.info("Чистка завершена!")
            input("Нажми Enter чтобы закрыть...")

        elif choice == "m":
            logger.info("Запускаем миграцию/проверку ID лотов...")
            await run_migrate_verify_ids(g2g)
            logger.info("Миграция завершена!")
            input("Нажми Enter чтобы закрыть...")

        elif choice == "p":
            logger.info("Запускаем проверку цен...")
            await run_price_check_menu(context, games)
            input("Нажми Enter чтобы закрыть...")

        else:
            game_cfg = games[int(choice) - 1]
            logger.info(f"Запускаем бота для игры: {game_cfg['name']}")
            logger.info(f"title_blacklist: {game_cfg.get('title_blacklist', [])}")
            logger.info(f"blacklist_whitelist: {game_cfg.get('blacklist_whitelist', [])}")
            logger.info(f"seller_blacklist: {game_cfg.get('seller_blacklist', [])}")
            logger.info(f"builtin_blacklist: {len(game_cfg.get('builtin_blacklist', []))} слов")

            iteration = 0
            while True:
                iteration += 1
                logger.info(f"\n{'#'*55}")
                logger.info(f"  Итерация #{iteration} | {datetime.now():%Y-%m-%d %H:%M:%S}")
                logger.info(f"{'#'*55}")

                try:
                    if game_cfg.get("roblox_games"):
                        created = await run_roblox_pass(
                            funpay=funpay,
                            g2g=g2g,
                            roblox_cfg=game_cfg,
                            is_first_pass=(iteration == 1),
                            context=context,
                        )
                    else:
                        created = await run_pass(
                            funpay=funpay,
                            g2g=g2g,
                            game_cfg=game_cfg,
                            is_first_pass=(iteration == 1),
                            context=context,
                        )
                    logger.info(f"\nИтог итерации #{iteration}: создано {created} лотов")

                except Exception as e:
                    logger.error(f"Ошибка итерации: {e}", exc_info=True)

                logger.info(f"Следующий цикл через {game_cfg['loop_interval_seconds']} сек...")
                await asyncio.sleep(game_cfg["loop_interval_seconds"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")