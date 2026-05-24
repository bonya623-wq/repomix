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


# FunPay EU server name (lowercase, без региона) → G2G server name
# Серверы НЕ в этом словаре (напр. Books of Calan) — пропускаются
_FUNPAY_TO_G2G_SERVER = {
    "akeba":                  "Akeba",
    "al'maiesh":              "Al'Maiesh",
    "ammuit":                 "Ammuit",
    "angiris council":        "Angiris Council (EN)",
    "aranoch":                "Aranoch (IT)",
    "archbishop lazarus":     "Archbishop Lazarus (EN)",
    "arreat summit":          "Arreat Summit (EN)",
    "beledwe":                "Beledwe (FR)",
    "blood rose":             "Blood Rose (EN)",
    "cathan":                 "Cathan",
    "cathedral of light":     "Cathedral of Light (EN)",
    "charsi":                 "Charsi (FR)",
    "crodric":                "Crodric",
    "crystal arch":           "Crystal Arch (EN)",
    "dark exile":             "Dark Exile (EN)",
    "dark wanderer":          "Dark Wanderer (EN)",
    "diamond gates":          "Diamond Gates (EN)",
    "dravec":                 "Dravec (FR)",
    "el'druin":               "El' Druin",
    "esu":                    "Esu (FR)",
    "fara":                   "Fara (ES)",
    "farnham":                "Farnham (ES)",
    "frost horrors":          "Frost Horrors (EN)",
    "frozen orb":             "Frozen Orb (EN)",
    "gardens of hope":        "Gardens of Hope (EN)",
    "gharbad the weak":       "Gharbad the Weak (EN)",
    "greiz":                  "Greiz (ES)",
    "harrogath":              "Harrogath (GE)",
    "hemlir":                 "Hemlir (GE)",
    "horadric malus":         "Horadric Malus (IT)",
    "hratli":                 "Hratli (GE)",
    "imperius":               "Imperius (EN)",
    "itherael":               "Itherael",
    "kabraxis":               "Kabraxis (FR)",
    "karshun":                "Karshun (PL)",
    "kesor":                  "Kesor (PL)",
    "kion":                   "Kion (PL)",
    "larzuk":                 "Larzuk (GE)",
    "leoric":                 "Leoric (FR)",
    "marius":                 "Marius (GE)",
    "oblivion knight":        "Oblivion Knight (EN)",
    "peace warders":          "Peace Warders (EN)",
    "pools of wisdom":        "Pools of Wisdom",
    "qual-kehk":              "Qual-Kehk (GE)",
    "sea of light":           "Sea of Light (EN)",
    "segithis":               "Segithis",
    "sescheron":              "Sescheron",
    "sightless eye":          "Sightless Eye (EN)",
    "skarn":                  "Skarn",
    "solarion":               "Solarian (EN)",  # FunPay пишет Solarion, G2G — Solarian
    "stone of jordan":        "Stone of Jordan (EN)",
    "stygian fury":           "Stygian Fury (EN)",
    "tabri":                  "Tabri (GE)",
    "talus'ar":               "Talus' Ar",
    "talva silvertongue":     "Talva Silvertongue (EN)",
    "the ancients":           "The Ancients (EN)",
    "the borderlands":        "The Borderlands (EN)",
    "the butcher":            "The Butcher (EN)",
    "the countess":           "The Countess (EN)",
    "the hellforge":          "The Hellforge (EN)",
    "the martyr":             "The Martyr (EN)",
    "the unspoken":           "The Unspoken (EN)",
    "the void":               "The Void (EN)",
    "thorned hulk":           "Thorned Hulk (EN)",
    "throne of destruction":  "Throne of Destruction (EN)",
    "trade consortium":       "Trade Consortium (EN)",
    "vizjerei":               "Vizjerei",
    "warden of everfrost":    "Warden of Everfrost (PL)",
    "wodem castell":          "Wodem Castell (PL)",
    "wood wraith":            "Wood Wraith (EN)",
    "yshari sanctum":         "Yshari Sanctum (EN)",
    "zatham":                 "Zatham",
    "zolthrax":               "Zolthrax (GE)",
    # Books of Calan — есть на FunPay, нет на G2G → не добавляем, лот пропускается
}


def get_di_server(funpay_server: str) -> str:
    """Возвращает G2G-имя сервера или '' если сервер не поддерживается на G2G."""
    s = funpay_server.strip()
    if not s.lower().startswith("(eu) "):
        return ""  # не EU — пропускаем
    base = s[5:].strip().lower()
    return _FUNPAY_TO_G2G_SERVER.get(base, "")


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
