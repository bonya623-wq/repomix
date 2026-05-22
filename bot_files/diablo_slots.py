"""
diablo_slots.py — Diablo Immortal helper functions.
Server/class mapping FunPay → G2G, level tier, brief generation.
"""


# FunPay class name (lowercase, Russian or English) → G2G English class name
FUNPAY_TO_G2G_CLASS = {
    # Russian
    "рыцарь крови":      "Blood Knight",
    "варвар":            "Barbarian",
    "крестоносец":       "Crusader",
    "охотник на демонов": "Demon Hunter",
    "друид":             "Druid",
    "монах":             "Monk",
    "некромант":         "Necromancer",
    "буревестник":       "Tempest",
    "повелитель бури":   "Tempest",
    "волшебник":         "Wizard",
    # English (in case FunPay already returns English)
    "blood knight":      "Blood Knight",
    "barbarian":         "Barbarian",
    "crusader":          "Crusader",
    "demon hunter":      "Demon Hunter",
    "druid":             "Druid",
    "monk":              "Monk",
    "necromancer":       "Necromancer",
    "tempest":           "Tempest",
    "wizard":            "Wizard",
}


def get_di_server(funpay_server: str) -> str:
    """Принимает только EU серверы, возвращает чистое имя без региона."""
    s = funpay_server.strip()
    if s.lower().startswith("(eu) "):
        return s[5:]  # "Blood Rose", "Akeba", etc.
    return ""  # не EU — пропускаем


def get_di_class(funpay_class: str) -> str:
    return FUNPAY_TO_G2G_CLASS.get(funpay_class.strip().lower(), "")


def di_level_tier(level: int) -> str:
    if level >= 60: return "60"
    if level >= 55: return "55+"
    if level >= 50: return "50+"
    if level >= 45: return "45+"
    if level >= 40: return "40+"
    if level >= 35: return "35+"
    if level >= 30: return "30+"
    return "29 or below"
