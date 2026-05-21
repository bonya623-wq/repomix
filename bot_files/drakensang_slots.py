import re

FUNPAY_TO_G2G_SERVER = {
    "(eu) grimmag": "[EU] Grimmag",
    "(eu) harold":  "[EU] Harold",
    "(eu) heredur": "[EU] Heredur",
    "(eu) werian":  "[EU] Werian",
    "(us) agathon": "[US] Agathon",
    "(us) tegan":   "[US]Tegan",
}

FUNPAY_TO_G2G_CLASS = {
    "dragonknight":    "Dragonknight",
    "spellweaver":     "Spellweaver",
    "ranger":          "Ranger",
    "steam mechanicus": "Steam Mechanicus",
}

# Named sets/items to detect — order matters (checked top to bottom)
_NAMED_SETS = [
    ("dragan",      "Dragan Set"),
    ("golden dragon", "Golden Dragon Set"),
    ("bgh",         "BGH Set"),
    ("winter",      "Winter Set"),
    ("guardian",    "Guardian Set"),
    ("sargon",      "Sargon Set"),
    ("herald",      "Herald Set"),
    ("q7",          "Q7 Set"),
    ("chinese",     "Chinese New Year Set"),
    ("lunar",       "Lunar Set"),
]

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FFFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F"
    r"\U0001F900-\U0001F9FF\U00002702-\U000027B0]+"
)


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


def _extract_highlights(text: str) -> str:
    """
    Extracts: named sets + Knowledge count.
    Example output: "Dragan Set + BGH Set + Winter Set + 189 Knowledge"
    """
    text_lower = text.lower()
    parts = []

    # Named sets (in defined order, deduplicated)
    for kw, label in _NAMED_SETS:
        if kw in text_lower:
            parts.append(label)

    # Knowledge points: "189 knowledge" or "knowledge 189"
    m = re.search(r"(\d+)\s*knowledge", text_lower) or \
        re.search(r"knowledge\s*[:\-]?\s*(\d+)", text_lower)
    if m:
        parts.append(f"{m.group(1)} Knowledge")

    return " + ".join(parts)


def generate_dso_brief(level: int, dso_class: str, server: str,
                       original_brief: str, detailed: str = "") -> str:
    """
    Builds a concise G2G brief:
      Lv100 Steam Mechanicus | Grimmag | Dragan Set + BGH Set + 189 Knowledge
    Falls back to original_brief if nothing extracted.
    """
    header_parts = []
    if level > 0:  header_parts.append(f"Lv{level}")
    if dso_class:  header_parts.append(dso_class)
    header = " ".join(header_parts)

    srv = server.replace("[EU] ", "").replace("[US] ", "").strip()

    source = (detailed or "") + " " + (original_brief or "")
    highlights = _extract_highlights(source)

    if not highlights:
        # Fallback: strip emoji from original brief and use first 100 chars
        clean = _EMOJI_RE.sub("", original_brief or "").strip()
        highlights = re.sub(r"\s+", " ", clean)[:100]

    parts = [p for p in [highlights, header, srv] if p]
    return " | ".join(parts)[:200]
