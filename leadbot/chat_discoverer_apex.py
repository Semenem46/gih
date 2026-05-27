import os
import json
import traceback
import asyncio
import random
import logging
import aiosqlite
from typing import List
import httpx
from datetime import datetime, timezone, date
from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError
from telethon.tl.types import Chat, Channel

# ==========================================
# ⚙️ НАСТРОЙКИ
# ==========================================
API_ID = 37992056
API_HASH = '60613234526a75894811075d80c5b7f3'
SESSION_NAME = 'apex_discoverer_ai'           # ⚡ ОТДЕЛЬНАЯ сессия от парсера (важно!)
DB_FILE = 'apex_ai.db'                        # ⚡ ОТДЕЛЬНАЯ БД, общая с parser_apex_ai.py
OFFSETS_FILE = 'parser_offsets.json'
ADMIN_ID = 8128303065
ALERT_BOT_TOKEN = os.environ.get('BOT_TOKEN') or os.environ.get('ALERT_BOT_TOKEN', '')

# 🛡️ СУТОЧНЫЙ ЛИМИТ ЗАПРОСОВ (anti-ban hard cap)
DAILY_QUERY_BUDGET = 250          # макс. поисков в сутки (был ~500-1000)
MIN_MEMBERS = 200                 # пропускать мёртвые чаты <200 участников
DAILY_BUDGET_FILE = 'discoverer_budget.json'

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DISCOVERER-AI] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ==========================================
# 🧠 ГЕНЕРАЦИЯ ВОРОНКИ ПОИСКА (AI-НИША)
# ==========================================

# === Прямые AI-продукты (где сидят те, кто УЖЕ ищет/обсуждает) ===
AI_PRODUCT_NICHES = [
    "чат-бот", "чатбот", "telegram бот", "тг-бот", "ии-бот", "ai bot",
    "ии-ассистент", "ai-ассистент", "автоворонка", "автоматизация бизнеса",
    "автоматизация воронки", "автоматизация продаж", "автоответчик",
    "голосовой бот", "voicebot",
]

# === AI-стек: чаты разработчиков, no-code мейкеров (потенциальные клиенты) ===
AI_TECH_NICHES = [
    "chatgpt", "openai", "deepseek", "claude ai", "gpt-4", "gpt бизнес",
    "n8n", "make.com", "zapier", "no-code", "low-code", "лоу-код",
    "интеграмат", "albato", "альбато",
    "промпт инженер", "ии разработка", "ai разработка",
]

# === CRM-интеграции (горячая зона: бизнес уже платит за CRM, готов докупать автоматизацию) ===
CRM_INTEGRATION_NICHES = [
    "amocrm", "amo crm", "амоцрм", "битрикс24", "bitrix24",
    "1с интеграция", "crm автоматизация", "интеграция api",
    "автоворонка amocrm", "ботконструктор",
]

# === B2B и владельцы бизнеса (целевая аудитория, нет осознанной потребности в боте) ===
BUSINESS_OWNER_NICHES = [
    "предприниматели", "владельцы бизнеса", "малый бизнес",
    "интернет магазин владельцы", "ecommerce", "оптовая торговля",
    "smm агентство", "маркетинговое агентство", "владельцы агентств",
    "стартап", "founders", "b2b", "чат предпринимателей",
    "клуб предпринимателей", "бизнес-клуб", "нетворкинг бизнес",
]

# === Pain-зоны: куда боль приходит ===
PAIN_NICHES = [
    "колл-центр", "customer support", "клиентский сервис",
    "отдел продаж", "руководители продаж", "head of sales",
    "лидогенерация", "обработка заявок", "квалификация лидов",
    "чат поддержки", "первая линия поддержки",
]

# === Сидовые гео-комбинации ===
GEO_CITIES = [
    "москва", "спб", "казань", "новосибирск", "екатеринбург", "краснодар",
    "сочи", "ростов", "уфа", "самара", "красноярск", "челябинск", "омск",
    "пермь", "волгоград", "воронеж", "нижний новгород", "тюмень",
    "ярославль", "владивосток", "томск", "иркутск", "кемерово",
]

