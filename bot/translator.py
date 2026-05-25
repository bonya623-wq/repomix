"""
Chinese → English translation for RSL account data.

Uses deep-translator (free Google Translate web API, no API key needed).
Install: pip install deep-translator

RSL champion names in Chinese are official game localizations and often
don't translate literally to English names.  The CHAMPION_MAP below maps
the most common ones.  Add more entries as you encounter unknown names.
"""

import logging
import re

logger = logging.getLogger(__name__)

try:
    from deep_translator import GoogleTranslator
    _AVAIL = True
except ImportError:
    _AVAIL = False
    logger.warning("deep-translator not installed — run: pip install deep-translator")


# ---------------------------------------------------------------------------
# Known Chinese → English RSL champion name mappings.
# Chinese names are simplified (zh-CN) as used in the game.
# Extend this dict whenever you see a "???" champion name in a listing.
# ---------------------------------------------------------------------------
CHAMPION_MAP: dict[str, str] = {
    # ── Mythics ──────────────────────────────────────────────────────────────
    "御剑仙子": "Lady Mikage",
    "加罗尔": "Gharol Bloodmaul",
    "梅佐梅尔": "Mezomel Luperfang",
    # ── Top Legendaries ──────────────────────────────────────────────────────
    "女武神": "Valkyrie",
    "仲裁者": "Arbiter",
    "西菲": "Siphi the Lost Bride",
    "莉莉图公爵夫人": "Duchess Lilitu",
    "尼娜": "Ninja",
    "斯基尔": "Scyl of the Drakes",
    "隆达": "Ronda",
    "布罗尼": "Brogni",
    "图鲁纳": "Trunda Giltmallet",
    "墓穴领主": "Tomb Lord",
    "安加尔": "Angar",
    "阿斯特拉隆": "Astralon",
    "薇希克斯": "Visix the Unbowed",
    "迪肯·阿姆斯特朗": "Deacon Armstrong",
    "克里斯克": "Krisk the Ageless",
    "巴德·艾尔·卡扎尔": "Bad-el-Kazar",
    "玛蒂尔": "Martyr",
    "马丹·瑟里斯": "Madame Serris",
    "凯马尔": "Kymar",
    "图尔沃德": "Turvold",
    "领主": "Warlord",
    "莉迪亚": "Lydia the Deathsiren",
    "罗托斯": "Rotos the Lost Groom",
    "地理学家": "Geomancer",
    "阿尔坦": "Altan",
    "铁腕布拉戈": "Iron Brago",
    "伊兹·莉克斯伯爵夫人": "Countess Lix",
    "鹿骑士": "Stag Knight",
    "托尔明": "Tormin the Cold",
    "冰幽灵": "Ghostborn",
    "皇家卫队": "Royal Guard",
    "拉曼图": "Ramantu Drakesblood",
    "丰收杰克": "Harvest Jack",
    "骷髅皇冠": "Skullcrown",
    "冰封女妖": "Frozen Banshee",
    "深渊男爵夫人": "Infernal Baroness",
    "厄斯福": "Versulf the Grim",
    "剑白骑士": "Wytheknight",
    "多姆斯克里奇": "Doomscreech",
    "马沙德": "Ma'Shalled",
    "雷因兽": "Reinbeast",
    "坎德拉丰": "Candraphon",
}

# All known English RSL champion names (for fuzzy-match fallback)
ALL_EN_CHAMPIONS: list[str] = sorted(set(CHAMPION_MAP.values()) | {
    "Ezio Auditore", "Maulie Tankard", "Frenzy", "Skullcrown",
    "Tatura Rimehide", "Versulf the Grim",
})


def has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def translate_to_en(text: str) -> str:
    """
    Translate a block of Chinese text to English.
    Returns the original text unchanged if translation is unavailable or fails.
    Splits into ≤4500-char chunks to stay under Google's free-tier limit.
    """
    if not text.strip() or not has_chinese(text):
        return text
    if not _AVAIL:
        return text
    try:
        MAX = 4500
        chunks = [text[i : i + MAX] for i in range(0, len(text), MAX)]
        parts = []
        for chunk in chunks:
            t = GoogleTranslator(source="zh-CN", target="en").translate(chunk)
            parts.append(t or chunk)
        return "\n".join(parts)
    except Exception as exc:
        logger.warning(f"Translation failed: {exc}")
        return text


def translate_champion(zh_name: str) -> str:
    """
    Translate a single champion name CN→EN.
    Tries exact map first, then Google Translate, then returns original.
    """
    zh_name = zh_name.strip()
    if zh_name in CHAMPION_MAP:
        return CHAMPION_MAP[zh_name]
    if not has_chinese(zh_name):
        return zh_name
    return translate_to_en(zh_name)


def translate_champion_list(names: list[str]) -> list[str]:
    return [translate_champion(n) for n in names if n.strip()]
