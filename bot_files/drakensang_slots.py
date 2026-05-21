FUNPAY_TO_G2G_SERVER = {
    "(eu) grimmag": "[EU] Grimmag",
    "(eu) harold":  "[EU] Harold",
    "(eu) heredur": "[EU] Heredur",
    "(eu) werian":  "[EU] Werian",
    "(us) agathon": "[US] Agathon",
    "(us) tegan":   "[US]Tegan",
}

# FunPay EN display text matches G2G exactly — just normalise case
FUNPAY_TO_G2G_CLASS = {
    "dragonknight":    "Dragonknight",
    "spellweaver":     "Spellweaver",
    "ranger":          "Ranger",
    "steam mechanicus": "Steam Mechanicus",
}


def get_dso_server(funpay_server: str) -> str:
    return FUNPAY_TO_G2G_SERVER.get(funpay_server.strip().lower(), "")


def get_dso_class(funpay_class: str) -> str:
    return FUNPAY_TO_G2G_CLASS.get(funpay_class.strip().lower(), "")


def dso_level_tier(level: int) -> str:
    if level >= 100: return "100"
    if level >= 90:  return "90+"
    if level >= 80:  return "80+"
    if level >= 70:  return "70+"
    if level >= 60:  return "60+"
    if level >= 50:  return "50+"
    if level >= 40:  return "40+"
    if level >= 30:  return "30+"
    return "29 or below"


def generate_dso_brief(level: int, dso_class: str, server: str, original_brief: str) -> str:
    parts = []
    if level > 0:  parts.append(f"Lv{level}")
    if dso_class:  parts.append(dso_class)
    if server:     parts.append(server.replace("[EU] ", "").replace("[US] ", ""))
    prefix = " | ".join(parts)
    brief = f"{prefix} | {original_brief}" if prefix else original_brief
    return brief[:200]