# 🛑 Расширенный фильтр инфоцыган и продавцов (срабатывает на title чата)
INFOGYPSY_STOP_WORDS = [
    # Старая база
    "заработок", "крипта", "инвестиции", "сигналы", "трейдинг",
    "наставник", "наставничество", "успешный успех", "миллион",
    "мышление", "энергия", "вибрации", "эзотерика", "таро", "матрица",
    "арбитраж", "p2p", "прогрев",
    # AI-инфоцыгане
    "курс по chatgpt", "курс chatgpt", "обучение нейрос", "школа нейрос",
    "школа ии", "обучение ии", "академия ии", "academy gpt",
    "промпт-инжиниринг для", "гайд по нейрос", "освой нейросет",
    "заработок на нейрос", "заработок на ии", "заработок с chatgpt",
    "купить ключ", "продажа ключей", "openai key shop",
    # Агентства-конкуренты, продающие то же что и мы (не клиенты)
    "разработка ботов под ключ", "делаем ботов", "пишем ботов",
    "студия чат-ботов", "агентство чат-ботов",
]


def generate_queries() -> List[str]:
    """
    Стратегия: 4 пласта.
    1) Прямые AI-чаты (где люди уже спрашивают про боты).
    2) Технологические чаты (no-code, разработчики).
    3) B2B / владельцы бизнеса (наша целевая аудитория, не осознают потребность).
    4) Pain-зоны (отделы продаж, поддержка) + гео-привязки.
    """
    queries = []

    # === Пласт 1: AI-продуктовые ниши без модификаторов ===
    queries.extend(AI_PRODUCT_NICHES)
    queries.extend(AI_TECH_NICHES)
    queries.extend(CRM_INTEGRATION_NICHES)

    # === Пласт 2: AI + модификаторы ===
    short_ai = ["бот", "чат-бот", "ии", "ai", "автоматизация", "n8n", "gpt"]
    for n in short_ai:
        for mod in ["чат", "сообщество", "разработчики", "услуги", "заказы"]:
            queries.append(f"{n} {mod}")

    # === Пласт 3: B2B / владельцы (без AI — это наша скрытая целевая) ===
    queries.extend(BUSINESS_OWNER_NICHES)
    queries.extend(PAIN_NICHES)

    # === Пласт 4: Гео × B2B (самая жирная связка для широкой воронки) ===
    business_mods = [
        "предприниматели", "бизнес чат", "b2b", "опт",
        "владельцы магазинов", "smm агентство",
    ]
    for city in GEO_CITIES:
        for mod in business_mods:
            queries.append(f"{city} {mod}")

    # === Пласт 5: Гео × AI/автоматизация (где локальный бизнес ищет автоматизацию) ===
    ai_mods_short = ["автоматизация", "чат-бот", "amocrm"]
    for city in GEO_CITIES:
        for mod in ai_mods_short:
            queries.append(f"{city} {mod}")

    # Чистка
    seen = set()
    out = []
    for q in queries:
        q_norm = " ".join(q.split()).strip().lower()
        if q_norm and q_norm not in seen:
            seen.add(q_norm)
            out.append(q_norm)
    return out


SEARCH_QUERIES = generate_queries()


# ==========================================
# 🛡️ СУТОЧНЫЙ БЮДЖЕТ ЗАПРОСОВ (Hard Cap)
# ==========================================
def load_today_budget() -> int:
    """Возвращает, сколько запросов уже потрачено сегодня."""
    if not os.path.exists(DAILY_BUDGET_FILE):
        return 0
    try:
        with open(DAILY_BUDGET_FILE, 'r') as f:
            data = json.load(f)
        if data.get("date") == date.today().isoformat():
            return int(data.get("count", 0))
        return 0
    except Exception:
        return 0


