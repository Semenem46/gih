from __future__ import annotations
import re

BLACKLIST_PATTERNS = [
    r"\brabota", r"\brabotaw", r"\brabotaq", r"\brabotat", r"\brabotaz", r"работа", r"_rabot", r"rabot_",
    r"\bvakan", r"вакан", r"\bjobs?\b", r"\bhh[_-]", r"\bhr[_-]", r"recruit", r"_career", r"career_", r"hire_",
    r"podrabotka", r"подработ", r"\bsoiska", r"musofir", r"_ishlar", r"ishlar_", r"pa6ota", r"pab[oо]ta", r"r[a4]bota",
    r"_rabotaz", r"_rabotat", r"_rabotav", r"_rabotag", r"_rabotaw", r"_rabotac", r"\bznakom", r"знаком", r"_shop_", 
    r"shop_moskva", r"_dating", r"\bbaraholka", r"барахол", r"\bobjavleni", r"proektport", r"workspot", 
    r"vakanc.*direct", r"freelancehunt", r"upwork_ru", r"_aukcion", r"\bbiding", r"buyguide", r"\bvpsmm", r"_vpsmm", 
    r"\bbirzha", r"_birzha", r"birzh_", r"\bfrilans", r"\bfreelans", r"freelance_b", r"poisk_reklama", r"_poisk_", 
    r"poisk_freelance", r"_uz_", r"_tj_", r"tashkent_rabot", r"\buzb_", r"uzbek_", r"tajik_", r"_migrant", 
    r"resume_", r"_resume", r"\bcv_", r"kvartira_ish", r"_obi", r"\brabotchnich"
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in BLACKLIST_PATTERNS]

def is_blacklisted(chat_key: str | None) -> bool:
    if not chat_key: return False
    key = chat_key.lower()
    return any(rx.search(key) for rx in _COMPILED)

EXACT_BLACKLIST: set[str] = {"@rabotchnichki", "rabotchnichki", "работнички"}

def is_blacklisted_exact(chat_key: str | None) -> bool:
    if not chat_key: return False
    return chat_key.lower() in {k.lower() for k in EXACT_BLACKLIST}

def should_skip_chat(chat_key: str | None) -> bool:
    return is_blacklisted(chat_key) or is_blacklisted_exact(chat_key)
