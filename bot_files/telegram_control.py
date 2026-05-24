"""
telegram_control.py — управление G2G Bot через Telegram.

Установка:
    pip install pyTelegramBotAPI

Запуск:
    python telegram_control.py
"""

import subprocess
import sys
import json
import time
import threading
import logging
import os
import re
import functools
from pathlib import Path

import telebot
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN   = "8807961699:AAHOis26f7FFVaVmh8myIZc5t7DhqaRp46o"
ALLOWED_ID  = 444942538
MAIN_SCRIPT = "main.py"
LOG_DIR     = Path("logs")
CONFIG_PATH = Path("config.json")
LOT_PAIRS   = Path("lot_pairs.json")
STATS_PATH  = Path("stats.json")
# ====================================================

bot = telebot.TeleBot(BOT_TOKEN)
logger = logging.getLogger("tg_control")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# БАГ #1 ИСПРАВЛЕН: thread-safe доступ к bot_process через Lock
_process_lock = threading.Lock()
bot_process   = None
log_streaming = False
_selected     = {}   # chat_id → set индексов
_mode         = {}   # chat_id → "run" | "delete" | "check" | "prices"

# Мультиигровой режим
_multi_games  = {}   # chat_id → [idx1, idx2, ...] список выбранных игр
_multi_pos    = {}   # chat_id → текущая позиция в списке
_multi_chat   = 0    # chat_id активного мультизапуска
_no_lots_count: dict = {}  # chat_id → счётчик пустых итераций подряд
NO_LOTS_LIMIT  = 1   # сразу переключаем игру при первом "нет лотов"
_intentional_stop = False  # True если остановлен вручную, False если крэш
_switching_to_next = False  # True если переключаемся на следующую игру в мультирежиме


# ════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════

def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"config.json: {e}")
        return {"games": []}


