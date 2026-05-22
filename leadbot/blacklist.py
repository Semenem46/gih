"""
Чёрный список чатов для live_push.

Эти чаты никогда не попадают в pending_review даже если есть FTS5-матч —
там сидят соискатели вакансий, а не заказчики услуг.
Экономит DeepSeek-токены и убирает основной шум.
"""
from __future__ import annotations

import re

# Регексы для chat_key (после lower()). Если хоть один совпал — чат игнорим.
BLACKLIST_PATTERNS = [
    # «работа/rabota» — основной мусор по chats_audit (60-70% корпуса)
    r"\brabota",
    r"\brabotaw",
    r"\brabotaq",
    r"\brabotat",
    r"\brabotaz",
    r"работа",
    r"_rabot",
    r"rabot_",
    # Вакансии и HR
    r"\bvakan",
    r"вакан",
    r"\bjobs?\b",
    r"\bhh[_-]",
    r"\bhr[_-]",
    r"recruit",
    r"_career",
    r"career_",
    r"hire_",
    # Соискатели/подработка
    r"podrabotka",
    r"подработ",
    r"\bsoiska",
    # Узбекские/таджикские чаты вакансий мигрантов
    r"musofir",
    r"_ishlar",
    r"ishlar_",
    # Обфускация цифрами/латиницей
    r"pa6ota",      # PA6OTA = работа с 6 вместо «б»
    r"pab[oо]ta",   # paбota и подобное
    r"r[a4]bota",   # r4bota
    # Шумные региональные work-чаты
    r"_rabotaz",
    r"_rabotat",
    r"_rabotav",
    r"_rabotag",
    r"_rabotaw",
    r"_rabotac",
    # Знакомства / шопинг / прочее не по теме B2B
    r"\bznakom",
    r"знаком",
    r"_shop_",
    r"shop_moskva",
    r"_dating",
    # Объявления / барахолки
    r"\bbaraholka",
    r"барахол",
    r"\bobjavleni",
    # Аренда квартир / бытовуха не наша
    r"kvartira_ish",
    r"_obi",  # @mahachkala_obi и подобные «общаги»
]

# Скомпилированные паттерны (один раз на старте)
_COMPILED = [re.compile(p, re.IGNORECASE) for p in BLACKLIST_PATTERNS]


def is_blacklisted(chat_key: str | None) -> bool:
    """True если чат в чёрном списке."""
    if not chat_key:
        return False
    key = chat_key.lower()
    return any(rx.search(key) for rx in _COMPILED)


# Дополнительный exact-list (если какой-то конкретный канал надо явно убить)
EXACT_BLACKLIST: set[str] = {
    # Добавь сюда руками если найдёшь конкретный шумный чат, который не ловится regex'ом
    # пример: "@some_specific_chat",
}


def is_blacklisted_exact(chat_key: str | None) -> bool:
    if not chat_key:
        return False
    return chat_key.lower() in {k.lower() for k in EXACT_BLACKLIST}


def should_skip_chat(chat_key: str | None) -> bool:
    """Главная функция: True = пропустить чат, не лезть в pending_review."""
    return is_blacklisted(chat_key) or is_blacklisted_exact(chat_key)


if __name__ == "__main__":
    # Тест на твоих топ-чатах из chats_audit.csv
    test_cases = [
        ("@barnaul_rabotar", True),
        ("@PERM_PA6OTA", True),  # обфускация — теперь ловим
        ("@rabota_ekbq", True),
        ("@Chelyabinsk_biz", False),
        ("@biznes_v_krd", False),
        ("@padelpro_spb_ru", False),
        ("@Madina_shop_Moskva", True),
        ("@mahachkala_obi", True),
        ("@kvartira_musofirlar_ishlar", True),
        ("@seo_goodguys", False),
        ("@biznesdvigkrd", False),
        ("@chatb2bnews", False),
        ("@novosibirsk_biz", False),
    ]
    print(f"{'chat':<40} {'expected':<10} {'actual':<10} {'ok':<5}")
    for chat, expected in test_cases:
        got = should_skip_chat(chat)
        ok = "✓" if got == expected else "✗"
        print(f"{chat:<40} {str(expected):<10} {str(got):<10} {ok}")
