"""Title and description formatter for G2G listings."""

from parser import AccountData


def _shorten(name: str) -> str:
    """
    Extract the most recognizable word from a champion name.
    "Lady Mikage" → "Mikage", "Gharol Bloodmaul" → "Bloodmaul"
    """
    parts = name.strip().split()
    return parts[-1] if len(parts) > 1 else name


def _fmt_silver(millions: float) -> str:
    return f"{millions:.1f}M Silver"


def _fmt_energy(raw: float) -> str:
    if raw >= 1_000:
        return f"{raw / 1_000:.1f}K"
    return str(int(raw))


# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------

def format_title(acc: AccountData) -> str:
    """
    Build G2G listing title (≤ 255 chars).

    Example:
      [3 MYTH] Mikage • Bloodmaul • Mezomel • Ezio • Valkyrie • 7.6M Silver • 7696 Gems
    """
    segments: list[str] = []

    if acc.mythic_champions:
        segments.append(f"[{len(acc.mythic_champions)} MYTH]")
        segments.extend(_shorten(c) for c in acc.mythic_champions)

    # Up to 4 legendary names (shortened)
    segments.extend(_shorten(c) for c in acc.legendary_champions[:4])

    if acc.silver is not None:
        segments.append(_fmt_silver(acc.silver))

    if acc.gems is not None:
        segments.append(f"{acc.gems} Gems")

    title = " • ".join(segments) if segments else "Raid Shadow Legends Account"
    return title[:255]


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------

def format_description(acc: AccountData) -> str:
    """
    Build full G2G listing description.

    Matches the format from the spec exactly.
    """
    lines: list[str] = []

    lines += [
        "⚔️ RAID SHADOW LEGENDS — ACCOUNT FOR SALE ⚔️",
        "",
    ]

    # Mythics
    if acc.mythic_champions:
        lines.append(f"🔮 Mythic Champions ({len(acc.mythic_champions)}):")
        for c in acc.mythic_champions:
            lines.append(f"• {c}")
        lines.append("")

    # Legendaries — show up to 15, wrapped in groups of 5
    if acc.legendary_champions:
        lines.append("⭐ Legendary Champions:")
        max_show = 15
        shown = acc.legendary_champions[:max_show]
        has_more = len(acc.legendary_champions) > max_show
        chunks = [shown[i : i + 5] for i in range(0, len(shown), 5)]
        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            prefix = "• " if idx == 0 else "  "
            suffix = "..." if is_last and has_more else ("," if not is_last else "")
            lines.append(f"{prefix}{', '.join(chunk)}{suffix}")
        lines.append("")

    # Resources block
    lines.append("📦 Resources:")

    row1 = []
    if acc.silver is not None:
        row1.append(f"Silver: {acc.silver:.2f}M")
    if acc.gems is not None:
        row1.append(f"Gems: {acc.gems}")
    if acc.energy is not None:
        row1.append(f"Energy: {_fmt_energy(acc.energy)}")
    if row1:
        lines.append("• " + "  |  ".join(row1))

    row2 = []
    if acc.cb_keys is not None:
        row2.append(f"CB Keys: {acc.cb_keys}")
    if acc.brews is not None:
        row2.append(f"Brews: {acc.brews}")
    if row2:
        lines.append("• " + "  |  ".join(row2))

    if acc.tomes:
        parts = [f"{count} {rarity}" for rarity, count in acc.tomes.items()]
        lines.append(f"• Tomes: {' / '.join(parts)}")

    if acc.shards:
        parts = [f"{count} {stype}" for stype, count in acc.shards.items()]
        lines.append(f"• Shards: {', '.join(parts)}")

    if acc.chickens:
        parts = [f"{star} ×{count}" for star, count in acc.chickens.items()]
        lines.append(f"• Chickens: {' / '.join(parts)}")

    if acc.total_heroes is not None:
        lines.append(f"• Total Heroes: {acc.total_heroes}")

    lines.append("")

    if acc.account_age_days is not None:
        lines.append(f"📅 Account Age: {acc.account_age_days} days")

    lines.append(f"🔑 Account ID: {acc.account_id}")
    lines += [
        "",
        "✅ Email is changeable — you become the sole owner",
        "❓ Questions? Just ask!",
    ]

    return "\n".join(lines)
