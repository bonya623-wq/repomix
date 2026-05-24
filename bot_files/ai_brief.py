"""
ai_brief.py — Claude API для генерации краткого описания лота.

Читает описание с FunPay, возвращает краткую строку для G2G brief field.
Fallback: возвращает None — вызывающий код использует старую логику.
"""
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

_GAME_HINTS = {
    "raid":        "Focus on: power level, legendary count, mythic count, key champions (Arbiter, Siphi, etc.)",
    "wow":         (
        "Strict priority order for WoW account listings:\n"
        "1. GS/ilvl (e.g. '1750 GS', 'ilvl 450') — FIRST, most important\n"
        "2. Flying mount speed (e.g. 'Fly 60%', 'Epic Flying', '100% Mount')\n"
        "3. Professions with skill level (e.g. 'Enchanting 300', 'Herbalism 375')\n"
        "4. Race + Class + Level + Spec + BiS (e.g. 'Undead Priest 70 Shadow', 'Orc Warrior 70 Fury Pre-BiS')\n"
        "5. Dual Spec (if mentioned)\n"
        "6. Attunement / Heroic Keys / Raids (e.g. 'Kara attuned', 'Full Heroic Keys')\n"
        "7. PvP titles / Duelist / arena rating\n"
        "8. Named mounts / Legendary weapons\n"
        "9. Expansion / Season (e.g. 'TBC', 'Anniversary', 'S1')\n"
        "10. Server + Region (e.g. 'Spineshatter EU') — LAST\n"
        "Skip: account security info, country, subscription days, delivery info."
    ),
    "zenless":     "Focus on: inter-knot level, S-rank agents, server region",
    "eve":         (
        "Strict priority order for EVE Online account listings:\n"
        "1. Skill Points — e.g. '42M SP', '85M SP'\n"
        "2. Ship types — Titan, Rorqual, Carrier, Dreadnought, Orca, Supercarrier\n"
        "3. Key skills — Cyno V, Jump Drive Calibration V, Industry skills\n"
        "4. ISK / assets — e.g. '5B ISK', 'fitted ships'\n"
        "5. Region — EU / US — LAST\n"
        "Skip: account security, email, delivery info."
    ),
    "throne":      (
        "Strict priority order for Throne and Liberty account listings:\n"
        "1. Gear Score (GS) — e.g. '3500 GS' — FIRST, most important\n"
        "2. Weapon / class — e.g. 'Greatsword/Daggers', 'Staff/Wand', 'Crossbow/Dagger'\n"
        "3. Key items / sets — e.g. 'Tier 2 set', named set pieces, rare accessories\n"
        "4. Level — e.g. 'Lv50', 'Max level'\n"
        "5. Server — LAST\n"
        "Skip: account security, delivery info."
    ),
    "black desert": (
        "Strict priority order for Black Desert Online account listings:\n"
        "1. Gear Score (GS) — e.g. '280 GS', '320 GS' — FIRST, most important\n"
        "2. Key gear — TET/PEN Blackstar weapons, TET/PEN accessories (e.g. TET Capotia, PEN Crescent)\n"
        "3. Class — e.g. 'Warrior', 'Witch', 'Dark Knight'\n"
        "4. Level — e.g. 'Lv65', 'Lv67'\n"
        "5. Server / region — LAST\n"
        "Skip: account security, delivery info."
    ),
    "summoners":   (
        "Strict priority order for Summoners War account listings:\n"
        "1. Nat5 count — e.g. '45 nat5', '120 nat5' — FIRST, most important\n"
        "2. Key monsters by name — e.g. 'Verad, Tesarion, Bigo, Perna'\n"
        "3. Server — Global / Europe / Asia / Korea\n"
        "4. Arena / RTA ranking — e.g. 'C1 arena', 'G1 RTA'\n"
        "5. Legend runes count — e.g. '800 legend runes'\n"
        "Skip: account security, delivery info."
    ),
    "warframe":    "Focus on: Mastery Rank (MR), platinum, Prime Vault, key mods",
    "drakensang":  "Focus on: named sets (Dragan/BGH/Winter/Guardian etc.), Knowledge points, level, class",
    "wuthering":   (
        "Strict priority order for Wuthering Waves account listings:\n"
        "1. 5★ Resonators — list S-rank characters by name (Jinhsi, Camellya, Carlotta, Rover, Jiyan, etc.) — FIRST\n"
        "2. 5★ Weapons — S-rank weapons by name if mentioned\n"
        "3. Astrite — gacha currency amount (e.g. '12000 Astrite', '80 pulls')\n"
        "4. Echo/Sets — echo builds, set names (e.g. 'Sierra Gale set', '5pc Moonlit')\n"
        "5. Union Level — e.g. 'UL80', 'UL70'\n"
        "6. Server — EU — LAST\n"
        "Skip: account security, delivery info, payment methods."
    ),
    "diablo":      (
        "Strict priority order for Diablo Immortal account listings:\n"
        "1. Combat Rating (CR) — e.g. 'CR 5500', 'CR 4800' — FIRST, most important\n"
        "2. Resonance — e.g. 'Res 4200', 'Resonance 3800' — second most important\n"
        "3. Class — e.g. 'Necromancer', 'Blood Knight', 'Barbarian'\n"
        "4. 5-star legendary gems — e.g. '2x 5★', '5★ Voidgemstone' — if mentioned\n"
        "5. Paragon level — e.g. 'Paragon 650', 'P850'\n"
        "6. Key sets/gear — e.g. 'Shepherd set', 'Hailstone' — if mentioned\n"
        "7. Server — LAST\n"
        "Skip: account security, delivery info, payment methods."
    ),
}


