"""patch_aggregator_v2.py — закрывает дыру: query_engine не использовал blacklist чатов
и не отбрасывал агрегаторы с гугл-формами.

Что делает:
  1. Расширяет GLOBAL_STOP_PHRASES в query_engine.py: добавляет forms.gle, при поддержк,
     заполните анкету, фриланс кот, #ищу, #таргетолог, #контекстолог, и т.д.
  2. Добавляет в _fetch_fts_candidates фильтр чатов: импорт blacklist + skip если
     should_skip_chat(chat_key).
"""
from __future__ import annotations
import shutil
import sys
import time
from pathlib import Path

LEADBOT = Path("/root/leadbot")
QE = LEADBOT / "query_engine.py"

if not QE.exists():
    print(f"ERROR: не нашёл {QE}")
    sys.exit(1)

ts = int(time.time())
shutil.copy(QE, QE.parent / f"query_engine.py.bak_{ts}")
print(f"💾 Бэкап: query_engine.py.bak_{ts}")

content = QE.read_text(encoding="utf-8")

# ── 1. Расширить GLOBAL_STOP_PHRASES ────────────────────────────────────────
extra_stops = '''    "forms.gle",
    "google.com/forms",
    "при поддержк",
    "заполните анкету",
    "заполните форму",
    "отклик через форму",
    "фриланс кот",
    "#ищу #таргетолог",
    "#ищу #директолог",
    "#ищу #контекстолог",
    "#ищу #seo",
    "#ищу #разработчик",
    "#вакансия",
    "#вакансии",
    "вакансий чат",
    "работа удалённо",
    "удаленка",  # часто в вакансиях
'''

# Найти существующий список и добавить в конец
old_list_end = '    "премиум участник",\n]'
new_list_end = '    "премиум участник",\n' + extra_stops + ']'

if old_list_end in content:
    content = content.replace(old_list_end, new_list_end)
    print("✓ GLOBAL_STOP_PHRASES расширены (агрегаторы + хеш-теги вакансий)")
elif "forms.gle" in content:
    print("· уже расширены")
else:
    print("⚠️ не нашёл маркер GLOBAL_STOP_PHRASES — пропускаю")

# ── 2. Добавить blacklist чатов в _fetch_fts_candidates ─────────────────────
# Импортируем blacklist в начале файла
if "from blacklist import should_skip_chat" not in content:
    import_marker = "from openai import AsyncOpenAI"
    if import_marker in content:
        content = content.replace(
            import_marker,
            import_marker + "\n\n# Blacklist чатов (для отсечки work/vakansii/aggregator)\n"
            "import sys as _sys\n"
            "_sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "try:\n"
            "    from blacklist import should_skip_chat\n"
            "except ImportError:\n"
            "    def should_skip_chat(_): return False\n",
        )
        print("✓ импорт blacklist.should_skip_chat добавлен")

# Применить фильтр в _fetch_fts_candidates
# Находим блок:
#   text_lower = row["text"].lower()
#   if any(sk in text_lower for sk in stop_keywords + GLOBAL_STOP_PHRASES):
#       continue
#   rows.append(dict(row))
# Заменяем чтобы перед этим был skip по chat_key

old_block = '''                async for row in cur:
                    text_lower = row["text"].lower()
                    if any(sk in text_lower for sk in stop_keywords + GLOBAL_STOP_PHRASES):
                        continue
                    rows.append(dict(row))'''

# Если patch_aggregator_filter.py не применялся, может быть старая версия (без GLOBAL_STOP_PHRASES)
old_block_v1 = '''                async for row in cur:
                    text_lower = row["text"].lower()
                    if any(sk in text_lower for sk in stop_keywords):
                        continue
                    rows.append(dict(row))'''

new_block = '''                async for row in cur:
                    chat_key = row["chat_key"] or ""
                    if should_skip_chat(chat_key):
                        continue
                    text_lower = row["text"].lower()
                    if any(sk in text_lower for sk in stop_keywords + GLOBAL_STOP_PHRASES):
                        continue
                    rows.append(dict(row))'''

if "should_skip_chat(chat_key)" in content:
    print("· _fetch_fts_candidates: уже применяет blacklist чатов")
elif old_block in content:
    content = content.replace(old_block, new_block)
    print("✓ _fetch_fts_candidates: добавлен skip по blacklist чатов (v2)")
elif old_block_v1 in content:
    # Старая версия без GLOBAL_STOP — заменим целиком на новую с обоими фильтрами
    new_block_v1 = '''                async for row in cur:
                    chat_key = row["chat_key"] or ""
                    if should_skip_chat(chat_key):
                        continue
                    text_lower = row["text"].lower()
                    if any(sk in text_lower for sk in stop_keywords + GLOBAL_STOP_PHRASES):
                        continue
                    rows.append(dict(row))'''
    content = content.replace(old_block_v1, new_block_v1)
    print("✓ _fetch_fts_candidates: добавлен skip + GLOBAL_STOP_PHRASES (с v1 базы)")
else:
    print("⚠️ не нашёл блок фильтрации в _fetch_fts_candidates")

QE.write_text(content, encoding="utf-8")

# Sanity-check
import ast
try:
    ast.parse(content)
    print("✓ синтаксис query_engine.py валиден")
except SyntaxError as e:
    print(f"❌ СИНТАКСИС СЛОМАН: {e}")
    print(f"Откат: cp {QE}.bak_{ts} {QE}")
    sys.exit(1)

print(f"\n✅ patched")
print(f"Откат: cp {QE}.bak_{ts} {QE}")