def save_today_budget(count: int) -> None:
    with open(DAILY_BUDGET_FILE, 'w') as f:
        json.dump({"date": date.today().isoformat(), "count": count}, f)


# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
async def load_known_chats(db_path: str) -> set:
    known = set()
    if os.path.exists(OFFSETS_FILE):
        with open(OFFSETS_FILE, 'r', encoding='utf-8') as f:
            try:
                known.update(json.load(f).keys())
            except Exception:
                pass

    async with aiosqlite.connect(db_path) as db:
        try:
            async with db.execute("SELECT chat_key FROM chat_offsets") as cursor:
                known.update([row[0] async for row in cursor])
            async with db.execute("SELECT chat_identifier FROM target_chats") as cursor:
                known.update([row[0] async for row in cursor])
        except Exception as e:
            logger.warning(f"⚠️ Таблицы кэша еще не созданы или пусты: {e}")

    return known


async def alert_admin(client: TelegramClient, text: str):
    try:
        async with httpx.AsyncClient() as http_client:
            url = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await http_client.post(url, json=payload)
    except Exception as e:
        logger.warning(f"🔇 Алерт сброшен (Ошибка HTTP: {str(e)[:40]}).")


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS target_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_identifier TEXT UNIQUE,
                title TEXT,
                source_query TEXT,
                members_count INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_processed BOOLEAN DEFAULT 0
            )
        """)
        # Миграция — если таблица старая (без members_count), добавим колонку
        try:
            await db.execute("ALTER TABLE target_chats ADD COLUMN members_count INTEGER DEFAULT 0")
            await db.commit()
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_offsets (
                chat_key TEXT PRIMARY KEY,
                last_id INTEGER DEFAULT 0
            )
        """)
        await db.commit()
    logger.info("🗄️ База данных инициализирована (WAL mode включен).")


async def save_chats_to_db(db_path: str, chats_data: List[tuple]) -> int:
    """chats_data = [(chat_identifier, title, source_query, members_count), ...]"""
    if not chats_data:
        return 0

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.executemany("""
            INSERT OR IGNORE INTO target_chats (chat_identifier, title, source_query, members_count)
            VALUES (?, ?, ?, ?)
        """, chats_data)

        offsets_data = [(chat[0], 0) for chat in chats_data]
        await db.executemany("""
            INSERT OR IGNORE INTO chat_offsets (chat_key, last_id)
            VALUES (?, ?)
        """, offsets_data)

        await db.commit()
        return cursor.rowcount


