"""patch_aggregator_filter.py — отсечь каналы-агрегаторы где контакт за PRO-подпиской.

Что делает:
  1. blacklist.py — добавляет regex для известных аукционных/посреднических каналов
  2. query_engine.py — добавляет GLOBAL_STOP_PHRASES (фразы из текста сообщения которые
     указывают что это пост агрегатора: "Pro подписке", "получили этот контакт", и т.д.)
  3. query_engine.py — обновляет qualify-промпт: явное правило отказа от агрегаторов

Идемпотентен.
"""
from __future__ import annotations
import shutil
import sys
import time
from pathlib import Path

LEADBOT = Path("/root/leadbot")
BLACKLIST = LEADBOT / "blacklist.py"
QE = LEADBOT / "query_engine.py"

if not BLACKLIST.exists() or not QE.exists():
    print(f"ERROR: не нашёл {BLACKLIST} или {QE}")
    sys.exit(1)

ts = int(time.time())

# ── 1. blacklist.py ─────────────────────────────────────────────────────────
shutil.copy(BLACKLIST, BLACKLIST.parent / f"blacklist.py.bak_{ts}")
bl_content = BLACKLIST.read_text(encoding="utf-8")

aggregator_block = """
    # Каналы-агрегаторы / биржи (контакты за PRO-подпиской — бесполезные лиды)
    r"proektport",
    r"workspot",
    r"vakanc.*direct",
    r"freelancehunt",
    r"upwork_ru",
    r"_aukcion",
    r"\\bbiding",
    r"buyguide",
"""

if "proektport" in bl_content:
    print("· blacklist.py: уже содержит proektport")
else:
    # Вставляем перед закрывающим ]
    marker = "# Аренда квартир / бытовуха не наша"
    if marker in bl_content:
        bl_content = bl_content.replace(marker, aggregator_block.strip() + "\n    " + marker)
        BLACKLIST.write_text(bl_content, encoding="utf-8")
        print("✓ blacklist.py: добавлены паттерны агрегаторов")
    else:
        # fallback: вставляем в конец BLACKLIST_PATTERNS
        bl_content = bl_content.replace(
            "BLACKLIST_PATTERNS = [\n",
            "BLACKLIST_PATTERNS = [\n" + aggregator_block,
        )
        BLACKLIST.write_text(bl_content, encoding="utf-8")
        print("✓ blacklist.py: паттерны добавлены в начало списка")

# ── 2. query_engine.py — GLOBAL_STOP_PHRASES + патч qualify-промпта ─────────
shutil.copy(QE, QE.parent / f"query_engine.py.bak_{ts}")
qe_content = QE.read_text(encoding="utf-8")

# 2a. Добавить GLOBAL_STOP_PHRASES константу + использовать в _fetch_fts_candidates
global_stops_def = '''
# Глобальные стоп-фразы (отсекают агрегаторы / биржи где контакт за PRO-подпиской)
GLOBAL_STOP_PHRASES = [
    "доступно в pro",
    "доступно в про",
    "pro подписк",
    "про подписк",
    "pro-участник",
    "про-участник",
    "получили этот контакт",
    "получили эту вакансию",
    "контакт автора скрыт",
    "автор скрыт",
    "хочешь быть в числе первых",
    "оформи подписку чтобы",
    "только для платных",
    "premium участник",
    "премиум участник",
]
'''

if "GLOBAL_STOP_PHRASES" in qe_content:
    print("· query_engine.py: GLOBAL_STOP_PHRASES уже есть")
else:
    # Вставляем после QUALIFIER_THRESHOLD строки
    marker = "QUALIFIER_THRESHOLD"
    if marker in qe_content:
        # Найти первую строку с QUALIFIER_THRESHOLD и вставить после неё
        lines = qe_content.split("\n")
        new_lines = []
        inserted = False
        for i, line in enumerate(lines):
            new_lines.append(line)
            if not inserted and line.startswith("QUALIFIER_THRESHOLD"):
                # вставляем после константной секции (когда найдём пустую строку или комментарий)
                pass
        qe_content = qe_content.replace(
            "QUALIFIER_THRESHOLD = 85",
            "QUALIFIER_THRESHOLD = 85\n" + global_stops_def,
            1,
        )
        # fallback если 85 не нашли (может быть 80 или 75)
        if "GLOBAL_STOP_PHRASES" not in qe_content:
            for thr_val in ("80", "75"):
                key = f"QUALIFIER_THRESHOLD = {thr_val}"
                if key in qe_content:
                    qe_content = qe_content.replace(key, key + "\n" + global_stops_def, 1)
                    break
        print("✓ query_engine.py: GLOBAL_STOP_PHRASES добавлены")
    else:
        print("⚠️ не нашёл QUALIFIER_THRESHOLD — пропускаю вставку GLOBAL_STOP_PHRASES")

# 2b. В функции _fetch_fts_candidates добавить применение GLOBAL_STOP_PHRASES
# Находим строку: if any(sk in text_lower for sk in stop_keywords):
#                  continue
# Заменяем на: if any(sk in text_lower for sk in stop_keywords + GLOBAL_STOP_PHRASES):

old_check = "if any(sk in text_lower for sk in stop_keywords):"
new_check = "if any(sk in text_lower for sk in stop_keywords + GLOBAL_STOP_PHRASES):"

if new_check in qe_content:
    print("· _fetch_fts_candidates: уже использует GLOBAL_STOP_PHRASES")
elif old_check in qe_content:
    qe_content = qe_content.replace(old_check, new_check)
    print("✓ _fetch_fts_candidates: применяет GLOBAL_STOP_PHRASES")
else:
    print("⚠️ не нашёл строку фильтрации в _fetch_fts_candidates")

# 2c. Добавить в qualify-промпт правило отказа от агрегаторов
old_aggr_marker = "❌ Бот / реклама / новостной канал."
new_aggr_marker = """❌ КАНАЛ-АГРЕГАТОР / БИРЖА (контакт скрыт за PRO-подпиской):
   Признаки в тексте: "Доступно в Pro подписке", "PRO-участники получили",
                      "Хочешь быть в числе первых", "Контакт автора скрыт",
                      "оформи подписку чтобы".
   → Это перепродажа лидов, не реальный заказчик. ОТКЛОНЯЙ.

❌ Бот / реклама / новостной канал."""

if "КАНАЛ-АГРЕГАТОР" in qe_content:
    print("· qualify-промпт: уже содержит правило про агрегаторы")
elif old_aggr_marker in qe_content:
    qe_content = qe_content.replace(old_aggr_marker, new_aggr_marker)
    print("✓ qualify-промпт: добавлено правило про агрегаторы")
else:
    print("⚠️ не нашёл маркер 'Бот / реклама' в qualify — пропускаю")

QE.write_text(qe_content, encoding="utf-8")

print("\n✅ patched")
print(f"Откат: cp {BLACKLIST}.bak_{ts} {BLACKLIST}; cp {QE}.bak_{ts} {QE}")
