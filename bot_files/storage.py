"""
storage.py — единое хранилище для used_lots и lot_pairs
════════════════════════════════════════════════════════════════════════════════

Файлы:
  used_lots_all.json  — использованные ID, разбитые по играм
  lot_pairs.json      — пары funpay↔g2g, разбитые по играм (формат не меняется)

Структура used_lots_all.json:
  {
    "Raid: Shadow Legends":                          ["id1", "id2", ...],
    "WOW Classic Era / Seasonal / TBC Anniversary": ["id3", ...],
    "Zenless Zone Zero":                             ["id4", ...]
  }

Миграция (однократно, при первом запуске):
  used_lots.json                              → Raid: Shadow Legends
  used_lots_wow_classic_era_*.json            → WOW Classic Era / ...
  used_lots_zenless_zone_zero.json            → Zenless Zone Zero
"""

from __future__ import annotations

import json
import logging
import re
import os
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Файлы ────────────────────────────────────────────────────────────────────
USED_LOTS_FILE = Path("used_lots_all.json")
LOT_PAIRS_FILE = Path("lot_pairs.json")

# ── Маппинг: старые файлы → имя игры ────────────────────────────────────────
_LEGACY_FILES: dict[str, str] = {
    "used_lots.json": "Raid: Shadow Legends",
    "used_lots_wow_classic_era___seasonal___tbc_anniversary.json":
        "WOW Classic Era / Seasonal / TBC Anniversary",
    "used_lots_zenless_zone_zero.json": "Zenless Zone Zero",
}

# ── Атомарная запись ─────────────────────────────────────────────────────────