def load_pairs():
    try:
        if LOT_PAIRS.exists():
            return json.loads(LOT_PAIRS.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def init_stats_from_pairs():
    """При старте: чистит stats.json от записей старше 7 дней."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        stats = {}
        if STATS_PATH.exists():
            stats = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        old_keys = [d for d in stats if d < cutoff]
        for k in old_keys:
            del stats[k]
        if old_keys:
            logger.info(f"stats: удалено {len(old_keys)} устаревших дней")
        STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"stats: ошибка инициализации: {e}")


def get_games():
    return [g for g in load_config().get("games", []) if g.get("enabled", True)]


def is_running():
    with _process_lock:
        return bot_process is not None and bot_process.poll() is None


def decode_line(raw):
    for enc in ("utf-8", "cp1251", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def safe_send(chat_id, text, **kwargs):
    """Отправка с 3 попытками при ConnectTimeout."""
    for attempt in range(3):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except Exception as e:
            if attempt < 2:
                logger.warning(f"send_message попытка {attempt+1}/3: {e}")
                time.sleep(3)
            else:
                logger.error(f"send_message не удалось после 3 попыток: {e}")
    return None


def safe_edit(text, chat_id, msg_id, **kwargs):
    """Редактирование сообщения с 3 попытками при ошибке."""
    for attempt in range(3):
        try:
            return bot.edit_message_text(text, chat_id, msg_id, **kwargs)
        except Exception as e:
            if attempt < 2:
                logger.warning(f"edit_message попытка {attempt+1}/3: {e}")
                time.sleep(3)
            else:
                logger.error(f"edit_message не удалось после 3 попыток: {e}")
    return None


# ════════════════════════════════════════════════
# Клавиатуры
# ════════════════════════════════════════════════

def _send_lot_pairs_file(chat_id):
    """Отправляет lot_pairs.json как файл в Telegram."""
    try:
        if LOT_PAIRS.exists():
            pairs = load_pairs()
            total = sum(len(v) for v in pairs.values())
            with open(LOT_PAIRS, "rb") as f:
                bot.send_document(chat_id, f, caption=f"📋 База лотов: {total} шт.")
        else:
            safe_send(chat_id, "📋 База лотов пуста")
    except Exception as e:
        logger.warning(f"lot_pairs send: {e}")


def main_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    if is_running():
        kb.row(KeyboardButton("⛔ Остановить бота"))
    else:
        kb.row(KeyboardButton("🚀 Запустить все"))
        kb.row(KeyboardButton("🎮 Выбрать игры"))
    kb.row(KeyboardButton("📋 Логи"),           KeyboardButton("📊 Статус"))
    kb.row(KeyboardButton("🔍 Проверить базу"), KeyboardButton("🗑 Удалить лоты"))
    kb.row(KeyboardButton("💰 Проверить цены"), KeyboardButton("📄 База лотов"))
    return kb


def games_keyboard(selected=None, mode="run"):
    games = get_games()
    kb    = InlineKeyboardMarkup(row_width=1)
    sel   = selected or set()

    for i, g in enumerate(games):
        mark = "✅" if i in sel else "⬜"
        kb.add(InlineKeyboardButton(
            f"{mark} {g['name']}",
            callback_data=f"tog.{i}.{mode}",   # БАГ #2 ИСПРАВЛЕН: точка вместо _ чтобы не ломать split
        ))

    if mode == "run":
        kb.add(InlineKeyboardButton("▶️ Запустить выбранные",     callback_data="start_selected"))
    elif mode == "delete":
        kb.add(InlineKeyboardButton("🗑 Удалить лоты выбранных",  callback_data="delete_selected"))
    elif mode == "check":
        kb.add(InlineKeyboardButton("🔍 Проверить выбранные",     callback_data="check_selected"))
        kb.add(InlineKeyboardButton("✅ Проверить все",            callback_data="check_all"))
    elif mode == "prices":
        kb.add(InlineKeyboardButton("💰 Проверить выбранную",     callback_data="prices_selected"))
        kb.add(InlineKeyboardButton("💰 Проверить все игры",      callback_data="prices_all"))  # БАГ #3 ИСПРАВЛЕН: добавлена кнопка "все"

    kb.add(InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    return kb


# ════════════════════════════════════════════════
# Запуск / остановка
# ════════════════════════════════════════════════

def start_bot_process(choice, chat_id, label="", is_cleanup=False, extra_inputs=None):
    global bot_process, log_streaming

    if is_running():
        safe_send(chat_id, "⚠️ Бот уже работает!")
        return

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"

    try:
        with _process_lock:
            bot_process = subprocess.Popen(
                [sys.executable, MAIN_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                env=env,
                cwd=str(Path.cwd()),
            )
            bot_process.stdin.write(f"{choice}\n".encode("utf-8"))
            for extra in (extra_inputs or []):
                bot_process.stdin.write(f"{extra}\n".encode("utf-8"))
            bot_process.stdin.flush()
    except Exception as e:
        safe_send(chat_id, f"❌ Ошибка запуска: {e}", reply_markup=main_keyboard())
        return

    safe_send(
        chat_id,
        f"▶️ Запущено: {label or choice}\nДля остановки нажми кнопку ниже 👇",
        reply_markup=main_keyboard(),
    )

    log_streaming = True
    threading.Thread(
        target=stream_logs,
        args=(chat_id, choice, is_cleanup),
        daemon=True,
    ).start()


def stop_bot():
    global bot_process, log_streaming, _multi_games, _multi_pos, _multi_chat, _intentional_stop
    log_streaming = False
    _intentional_stop = True
    # Очищаем мультирежим — чтобы после остановки не запускалась следующая игра
    _multi_games.clear()
    _multi_pos.clear()
    _multi_chat = 0
    _no_lots_count.clear()
    with _process_lock:
        if bot_process and bot_process.poll() is None:
            bot_process.terminate()
            try:
                # БАГ #4 ИСПРАВЛЕН: ловим правильный тип исключения
                bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bot_process.kill()
                bot_process.wait()
        bot_process = None


# ════════════════════════════════════════════════
# Стриминг логов
# ════════════════════════════════════════════════

# Важные события — шлём СРАЗУ
_INSTANT_MAP = {
    "итерация #":                  ("🔁", None),
    "лот опубликован ok":          ("✅", None),
    "пара сохранена":              ("💾", None),
    "следующий цикл через":        ("⏱",  None),
    "ошибка публикации":           ("❌", None),
    "ошибка итерации":             ("❌", None),
    "[error]":                     ("⚠️", None),
    "подходящих лотов не найдено": ("😶", "Подходящих лотов нет"),
    "лот не найден - пропускаем":  ("😶", "Подходящих лотов нет"),
}

# Обычные события — буфер по 3
_STATUS_MAP = {
    "funpay: лот ->":              ("📦", None),
    "funpay: найдено карточек":    ("🔍", None),
    "заполняем title":             ("✏️", "Заполняем форму G2G..."),
    "нажимаем publish":            ("🚀", "Публикуем лот..."),
    "manage offer нажат":          ("🔗", "Нажат Manage offer"),
    "удаляем лот":                 ("🗑", None),
    "удалён ok":                   ("✅", None),
    "пропускаем лот":              ("⏭️", None),
    "blacklist: стоп":             ("🚫", None),
}


# БАГ #6 ИСПРАВЛЕН: убрали "    " (4 пробела) — слишком широкий фильтр
_SKIP_CONTAINS = (
    "charmap", "codec can't encode", "codec can't decode",
    "unicodeencodeerror", "unicodedecodeerror",
    "httpx:", "apscheduler", "telegram.ext",
)
_SKIP_PREFIXES = (
    "traceback (most recent", "during handling of the above",
    "the above exception was", "--- logging error ---",
)


def _make_from_map(mapping, line_lower, line_raw):
    for kw, (emoji, label) in mapping.items():
        if kw in line_lower:
            if label:
                return f"{emoji} {label}"
            short = line_raw
            for sep in ("] ", "]: ", ": "):
                if sep in short:
                    short = short.split(sep, 1)[-1]
            # Убираем технические префиксы типа "G2G: ", "FunPay: "
            _tech_prefixes = ("g2g: ", "funpay: ", "main: ", "g2g bot: ")
            short_low = short.lower()
            for pref in _tech_prefixes:
                if short_low.startswith(pref):
                    short = short[len(pref):]
                    break
            if len(short) < 4:
                return None
            return f"{emoji} {short[:160]}"
    return None


def _next_game(chat_id) -> tuple:
    """
    Возвращает (choice, name) следующей игры в мультирежиме.
    Циклически переходит по списку _multi_games[chat_id].
    Если игр не осталось — возвращает (None, None).
    """
    games = _multi_games.get(chat_id, [])
    if not games:
        return None, None

    # Текущая позиция → следующая (по кругу)
    pos = _multi_pos.get(chat_id, 0)
    next_pos = (pos + 1) % len(games)
    _multi_pos[chat_id] = next_pos

    idx = games[next_pos]
    all_games = get_games()
    if idx >= len(all_games):
        return None, None

    game_cfg  = all_games[idx]
    choice    = str(idx + 1)   # номер для stdin main.py (1-based)
    name      = game_cfg["name"]
    return choice, name


def _restart_for_game(chat_id, choice, name):
    """Останавливаем текущий процесс и запускаем следующую игру."""
    global bot_process, log_streaming, _intentional_stop, _multi_chat
    time.sleep(1)
    # Сохраняем список игр ДО стопа — stop_bot() очищает _multi_games
    saved_games = list(_multi_games.get(chat_id, []))
    saved_pos   = _multi_pos.get(chat_id, 0)
    if not saved_games:
        logger.info(f"_restart_for_game: мультирежим очищен — отмена запуска {name}")
        return
    _intentional_stop = True
    stop_bot()
    time.sleep(1)
    # Восстанавливаем мультирежим после стопа (stop_bot его очистил)
    _multi_games[chat_id] = saved_games
    _multi_pos[chat_id]   = saved_pos
    _multi_chat = chat_id
    start_bot_process(choice, chat_id, label=name)


def stream_logs(chat_id, choice, is_cleanup=False):
    global bot_process, log_streaming, _intentional_stop, _switching_to_next

    is_price_check = (choice == "p")
    buffer = []
    _pc_done_sent = False  # защита от двойной отправки lot_pairs

    # Переменные для отчёта о ценах
    price_updates      = []   # список изменённых лотов
    price_to_update    = 0    # сколько лотов нужно обновить
    price_games_done   = []   # завершённые игры
    price_current_lot  = ""   # текущий проверяемый лот
    pc_total           = 0    # сколько всего лотов в текущей игре
    pc_checked         = 0    # проверено
    pc_skipped         = 0    # пропущено (мелкая разница)
    pc_to_update_cnt   = 0    # нужно обновить
    pc_updated         = 0    # реально обновлено на G2G
    pc_not_found       = 0    # цена не найдена
    pc_game            = ""   # текущая игра

    # Счётчики чистки
    total_lots      = 0
    checked_count   = 0
    sold_count      = 0
    offline_count   = 0
    deleted_count   = 0
    _sent_gids      = set()   # дедупликация: уже отправленные ID лотов
    _last_progress  = None    # дедупликация: последний кортеж (checked, sold, offline, deleted)

    def flush():
        if buffer:
            try:
                safe_send(chat_id, "\n".join(buffer))
            except Exception as e:
                logger.warning(f"send: {e}")
            buffer.clear()

    def send_progress(force=False):
        nonlocal _last_progress
        n = total_lots if total_lots > 0 else "?"
        if not force and checked_count % 10 != 0 and checked_count != total_lots:
            return
        state = (checked_count, sold_count, offline_count, deleted_count)
        if state == _last_progress:
            return
        _last_progress = state
        pct = f" ({checked_count * 100 // total_lots}%)" if total_lots > 0 else ""
        text = (
            f"🔄 Проверено пар: {checked_count} из {n}{pct}\n"
            f"🛒 Продано на FunPay: {sold_count}\n"
            f"😴 Не в сети 2+ дн.: {offline_count}\n"
            f"🗑 Удалено с G2G: {deleted_count}"
        )
        try:
            safe_send(chat_id, text)
        except Exception as e:
            logger.warning(f"send_progress: {e}")

    try:
        with _process_lock:
            proc = bot_process
        if proc is None:
            return

        for raw in proc.stdout:
            if not log_streaming:
                break

            line_raw   = decode_line(raw).strip()
            if not line_raw:
                continue
            line_lower = line_raw.lower()

            if any(s in line_lower for s in _SKIP_CONTAINS):
                continue
            if any(line_lower.startswith(p) for p in _SKIP_PREFIXES):
                continue

            # ── Режим чистки ─────────────────────────────────────────────
            if is_cleanup:
                if "всего лотов в базе" in line_lower:
                    # Берём ПОСЛЕДНЕЕ число в строке — это кол-во лотов, не год из даты
                    nums = re.findall(r"\d+", line_raw)
                    if nums:
                        total_lots = int(nums[-1])
                    try:
                        safe_send(chat_id,
                            f"🔍 Начинаю проверку {total_lots} пар FunPay↔G2G\n"
                            f"Проданные лоты будут удалены с G2G")
                    except Exception:
                        pass

                elif "прогресс: проверено" in line_lower:
                    try:
                        m_p = re.search(r"проверено\s+(\d+)\s+из\s+(\d+)", line_lower)
                        m_s = re.search(r"продано:\s*(\d+)", line_lower)
                        m_o = re.search(r"не в сети 2\+[^:]*:\s*(\d+)", line_lower)
                        if m_p:
                            checked_count = int(m_p.group(1))
                            total_lots    = int(m_p.group(2))
                        if m_s:
                            sold_count    = int(m_s.group(1))
                        if m_o:
                            offline_count = int(m_o.group(1))
                        send_progress()
                    except Exception as _pe:
                        logger.warning(f"progress send error: {_pe}")

                elif "удаляем g2g=" in line_lower or "удаляем лот" in line_lower:
                    m = re.search(r"g2g=(\S+)", line_lower) or \
                        re.search(r"удаляем лот\s+(\S+)", line_lower)
                    gid = m.group(1).upper().rstrip(".") if m else None
                    if gid and gid not in _sent_gids:
                        _sent_gids.add(gid)
                        try:
                            safe_send(chat_id, f"🛒 Лот продан/снят\nG2G: {gid}\nУдаляю с G2G...")
                        except Exception:
                            pass

                elif "удалён ok" in line_lower:
                    deleted_count += 1
                    send_progress(force=True)

                elif "итог:" in line_lower and "удалено с g2g" in line_lower:
                    m_d = re.search(r"удалено с g2g\s*(\d+)/(\d+)", line_lower)
                    m_b = re.search(r"осталось в базе[:\s]+(\d+)", line_lower)
                    d_str = f"{m_d.group(1)}/{m_d.group(2)}" if m_d else str(deleted_count)
                    b_str = m_b.group(1) if m_b else "?"
                    try:
                        safe_send(
                            chat_id,
                            f"✅ Проверка базы завершена\n"
                            f"📊 Проверено: {checked_count} из {total_lots} лотов\n"
                            f"🗑 Удалено с G2G: {d_str}\n"
                            f"📋 Осталось в базе: {b_str}",
                        )
                        _send_lot_pairs_file(chat_id)
                    except Exception:
                        pass

                elif any(k in line_lower for k in ("ошибка", "error")):
                    try:
                        safe_send(chat_id, f"⚠️ {line_raw[:150]}")
                    except Exception:
                        pass
                continue

            # ── Режим проверки цен ────────────────────────────────────────
            if is_price_check:
                # Старт новой игры — сбрасываем счётчики и сообщаем
                m_start = re.search(r"запуск для \[([^\]]+)\]", line_lower)
                if m_start:
                    pc_game = m_start.group(1)
                    pc_total = pc_checked = pc_skipped = pc_to_update_cnt = 0
                    pc_updated = pc_not_found = 0
                    try:
                        safe_send(chat_id, f"💰 Запуск: {pc_game}")
                    except Exception:
                        pass
                    continue

                # Сколько всего лотов
                m_total = re.search(r"(\d+)\s+лотов\s*\|\s*порог", line_lower)
                if m_total:
                    pc_total = int(m_total.group(1))
                    try:
                        safe_send(chat_id, f"🔍 Проверяем {pc_total} лотов...")
                    except Exception:
                        pass
                    continue

                # Каждый лот: "[N/M] ..."
                if re.search(r"\[\d+/\d+\]", line_lower):
                    pc_checked += 1
                    if pc_total > 0 and (pc_checked % 10 == 0 or pc_checked == pc_total):
                        try:
                            safe_send(chat_id,
                                f"📊 [{pc_game}] {pc_checked}/{pc_total}\n"
                                f"  ✅ Пропущено: {pc_skipped}\n"
                                f"  ✏️ К обновлению: {pc_to_update_cnt}\n"
                                f"  ⚠️ Не найдено: {pc_not_found}")
                        except Exception:
                            pass
                    continue

                if "ok изменение <" in line_lower:
                    pc_skipped += 1
                    continue
                if "нужно обновить:" in line_lower:
                    pc_to_update_cnt += 1
                    continue
                if "цена не найдена" in line_lower:
                    pc_not_found += 1
                    continue
                if "цена успешно обновлена" in line_lower:
                    pc_updated += 1
                    continue

                # Итог игры
                if "готово | обновлено" in line_lower:
                    try:
                        safe_send(chat_id,
                            f"✅ Готово: {pc_game}\n"
                            f"  Проверено: {pc_checked}/{pc_total}\n"
                            f"  Обновлено: {pc_updated}\n"
                            f"  Пропущено: {pc_skipped}\n"
                            f"  Не найдено: {pc_not_found}")
                    except Exception:
                        pass
                    continue

                # Завершение всех игр
                if "проверка цен завершена" in line_lower and not _pc_done_sent:
                    _pc_done_sent = True
                    try:
                        safe_send(chat_id, "✅ Проверка цен завершена!")
                        _send_lot_pairs_file(chat_id)
                    except Exception:
                        pass
                    continue

                # Ошибки
                if any(k in line_lower for k in ("ошибка", "error")):
                    try:
                        safe_send(chat_id, f"⚠️ {line_raw[:150]}")
                    except Exception:
                        pass
                continue

            # ── Обычный режим ─────────────────────────────────────────────
            instant = _make_from_map(_INSTANT_MAP, line_lower, line_raw)
            if instant:
                flush()
                try:
                    safe_send(chat_id, instant)
                except Exception as e:
                    logger.warning(f"instant: {e}")

                # Переключение игры в мультирежиме
                if _multi_chat == chat_id and _multi_games.get(chat_id):
                    if "подходящих лотов не найдено" in line_lower:
                        _no_lots_count[chat_id] = _no_lots_count.get(chat_id, 0) + 1
                        if _no_lots_count[chat_id] >= NO_LOTS_LIMIT:
                            _no_lots_count[chat_id] = 0
                            next_choice, next_name = _next_game(chat_id)
                            if next_name:
                                _switching_to_next = True
                                _intentional_stop  = True
                                safe_send(chat_id,
                                    f"⏭ Нет лотов — переключаюсь на *{next_name}*",
                                    parse_mode="Markdown")
                                threading.Thread(
                                    target=_restart_for_game,
                                    args=(chat_id, next_choice, next_name),
                                    daemon=True,
                                ).start()
                                return  # выходим из текущего stream_logs
                    elif "лот опубликован ok" in line_lower:
                        _no_lots_count[chat_id] = 0  # сбрасываем если опубликовали

                continue

            status = _make_from_map(_STATUS_MAP, line_lower, line_raw)
            if not status:
                continue

            buffer.append(status)
            if len(buffer) >= 3:
                flush()

    except Exception as e:
        logger.error(f"stream_logs: {e}")
        try:
            safe_send(chat_id, f"⚠️ stream_logs crash: {e}")
        except Exception:
            pass
    finally:
        flush()
        with _process_lock:
            if bot_process:
                try:
                    bot_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        log_streaming = False
        with _process_lock:
            bot_process = None
        try:
            if _switching_to_next:
                # Идёт переключение на следующую игру — ничего не показываем
                _switching_to_next = False
                _intentional_stop  = False
            elif _intentional_stop:
                _intentional_stop = False
                safe_send(chat_id, "⛔ Бот остановлен", reply_markup=main_keyboard())
            else:
                logger.error("stream_logs: процесс завершился неожиданно")
                safe_send(chat_id, "🔴 Бот упал — перезапусти вручную!", reply_markup=main_keyboard())
        except Exception:
            pass


# ════════════════════════════════════════════════
# Guards
# ════════════════════════════════════════════════

def guard(func):
    # БАГ #7 ИСПРАВЛЕН: functools.wraps сохраняет имя функции для telebot
    @functools.wraps(func)
    def wrapper(msg):
        if msg.from_user.id != ALLOWED_ID:
            return
        return func(msg)
    return wrapper


def guard_cb(func):
    @functools.wraps(func)
    def wrapper(call):
        if call.from_user.id != ALLOWED_ID:
            return
        return func(call)
    return wrapper


# ════════════════════════════════════════════════
# Хендлеры — Reply Keyboard
# ════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
@guard
def cmd_start(msg):
    safe_send(msg.chat.id, "👋 *G2G Bot — управление*",
                     parse_mode="Markdown", reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: m.text == "🚀 Запустить все")
@guard
def cmd_run_all(msg):
    global _multi_games, _multi_pos, _multi_chat
    games = get_games()
    if not games:
        safe_send(msg.chat.id, "❌ Нет активных игр в config.json")
        return
    chat_id = msg.chat.id
    if len(games) > 1:
        all_idx = list(range(len(games)))
        _multi_games[chat_id] = all_idx
        _multi_pos[chat_id] = 0
        _multi_chat = chat_id
        _no_lots_count[chat_id] = 0
        names = " → ".join(g["name"] for g in games)
        safe_send(chat_id, f"▶️ Мультирежим: {names}\nНачинаю с: {games[0]['name']}")
    start_bot_process("1", chat_id, label=games[0]["name"])


@bot.message_handler(func=lambda m: m.text == "🎮 Выбрать игры")
@guard
def cmd_choose(msg):
    _selected[msg.chat.id] = set()
    _mode[msg.chat.id] = "run"
    safe_send(msg.chat.id, "Выбери игры для запуска:",
                     reply_markup=games_keyboard(mode="run"))


@bot.message_handler(func=lambda m: m.text == "⛔ Остановить бота")
@guard
def cmd_stop(msg):
    stop_bot()
    safe_send(msg.chat.id, "⛔ Бот остановлен", reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: m.text == "📊 Статус")
@guard
def cmd_status(msg):
    from datetime import datetime
    games  = get_games()
    status = "🟢 Запущен" if is_running() else "🔴 Остановлен"
    lines  = [
        f"📊 *Статус бота:* {status}",
        "",
        f"✅ *Активных игр: {len(games)}*",
    ]
    for g in games:
        lines.append(f"  • {g['name']}")

    try:
        today = datetime.now().strftime("%Y-%m-%d")
        stats_data = {}
        if STATS_PATH.exists():
            stats_data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        today_stats = stats_data.get(today, {})
        total_today = sum(today_stats.values())
        lines.append("")
        lines.append(f"💰 *Продано сегодня ({total_today} всего):*")
        if today_stats:
            for game, count in sorted(today_stats.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  • {game}: {count} шт.")
        else:
            lines.append("  (пока ничего)")
    except Exception:
        pass

    safe_send(msg.chat.id, "\n".join(lines),
              parse_mode="Markdown", reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: m.text == "📋 Логи")
@guard
def cmd_logs(msg):
    logs = sorted(LOG_DIR.glob("bot_*.log"),
                  key=lambda f: f.stat().st_mtime, reverse=True)
    if logs:
        try:
            text = logs[0].read_text(encoding="utf-8", errors="ignore")[-2500:]
            safe_send(msg.chat.id,
                             f"`{logs[0].name}`\n```\n{text}\n```",
                             parse_mode="Markdown")
        except Exception:
            safe_send(msg.chat.id, "❌ Не удалось прочитать лог")
    else:
        safe_send(msg.chat.id, "Логов пока нет")


@bot.message_handler(func=lambda m: m.text == "🔍 Проверить базу")
@guard
def cmd_check(msg):
    if is_running():
        safe_send(msg.chat.id, "⚠️ Сначала останови бота!")
        return
    _selected[msg.chat.id] = set()
    _mode[msg.chat.id] = "check"
    pairs = load_pairs()
    total = sum(len(v) for v in pairs.values())
    safe_send(msg.chat.id,
              f"🔍 Выбери игры для проверки (лотов в базе: {total}):",
              reply_markup=games_keyboard(mode="check"))



@bot.message_handler(func=lambda m: m.text == "🗑 Удалить лоты")
@guard
def cmd_delete(msg):
    if is_running():
        safe_send(msg.chat.id, "⚠️ Сначала останови бота!")
        return
    _selected[msg.chat.id] = set()
    _mode[msg.chat.id] = "delete"
    safe_send(msg.chat.id, "Выбери игры для удаления лотов:",
                     reply_markup=games_keyboard(mode="delete"))


@bot.message_handler(func=lambda m: m.text == "📄 База лотов")
@guard
def cmd_lot_pairs(msg):
    _send_lot_pairs_file(msg.chat.id)


@bot.message_handler(func=lambda m: m.text == "💰 Проверить цены")
@guard
def cmd_prices(msg):
    if is_running():
        safe_send(msg.chat.id, "⚠️ Сначала останови бота!")
        return
    _selected[msg.chat.id] = set()
    _mode[msg.chat.id] = "prices"
    safe_send(msg.chat.id, "Выбери игру для проверки цен:",
                     reply_markup=games_keyboard(mode="prices"))


# ════════════════════════════════════════════════
# Callbacks
# ════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("tog."))
@guard_cb
def cb_toggle(call):
    # формат: tog.<idx>.<mode>
    parts = call.data.split(".")
    if len(parts) < 3:
        return
    idx  = int(parts[1])
    mode = parts[2]
    sel  = _selected.setdefault(call.message.chat.id, set())
    if idx in sel:
        sel.remove(idx)
    else:
        sel.add(idx)
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id,
            reply_markup=games_keyboard(sel, mode=mode),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "start_selected")
@guard_cb
def cb_start_selected(call):
    global _multi_games, _multi_pos, _multi_chat, _no_lots_count
    sel = _selected.get(call.message.chat.id, set())
    if not sel:
        bot.answer_callback_query(call.id, "Ничего не выбрано!")
        return
    games  = get_games()
    sorted_sel = sorted(sel)
    idx    = sorted_sel[0]
    if idx >= len(games):
        bot.answer_callback_query(call.id, "Ошибка: игра не найдена")
        return
    game   = games[idx]
    choice = str(idx + 1)

    # Инициализируем мультирежим если выбрано несколько игр
    chat_id = call.message.chat.id
    if len(sorted_sel) > 1:
        _multi_games[chat_id]  = sorted_sel
        _multi_pos[chat_id]    = 0        # начинаем с первой
        _multi_chat            = chat_id
        _no_lots_count[chat_id] = 0
        names = " → ".join(games[i]["name"] for i in sorted_sel if i < len(games))
        safe_edit(f"▶️ Мультирежим: {names}\nНачинаю с: {game['name']}",
                  chat_id, call.message.message_id)
    else:
        # Одна игра — сбрасываем мультирежим
        _multi_games.pop(chat_id, None)
        _multi_pos.pop(chat_id, None)
        _multi_chat = 0
        safe_edit(f"▶️ Запускаю: {game['name']}",
                  chat_id, call.message.message_id)

    _selected.pop(chat_id, None)
    start_bot_process(choice, chat_id, label=game["name"])


@bot.callback_query_handler(func=lambda c: c.data == "delete_selected")
@guard_cb
def cb_delete_selected(call):
    sel = _selected.get(call.message.chat.id, set())
    if not sel:
        bot.answer_callback_query(call.id, "Ничего не выбрано!")
        return
    safe_edit("🗑 Запускаю удаление лотов...",
                          call.message.chat.id, call.message.message_id)
    _selected.pop(call.message.chat.id, None)
    start_bot_process("0", call.message.chat.id,
                      label="Удаление лотов", is_cleanup=True)


@bot.callback_query_handler(func=lambda c: c.data == "check_selected")
@guard_cb
def cb_check_selected(call):
    sel = _selected.get(call.message.chat.id, set())
    if not sel:
        bot.answer_callback_query(call.id, "Ничего не выбрано!")
        return
    games = get_games()
    names = ", ".join(games[i]["name"] for i in sorted(sel) if i < len(games))
    if len(sel) > 1:
        note = "⚠️ Проверка запускается для всей базы (main.py не различает игры при чистке)"
    else:
        note = f"🔍 Проверяю: {names}"
    safe_edit(note, call.message.chat.id, call.message.message_id)
    _selected.pop(call.message.chat.id, None)
    start_bot_process("0", call.message.chat.id,
                      label=f"Проверка базы ({names})", is_cleanup=True)


@bot.callback_query_handler(func=lambda c: c.data == "check_all")
@guard_cb
def cb_check_all(call):
    safe_edit("🔍 Проверяю все игры...",
                          call.message.chat.id, call.message.message_id)
    _selected.pop(call.message.chat.id, None)
    start_bot_process("0", call.message.chat.id,
                      label="Проверка всей базы", is_cleanup=True)


@bot.callback_query_handler(func=lambda c: c.data == "prices_all")
@guard_cb
def cb_prices_all(call):
    safe_edit("💰 Проверяю цены для всех игр...",
                          call.message.chat.id, call.message.message_id)
    _selected.pop(call.message.chat.id, None)
    start_bot_process("p", call.message.chat.id,
                      label="Проверка цен (все игры)",
                      extra_inputs=["0"])


@bot.callback_query_handler(func=lambda c: c.data == "prices_selected")
@guard_cb
def cb_prices_selected(call):
    sel = _selected.get(call.message.chat.id, set())
    if not sel:
        bot.answer_callback_query(call.id, "Ничего не выбрано!")
        return

    games    = get_games()
    eligible = [g for g in games if g.get("price_tiers")]
    if not eligible:
        safe_edit("❌ Нет игр с price_tiers в конфиге.",
                              call.message.chat.id, call.message.message_id)
        return

    chosen = [games[i] for i in sorted(sel) if i < len(games)]
    # Берём только те что есть в eligible (имеют price_tiers)
    chosen_eligible = [g for g in chosen if g in eligible]
    if not chosen_eligible:
        safe_edit("❌ Выбранные игры не имеют price_tiers.",
                  call.message.chat.id, call.message.message_id)
        return

    # Передаём индексы выбранных игр через запятую: "1,3,5"
    idxs = [str(eligible.index(g) + 1) for g in chosen_eligible]
    sub_idx = ",".join(idxs) if len(idxs) > 1 else idxs[0]
    if len(chosen_eligible) > 1:
        names = ", ".join(g["name"] for g in chosen_eligible)
        label = f"Проверка цен ({names})"
    else:
        label = f"Проверка цен ({chosen_eligible[0]['name']})"

    safe_edit(f"💰 {label}",
                          call.message.chat.id, call.message.message_id)
    _selected.pop(call.message.chat.id, None)
    start_bot_process("p", call.message.chat.id, label=label,
                      extra_inputs=[sub_idx])


@bot.callback_query_handler(func=lambda c: c.data == "cancel")
@guard_cb
def cb_cancel(call):
    safe_edit("❌ Отменено",
                          call.message.chat.id, call.message.message_id)
    safe_send(call.message.chat.id, "Главное меню:", reply_markup=main_keyboard())


# ════════════════════════════════════════════════
# Запуск
# ════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        me = bot.get_me()
        logger.info(f"✅ Бот @{me.username} подключён")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения: {e}")
        sys.exit(1)

    init_stats_from_pairs()

    try:
        bot.send_message(ALLOWED_ID,
                         "✅ *Telegram Control запущен!*\nВыбери действие:",
                         parse_mode="Markdown", reply_markup=main_keyboard())
    except Exception as e:
        logger.warning(f"Стартовое сообщение: {e}")

    while True:
        try:
            logger.info("🔄 Polling...")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except KeyboardInterrupt:
            logger.info("⛔ Остановлено")
            break
        except Exception as e:
            logger.error(f"Polling: {e}")
            logger.info("⏳ Перезапуск через 10 сек...")
            time.sleep(10)