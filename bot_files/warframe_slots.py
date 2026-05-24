def warframe_rank_tier(rank: int) -> str:
    if rank >= 36: return "36"
    if rank == 35: return "35"
    if rank == 34: return "34"
    if rank == 33: return "33"
    if rank == 32: return "32"
    if rank == 31: return "31"
    if 26 <= rank <= 30: return "26 - 30"
    if 21 <= rank <= 25: return "21 - 25"
    if 16 <= rank <= 20: return "16 - 20"
    if 11 <= rank <= 15: return "11 - 15"
    return "1 - 10"


def generate_warframe_brief(rank: int, original_brief: str) -> str:
    prefix = f"MR{rank} | " if rank > 0 else ""
    return f"{prefix}{original_brief}"[:200]
