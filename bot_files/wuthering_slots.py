"""
wuthering_slots.py — Wuthering Waves helpers
G2G Union Level tiers: 80 / 70+ / 50+ / 30+ / 10+ / 9 or below
"""


def ww_union_level(level: int) -> str:
    if level >= 80: return "80"
    if level >= 70: return "70+"
    if level >= 50: return "50+"
    if level >= 30: return "30+"
    if level >= 10: return "10+"
    return "9 or below"


def generate_ww_brief(union_level: str, original_brief: str = "") -> str:
    parts = []
    if union_level:
        parts.append(f"UL{union_level.rstrip('+')}")
    if original_brief:
        parts.append(original_brief[:100])
    return " | ".join(parts)