# ==========================================
# 🔎 ОДИН ЗАПРОС
# ==========================================
async def discover_chat_single(client: TelegramClient, query: str, known_chats: set) -> int:
    """
    Возвращает количество сырых найденных чатов (для детекта теневого бана).
    """
    logger.info(f"🔎 Поиск: '{query}'")

    try:
        # 🛡️ HYBRID: сначала статический поиск групп по названию
        static_res = await client(functions.contacts.SearchRequest(q=query, limit=50))

        # Микро-пауза перед тяжёлым SearchGlobalRequest
        await asyncio.sleep(random.uniform(2.5, 5.0))

        # 🛡️ Глубокий скан с УМЕНЬШЕННЫМ лимитом (был 30 → 20)
        dynamic_res = await client(functions.messages.SearchGlobalRequest(
            q=query,
            filter=types.InputMessagesFilterEmpty(),
            min_date=None, max_date=None, offset_rate=0,
            offset_peer=types.InputPeerEmpty(), offset_id=0, limit=20
        ))

        raw_chats = getattr(static_res, 'chats', []) + getattr(dynamic_res, 'chats', [])
        raw_chats_count = len(raw_chats)
        logger.info(f"📥 Telegram отдал {raw_chats_count} результатов. Фильтруем...")

        chats_to_insert = []
        for chat in raw_chats:
            if not isinstance(chat, (Chat, Channel)):
                continue

            # Только публичные группы / megagroup, не broadcast
            if getattr(chat, 'broadcast', False):
                continue
            if not getattr(chat, 'username', None):
                continue

            title = getattr(chat, 'title', 'Unknown') or ''
            members_count = getattr(chat, 'participants_count', 0) or 0

            # 🛡️ Фильтр по числу участников (мёртвые чаты не нужны)
            if members_count and members_count < MIN_MEMBERS:
                continue

            # 🛡️ Инфоцыгане + продавцы услуг (по title)
            title_low = title.lower()
            if any(word in title_low for word in INFOGYPSY_STOP_WORDS):
                continue

            chat_identifier = f"@{chat.username}"
            if chat_identifier in known_chats:
                continue

            known_chats.add(chat_identifier)
            chats_to_insert.append((chat_identifier, title, query, members_count))

        inserted_count = await save_chats_to_db(DB_FILE, chats_to_insert)

        if inserted_count > 0:
            logger.info(f"✅ Из {raw_chats_count} добавлено {inserted_count} НОВЫХ чатов по '{query}'.")
        else:
            logger.info(f"⚠️ Из {raw_chats_count} результатов все уже в базе.")

        return raw_chats_count

    except FloodWaitError as e:
        # Расширенный буфер для разведчика (он тяжелее парсера для ТГ)
        safe_sleep = e.seconds + random.uniform(120, 300)
        logger.warning(f"🛑 FloodWait! Охлаждаем на {safe_sleep:.0f} сек ({safe_sleep/60:.1f} мин)...")
        await alert_admin(client, f"⚠️ <b>Разведчик словил FloodWait!</b>\nОтдыхаем {int(safe_sleep/60)} минут...")
        await asyncio.sleep(safe_sleep)
        return -1
    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"❌ Ошибка при поиске '{query}': {error_trace}")
        await alert_admin(client, f"❌ <b>Ошибка разведчика!</b>\nЗапрос: <code>{query}</code>\nОшибка: {e}")
        return -1


