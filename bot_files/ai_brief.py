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
    "wow":         "Focus on: character level, class, server, gear score or key achievements",
    "zenless":     "Focus on: inter-knot level, S-rank agents, server region",
    "eve":         "Focus on: skill points (SP), ship types (Titan/Rorqual/etc.), key skills",
    "throne":      "Focus on: gear score, server, class, key items",
    "black desert":"Focus on: gear score (GS), class, level, key items (TET/PEN Blackstar etc.)",
    "summoners":   "Focus on: nat5 count, key monsters, server",
    "warframe":    "Focus on: Mastery Rank (MR), platinum, Prime Vault, key mods",
    "drakensang":  "Focus on: named sets (Dragan/BGH/Winter/Guardian etc.), Knowledge points, level, class",
}


def _get_hint(game_name: str) -> str:
    name_lower = game_name.lower()
    for key, hint in _GAME_HINTS.items():
        if key in name_lower:
            return hint
    return "Focus on: level, key items, notable stats"


def generate_brief_ai(
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
        "model": "claude-haiku-4-5-20251001",
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