def _save_atomic(path: Path, data: dict) -> None:
    """Пишем во временный файл → rename. Никогда не портим данные."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.error(f"storage: ошибка записи {path}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ════════════════════════════════════════════════════════════════════════════════
# USED LOTS
# ════════════════════════════════════════════════════════════════════════════════

def _load_used_lots_raw() -> dict[str, list]:
    """Читаем used_lots_all.json. Если нет — возвращаем пустой dict."""
    if not USED_LOTS_FILE.exists():
        return {}
    try:
        data = json.loads(USED_LOTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("storage: used_lots_all.json не является dict — сбрасываем")
        return {}
    except Exception as e:
        logger.warning(f"storage: ошибка чтения {USED_LOTS_FILE}: {e}")
        return {}


def migrate_legacy_used_lots() -> None:
    """
    Однократная миграция: читаем старые плоские файлы → пишем в used_lots_all.json.
    Старые файлы НЕ удаляем (на случай отката), просто читаем.
    Если used_lots_all.json уже существует — пропускаем.
    """
    if USED_LOTS_FILE.exists():
        return  # уже мигрировали

    logger.info("storage: миграция старых used_lots файлов...")
    merged: dict[str, list] = {}

    for filename, game_name in _LEGACY_FILES.items():
        p = Path(filename)
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                ids = [str(x) for x in raw]
            elif isinstance(raw, dict):
                # уже категоризированный — берём flat список всех ID
                ids = [str(x) for vals in raw.values() for x in vals]
            else:
                ids = []
            merged[game_name] = list(dict.fromkeys(ids))  # дедупликация
            logger.info(f"storage: мигрировано {len(ids)} ID из {filename} → {game_name!r}")
        except Exception as e:
            logger.warning(f"storage: ошибка миграции {filename}: {e}")

    _save_atomic(USED_LOTS_FILE, merged)
    logger.info(f"storage: миграция завершена → {USED_LOTS_FILE}")


def load_used_lots(game_name: str) -> set:
    """
    Загрузить использованные ID для конкретной игры.
    Возвращает set строк.
    """
    data = _load_used_lots_raw()
    return set(data.get(game_name, []))


def save_used_lot(lot_id: str, game_name: str) -> None:
    """Добавить один ID в категорию игры."""
    data = _load_used_lots_raw()
    existing = data.get(game_name, [])
    if str(lot_id) not in existing:
        existing.append(str(lot_id))
        data[game_name] = existing
        _save_atomic(USED_LOTS_FILE, data)


def save_used_lots_bulk(lot_ids: set | list, game_name: str) -> None:
    """Добавить сразу несколько ID (например при синхронизации)."""
    data = _load_used_lots_raw()
    existing = set(data.get(game_name, []))
    existing.update(str(x) for x in lot_ids)
    data[game_name] = list(existing)
    _save_atomic(USED_LOTS_FILE, data)


def remove_used_lot(lot_id: str, game_name: str) -> None:
    """Удалить ID из категории (если вдруг нужно откатить)."""
    data = _load_used_lots_raw()
    before = data.get(game_name, [])
    after = [x for x in before if x != str(lot_id)]
    if len(after) != len(before):
        data[game_name] = after
        _save_atomic(USED_LOTS_FILE, data)


def get_all_used_ids() -> set:
    """Все использованные ID по всем играм (для глобальной дедупликации)."""
    data = _load_used_lots_raw()
    return {str(x) for ids in data.values() for x in ids}


# ════════════════════════════════════════════════════════════════════════════════
# LOT PAIRS  (формат уже правильный — dict по именам игр)
# ════════════════════════════════════════════════════════════════════════════════

def load_lot_pairs(game_name: Optional[str] = None) -> dict | list:
    """
    Загрузить пары funpay↔g2g.
    game_name=None  → вернуть весь dict {game: [pairs]}
    game_name=str   → вернуть list пар только для этой игры
    """
    if not LOT_PAIRS_FILE.exists():
        return {} if game_name is None else []
    try:
        data = json.loads(LOT_PAIRS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("storage: lot_pairs.json не является dict")
            return {} if game_name is None else []
        if game_name is None:
            return data
        return data.get(game_name, [])
    except Exception as e:
        logger.warning(f"storage: ошибка чтения lot_pairs.json: {e}")
        return {} if game_name is None else []


def save_lot_pair(
    funpay_id: str,
    g2g_id: str,
    title: str,
    funpay_url: str,
    game_name: str,
    funpay_price: float = 0.0,
    g2g_price: float = 0.0,
) -> None:
    """Сохранить новую пару в нужную категорию."""
    data = load_lot_pairs()
    if not isinstance(data, dict):
        data = {}
    pairs = data.get(game_name, [])
    pairs.append({
        "funpay_id":    str(funpay_id),
        "g2g_id":       str(g2g_id),
        "title":        title,
        "funpay_url":   funpay_url,
        "funpay_price": round(funpay_price, 2),
        "g2g_price":    round(g2g_price, 2),
    })
    data[game_name] = pairs
    _save_atomic(LOT_PAIRS_FILE, data)
    logger.info(
        f"storage: пара сохранена [{game_name}] "
        f"FP={funpay_id} G2G={g2g_id} fp=${funpay_price:.2f} g2g=${g2g_price:.2f}"
    )


def remove_lot_pair(g2g_id: str, game_name: Optional[str] = None) -> bool:
    """
    Удалить пару по g2g_id.
    game_name=None → искать во всех категориях.
    Возвращает True если удалено.
    """
    data = load_lot_pairs()
    if not isinstance(data, dict):
        return False

    found = False
    games = [game_name] if game_name else list(data.keys())
    for gname in games:
        before = data.get(gname, [])
        after = [p for p in before if p.get("g2g_id") != g2g_id]
        if len(after) != len(before):
            data[gname] = after
            found = True

    if found:
        _save_atomic(LOT_PAIRS_FILE, data)
        logger.info(f"storage: пара удалена G2G={g2g_id}")
    return found


def clear_game_pairs(game_name: str) -> None:
    """Очистить все пары для конкретной игры."""
    data = load_lot_pairs()
    if not isinstance(data, dict):
        return
    if game_name in data:
        data[game_name] = []
        _save_atomic(LOT_PAIRS_FILE, data)
        logger.info(f"storage: пары очищены для [{game_name}]")


def get_all_g2g_ids() -> set:
    """Все g2g_id по всем играм (для глобальной дедупликации)."""
    data = load_lot_pairs()
    if not isinstance(data, dict):
        return set()
    return {
        p.get("g2g_id", "")
        for pairs in data.values()
        for p in pairs
        if p.get("g2g_id")
    }


def get_all_funpay_ids() -> set:
    """Все funpay_id из lot_pairs по всем играм."""
    data = load_lot_pairs()
    if not isinstance(data, dict):
        return set()
    return {
        p.get("funpay_id", "")
        for pairs in data.values()
        for p in pairs
        if p.get("funpay_id")
    }


# ════════════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ════════════════════════════════════════════════════════════════════════════════

def print_stats() -> None:
    """Вывести статистику хранилища в лог."""
    used = _load_used_lots_raw()
    pairs = load_lot_pairs()

    logger.info("═" * 55)
    logger.info("  Статистика хранилища")
    logger.info("═" * 55)

    logger.info("  used_lots_all.json:")
    for game, ids in used.items():
        logger.info(f"    [{game}]: {len(ids)} ID")

    logger.info("  lot_pairs.json:")
    if isinstance(pairs, dict):
        for game, ps in pairs.items():
            logger.info(f"    [{game}]: {len(ps)} пар")
    logger.info("═" * 55)


# ════════════════════════════════════════════════════════════════════════════════
# МИГРАЦИЯ — запусти один раз: python storage.py
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info("Запуск миграции used_lots файлов...")
    migrate_legacy_used_lots()
    print_stats()
    logger.info("Готово!")