# ==========================================
# ♾️ ГЛАВНЫЙ ЦИКЛ
# ==========================================
async def main() -> None:
    await init_db(DB_FILE)
    known_chats = await load_known_chats(DB_FILE)
    logger.info(f"🛡️ Стейт восстановлен: {len(known_chats)} известных чатов.")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    logger.info(f"🚀 Запуск AI-разведчика. Запросов в воронке: {len(SEARCH_QUERIES)}")
    logger.info(f"🛡️ Дневной лимит: {DAILY_QUERY_BUDGET} запросов. Мин. размер чата: {MIN_MEMBERS} участников.")
    await client.start()

    try:
        await alert_admin(client, f"🚀 <b>AI-разведчик Apex запущен!</b>\nЗапросов в воронке: {len(SEARCH_QUERIES)}\nДневной лимит: {DAILY_QUERY_BUDGET}")

        while True:
            # 🌙 Ночной сон UTC 19–23 (4–6 утра по МСК / 2–6 ночи по КРСК)
            utc_hour = datetime.now(timezone.utc).hour
            if 19 <= utc_hour <= 23:
                sleep_duration = random.uniform(18000, 25200)  # 5–7 часов (был 4–6)
                logger.info(f"🌙 Ночной сон {sleep_duration/3600:.1f}ч.")
                await alert_admin(client, "🌙 <b>Разведчик в ночном сне.</b>")
                await asyncio.sleep(sleep_duration)
                continue

            # 🛡️ ПРОВЕРКА СУТОЧНОГО БЮДЖЕТА
            today_count = load_today_budget()
            if today_count >= DAILY_QUERY_BUDGET:
                # Спим до полуночи UTC + jitter
                now = datetime.now(timezone.utc)
                seconds_to_midnight = ((24 - now.hour) * 3600) - (now.minute * 60) - now.second + random.randint(60, 1800)
                logger.info(f"🛡️ Суточный лимит {DAILY_QUERY_BUDGET} достигнут. Спим до завтра ({seconds_to_midnight/3600:.1f}ч).")
                await alert_admin(client, f"🛡️ <b>Дневной лимит выбран ({today_count}).</b>\nСон до завтра.")
                await asyncio.sleep(seconds_to_midnight)
                continue

            # Перемешиваем запросы каждый круг — паттерн поиска уникальный
            queries = list(SEARCH_QUERIES)
            random.shuffle(queries)

            consecutive_zeroes = 0
            consecutive_floods = 0

            for idx, query in enumerate(queries, 1):
                # Проверка лимита на каждом шаге (важно если ребут посреди дня)
                if load_today_budget() >= DAILY_QUERY_BUDGET:
                    logger.info("🛡️ Дневной лимит исчерпан в середине круга. Прерываемся.")
                    break

                raw_count = await discover_chat_single(client, query, known_chats)

                # Считаем потраченный бюджет (даже если был flood — счётчик зачёлся)
                save_today_budget(load_today_budget() + 1)

                # 🛡️ ДЕТЕКТ ТЕНЕВОГО БАНА — ужесточил с 20 до 15 нулей подряд
                if raw_count == 0:
                    consecutive_zeroes += 1
                    if consecutive_zeroes >= 15:
                        logger.error("🛑 ВЕРОЯТНО ТЕНЕВОЙ БАН ПОИСКА (15 пустых ответов подряд).")
                        await alert_admin(client, "🛑 <b>ТЕНЕВОЙ БАН РАЗВЕДЧИКА!</b>\nГлубокий сон 8 часов...")
                        await asyncio.sleep(28800)  # 8 часов (было 6)
                        consecutive_zeroes = 0
                elif raw_count > 0:
                    consecutive_zeroes = 0

                # 🛡️ ДЕТЕКТ КАСКАДА FLOOD — 3 подряд = серьёзная проблема
                if raw_count == -1:
                    consecutive_floods += 1
                    if consecutive_floods >= 3:
                        logger.error("🛑 КАСКАД FLOODWAIT (3 подряд). Глубокий сон 12 часов.")
                        await alert_admin(client, "🛑 <b>КАСКАД FLOODWAIT!</b>\nСпим 12 часов для остывания.")
                        await asyncio.sleep(43200)  # 12 часов
                        consecutive_floods = 0
                else:
                    consecutive_floods = 0

                # 🛡️ ANTI-BAN BATCH BREAK: каждые 15 (было 20) запросов перерыв 12–25 мин (было 10–20)
                if idx % 15 == 0:
                    cycle_sleep = random.uniform(720, 1500)
                    logger.info(f"🛡️ Перекур {cycle_sleep/60:.1f} мин ({idx} из {len(queries)}).")
                    await asyncio.sleep(cycle_sleep)
                else:
                    sleep_time = random.uniform(25.0, 50.0)  # ⚡ Было 15–35 → теперь 25–50 сек
                    await asyncio.sleep(sleep_time)

            # ♻️ Конец круга
            round_sleep = random.uniform(21600, 36000)  # 6–10 часов (было 4–8)
            logger.info(f"🏁 Круг окончен. Перерыв {round_sleep/3600:.1f}ч.")
            await alert_admin(client, f"🏁 <b>Круг разведки завершён.</b>\nПерерыв {round_sleep/3600:.1f}ч.")
            await asyncio.sleep(round_sleep)

    except asyncio.CancelledError:
        logger.info("🛑 Разведчик остановлен системно.")
        await alert_admin(client, "🛑 <b>AI-разведчик остановлен.</b>")
        raise
    except Exception as e:
        logger.critical(f"FATAL: {e}")
        error_trace = traceback.format_exc()
        await alert_admin(
            client,
            f"🚨 <b>КРАШ AI-РАЗВЕДЧИКА!</b>\n<b>Ошибка:</b> {e}\n\n<code>{error_trace[-1000:]}</code>"
        )
        raise
    finally:
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Остановлен пользователем.")