def _get_hint(game_name: str) -> str:
    name_lower = game_name.lower()
    for key, hint in _GAME_HINTS.items():
        if key in name_lower:
            return hint
    return "Focus on: level, key items, notable stats"


def check_lot_safe_ai(description: str, api_key: str = "") -> bool:
    """
    Проверяет через Claude можно ли публиковать лот.
    Возвращает False если в описании несменная почта или Facebook-привязка.
    При ошибке API возвращает True (не блокируем лот при сбое).
    """
    if not api_key or not description.strip():
        return True

    text = description.strip()[:1000]

    prompt = (
        "You are checking a game account listing. Answer ONLY 'YES' or 'NO'.\n\n"
        "Does this listing mention ANY of the following:\n"
        "- Email cannot be changed / non-changeable email / email is fixed\n"
        "- Facebook login / Facebook linked / account bound to Facebook\n"
        "- Any similar restriction that prevents the buyer from securing the account\n\n"
        f"Listing text:\n{text}\n\n"
        "Answer YES if any such restriction is mentioned, NO if not."
    )

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        answer = result["content"][0]["text"].strip().upper()
        is_unsafe = answer.startswith("YES")
        if is_unsafe:
            logger.info(f"AI фильтр: лот заблокирован (несменная почта/FB)")
        return not is_unsafe
    except Exception as e:
        logger.warning(f"AI фильтр: ошибка ({e}) — лот пропускаем")
        return True



    game_name: str,
    description: str,
    extra_context: str = "",
    api_key: str = "",
    max_chars: int = 150,
) -> str | None:
    """
    Calls Claude Haiku to generate a brief description.
    Returns the brief string, or None if API call fails.
    """
    if not api_key:
        return None

    text = description.strip()
    if not text:
        return None

    # Truncate input to save tokens — 1500 chars is enough
    if len(text) > 1500:
        text = text[:1500]

    hint = _get_hint(game_name)
    extra = f"\nExtra info: {extra_context}" if extra_context else ""

    prompt = (
        f"Game: {game_name}\n"
        f"{extra}"
        f"Listing description:\n{text}\n\n"
        f"{hint}.\n"
        f"Write a SHORT English summary, max {max_chars} characters.\n"
        f"Format: key facts separated by ' + ' or ' | '. No emojis. No filler.\n"
        f"Return ONLY the summary line, nothing else."
    )

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        brief = result["content"][0]["text"].strip()
        # Safety trim
        brief = brief[:max_chars]
        logger.info(f"AI brief [{game_name}]: {brief!r}")
        return brief
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.warning(f"AI brief HTTP {e.code}: {body[:200]}")
        return None
    except Exception as e:
        logger.warning(f"AI brief error: {e}")
        return None
