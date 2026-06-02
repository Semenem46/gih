"""
parser_apex_ai.py — ИНДЕКСЕР (новая архитектура)

Что делает:
  1. Читает чаты из apex_ai.db (туда их кладёт chat_discoverer_apex.py).
  2. Применяет лёгкий префильтр: INTENT-сигнал есть + нет очевидного спам-слова.
  3. Сохраняет сырое сообщение в таблицу messages_corpus + FTS5 индекс.
  4. AI-скоринг и фильтрация по нише клиента происходят в query_engine.py
     ПО ЗАПРОСУ — не здесь.

Главное:
  - НЕ зовёт DeepSeek
  - НЕ алёртит каждый лид (только статистику раз в час)
  - НЕ привязан ни к какой нише — индексирует всё подряд с intent-сигналом
  - Антифлуд: 50 новых чатов/день, 30/цикл, 250/день суммарно
"""
import asyncio
import os
import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import FloodWaitError
import httpx
import aiosqlite

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import APEX_DB as _PATHS_APEX_DB, PARSER_SESSION as _PATHS_SESSION, PARSER_BUDGET_FILE as _PATHS_BUDGET
from source_policy import ensure_source_policy_columns

# ==========================================
# ⚙️ НАСТРОЙКИ
# ==========================================
API_ID = 37992056
API_HASH = '60613234526a75894811075d80c5b7f3'
SESSION_NAME = os.environ.get("PARSER_SESSION") or _PATHS_SESSION
ADMIN_ID = 8128303065
ALERT_BOT_TOKEN = '8565672652:AAGpwT7Lg50bSL-SDBgwG15ci0BcSydNAU4'

APEX_DB = os.environ.get("APEX_DB") or _PATHS_APEX_DB
DAILY_BUDGET_FILE = os.environ.get("PARSER_BUDGET_FILE") or _PATHS_BUDGET

# 🛡️ АНТИ-ФЛУД ЛИМИТЫ (0 флуд-бана)
MAX_NEW_CHATS_PER_DAY = 50
MAX_CHATS_PER_CYCLE = 30
MAX_CHATS_PER_DAY = 250

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# 🎯 Тестовые чаты (всегда первыми, лимиты не считаются)
TEST_CHATS = [
    "https://t.me/rabotchnichki",
]

# ==========================================
# 🔑 ПРЕФИЛЬТР — УНИВЕРСАЛЬНЫЙ INTENT (НЕ привязан к нише)
# ==========================================
# Сохраняем сообщение в корпус, если есть ХОТЯ БЫ ОДНО intent-слово
# и НЕТ очевидного спам-слова (см. STOP_WORDS).
# Фильтрация по нише клиента — на стороне query_engine.py.
INTENT_WORDS = [
    "нужен", "нужна", "нужно", "нужны",
    "ищу", "ищем", "ищите",
    "посоветуйте", "посоветуете", "порекомендуйте",
    "требуется", "требуются",
    "подскажите", "подсказать",
    "кто может", "кто делает", "кто занимается", "кто настроит",
    "кто сделает", "кто возьмется", "кто возьмётся", "кто работает",
    "подрядчик", "исполнитель", "специалист", "агентство", "студия",
    "разработчик", "мастер", "команда",
    "есть кто", "заказать", "найти", "найду",
    "цена", "стоимость", "сколько стоит", "прайс", "расценки",
    "помогите найти", "посоветуйте кого", "контакты",
    "интересует", "хочу заказать", "хочу нанять",
    "ищется", "услуги", "услугу",
    # боли (часто без явного "ищу")
    "не успева", "не справля",
    "теряем", "теряются", "уходят без ответа",
    "много рутин", "руками обрабатыв", "вручную",
    "разгрузить", "автоматизир", "оптимизир",
    "предложите", "предложить", "предложение",
]

# Универсальный спам-фильтр: режем СОВСЕМ очевидный мусор.
# Запросы под конкретные ниши не режем — клиент может искать что угодно.
STOP_WORDS = [
    # Соискатели / резюме
    'ищу работу', 'ищу проект', 'резюме', 'вакансия', 'вакансии',
    'без опыта', 'возьму на стажировку', 'мое резюме', 'моё резюме',
    # Продавцы услуг (не наша целевая)
    'предлагаю свои услуги', 'оказываю услуги',
    # Финансовый мусор
    'крипт', 'инвестиции', 'ставки на спорт', 'казино',
    'p2p обмен', 'трейдинг', 'сигналы по крипте',
    # Инфоцыгане
    'наставник', 'наставничество', 'школа',
    'курс по chatgpt', 'курс по нейрос', 'обучение по',
    'марафон по', 'вебинар по',
    # Пиар / админчат-мусор
    'взаимопиар', 'pr-обмен', 'бесплатно', 'розыгрыш',
]

# ==========================================
# 🛡️ СУТОЧНЫЙ БЮДЖЕТ
# ==========================================
def _load_today_budget() -> dict:
    if not os.path.exists(DAILY_BUDGET_FILE):
        return {"date": date.today().isoformat(), "new_chats": 0, "total_chats": 0, "saved_messages": 0}
    try:
        with open(DAILY_BUDGET_FILE, 'r') as f:
            data = json.load(f)
        if data.get("date") == date.today().isoformat():
            data.setdefault("saved_messages", 0)
            return data
    except Exception:
        pass
    return {"date": date.today().isoformat(), "new_chats": 0, "total_chats": 0, "saved_messages": 0}


def _save_today_budget(data: dict) -> None:
    with open(DAILY_BUDGET_FILE, 'w') as f:
        json.dump(data, f)


def can_process_new_chat() -> bool:
    return _load_today_budget()["new_chats"] < MAX_NEW_CHATS_PER_DAY


def can_process_any_chat() -> bool:
    return _load_today_budget()["total_chats"] < MAX_CHATS_PER_DAY


def increment_budget(is_new: bool, saved_count: int = 0) -> None:
    data = _load_today_budget()
    data["total_chats"] += 1
    if is_new:
        data["new_chats"] += 1
    data["saved_messages"] = data.get("saved_messages", 0) + saved_count
    _save_today_budget(data)


# ==========================================
# 🗄️ БАЗА ДАННЫХ — корпус сообщений + FTS5
# ==========================================
async def init_db():
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")

        # Таблица оффсетов (чтобы не перечитывать одни и те же сообщения)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_offsets (
                chat_key TEXT PRIMARY KEY,
                last_id INTEGER DEFAULT 0
            )
        """)

        # Корпус сообщений — главная таблица
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages_corpus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id INTEGER NOT NULL,
                chat_key TEXT NOT NULL,
                chat_id INTEGER,
                chat_title TEXT,
                sender_id INTEGER,
                sender_username TEXT,
                text TEXT NOT NULL,
                link TEXT,
                msg_date TIMESTAMP NOT NULL,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_key, msg_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_msg_date ON messages_corpus(msg_date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_key ON messages_corpus(chat_key)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sender ON messages_corpus(sender_id)")

        # FTS5 для быстрого полнотекстового поиска по нишам клиентов
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(text, content='messages_corpus', content_rowid='id',
                       tokenize='unicode61 remove_diacritics 1')
        """)
        # Триггеры синхронизации FTS с основной таблицей
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_corpus_ai
            AFTER INSERT ON messages_corpus BEGIN
                INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_corpus_ad
            AFTER DELETE ON messages_corpus BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
            END
        """)

        # Claim-лог: какие лиды уже куплены каким пользователем (эксклюзивность)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claimed_leads (
                corpus_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(corpus_id) REFERENCES messages_corpus(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_claimed_user ON claimed_leads(user_id)")

        # Подписки клиентов (для бота / query_engine)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                niche_text TEXT NOT NULL,
                niche_keywords TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                leads_delivered INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            )
        """)

        await ensure_source_policy_columns(db)
        await db.commit()
    logger.info(f"📂 БД готова: {APEX_DB} (messages_corpus + FTS5 + claimed_leads + subscriptions)")


async def sync_chats_from_db(chat_offsets: dict) -> dict:
    """Втягиваем новые чаты из target_chats (туда пишет разведчик).
    Чаты с source_policy='block' полностью игнорируются."""
    async with aiosqlite.connect(APEX_DB) as db:
        try:
            async with db.execute(
                "SELECT chat_identifier FROM target_chats "
                "WHERE is_processed = 0 AND COALESCE(source_policy, 'mixed') != 'block'"
            ) as cursor:
                new_chats = await cursor.fetchall()
                if new_chats:
                    await db.executemany(
                        "INSERT OR IGNORE INTO chat_offsets (chat_key, last_id) VALUES (?, 0)",
                        new_chats
                    )
                    await db.execute("UPDATE target_chats SET is_processed = 1 WHERE is_processed = 0")
                    await db.commit()
                    logger.info(f"📥 Синхронизировано {len(new_chats)} новых чатов из разведчика.")
        except sqlite3.OperationalError as e:
            if "no such table: target_chats" in str(e):
                logger.warning("⚠️ Таблица target_chats еще не создана — разведчик ещё не запускался.")
            else:
                raise

        async with db.execute("SELECT chat_key, last_id FROM chat_offsets") as cursor:
            return {row[0]: row[1] async for row in cursor}


async def update_chat_offset(chat_key: str, msg_id: int):
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO chat_offsets (chat_key, last_id) VALUES (?, ?)",
            (chat_key, msg_id)
        )
        await db.commit()


async def save_messages_to_corpus(rows: list[dict]) -> int:
    """Пишет пакет сообщений в корпус. Возвращает реально вставленных."""
    if not rows:
        return 0
    async with aiosqlite.connect(APEX_DB) as db:
        cur = await db.executemany(
            """INSERT OR IGNORE INTO messages_corpus
               (msg_id, chat_key, chat_id, chat_title,
                sender_id, sender_username, text, link, msg_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(r["msg_id"], r["chat_key"], r["chat_id"], r["chat_title"],
              r["sender_id"], r["sender_username"], r["text"], r["link"], r["msg_date"])
             for r in rows]
        )
        await db.commit()
        return cur.rowcount or 0


# ==========================================
# 🚨 АЛЕРТЫ (только статистика, не каждый лид)
# ==========================================
async def alert_admin(text: str):
    try:
        async with httpx.AsyncClient() as http_client:
            url = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            await http_client.post(url, json=payload)
    except Exception as e:
        logger.error(f"🔇 Алерт сброшен: {repr(e)}")


# ==========================================
# 🔄 ОБРАБОТКА ОДНОГО ЧАТА — индексирование в корпус
# ==========================================
async def index_chat(chat, chat_name: str, chat_offsets: dict, time_limit: datetime, is_test: bool = False) -> int:
    """Вытягивает свежие сообщения из чата, фильтрует по INTENT, складывает в корпус.
    Возвращает количество сохранённых сообщений."""
    chat_key = chat_name if chat_name.startswith("@") else str(chat.id)
    last_msg_id = chat_offsets.get(chat_key, chat_offsets.get(str(chat.id), 0))
    if is_test:
        last_msg_id = 0
    highest_msg_id = last_msg_id

    is_new_chat = (last_msg_id == 0) and not is_test

    if is_new_chat and not can_process_new_chat():
        logger.info(f"⏸️ {chat_name} — лимит новых чатов ({MAX_NEW_CHATS_PER_DAY}/день) исчерпан.")
        return 0

    fetch_limit = 30 if is_new_chat else 40
    if is_test:
        fetch_limit = 100

    chat_title = getattr(chat, 'title', None) or chat_name
    chat_id_val = getattr(chat, 'id', None)

    logger.info(f"📥 {chat_name} (от ID:{last_msg_id}, лимит:{fetch_limit}, new={is_new_chat})")

    saved_count = 0
    try:
        messages = await client.get_messages(chat, limit=fetch_limit, min_id=last_msg_id)
        if not messages:
            if not is_test:
                increment_budget(is_new_chat, 0)
            return 0

        messages = list(reversed(messages))
        rows_to_insert = []

        for msg in messages:
            if msg.id > highest_msg_id:
                highest_msg_id = msg.id
            if not msg.text or msg.date < time_limit:
                continue

            text_lower = msg.text.lower()

            # Префильтр: должен быть INTENT-сигнал
            if not any(kw in text_lower for kw in INTENT_WORDS):
                continue
            # Стоп-слова — режем универсальный мусор
            if any(w in text_lower for w in STOP_WORDS):
                continue
            # Слишком короткое = бесполезно (вряд ли реальный запрос)
            if len(msg.text.strip()) < 25:
                continue

            sender_username = None
            try:
                if msg.sender and getattr(msg.sender, 'username', None):
                    sender_username = msg.sender.username
            except Exception:
                pass

            link = (
                f"https://t.me/c/{abs(chat.id)}/{msg.id}"
                if str(chat.id).startswith("-100")
                else f"https://t.me/{chat_name.replace('@', '')}/{msg.id}"
            )

            rows_to_insert.append({
                "msg_id": msg.id,
                "chat_key": chat_key,
                "chat_id": chat_id_val,
                "chat_title": chat_title,
                "sender_id": msg.sender_id,
                "sender_username": sender_username,
                "text": msg.text[:4000],
                "link": link,
                "msg_date": msg.date.isoformat(),
            })

        if rows_to_insert:
            saved_count = await save_messages_to_corpus(rows_to_insert)
            if saved_count > 0:
                logger.info(f"✅ {chat_name}: +{saved_count} сообщений в корпус")

        if highest_msg_id > last_msg_id:
            chat_offsets[chat_key] = highest_msg_id
            await update_chat_offset(chat_key, highest_msg_id)

        if not is_test:
            increment_budget(is_new_chat, saved_count)

    except FloodWaitError as e:
        safe_sleep = e.seconds + random.uniform(15, 35)
        logger.warning(f"🛑 FloodWait! Спим {safe_sleep:.0f} сек...")
        await asyncio.sleep(safe_sleep)
    except Exception as e:
        logger.error(f"❌ Ошибка в {chat_name}: {e}")

    return saved_count


# ==========================================
# 📊 ЕЖЕЧАСНАЯ СТАТИСТИКА
# ==========================================
async def get_corpus_stats() -> dict:
    async with aiosqlite.connect(APEX_DB) as db:
        async with db.execute("SELECT COUNT(*) FROM messages_corpus") as cur:
            total = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM messages_corpus WHERE indexed_at >= datetime('now', '-1 hour')"
        ) as cur:
            last_hour = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM messages_corpus WHERE indexed_at >= datetime('now', '-1 day')"
        ) as cur:
            last_day = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM chat_offsets") as cur:
            chats_total = (await cur.fetchone())[0]
    return {"total": total, "last_hour": last_hour, "last_day": last_day, "chats": chats_total}


# ==========================================
# ♾️ ГЛАВНЫЙ ЦИКЛ
# ==========================================
async def main():
    await init_db()
    logger.info(f"🚀 Apex AI-Индексер. Сессия: {SESSION_NAME}")
    await client.start()
    await alert_admin("🚀 <b>Индексер запущен.</b>\nРежим: складывает в apex_ai.db, без AI-скоринга.")

    last_stats_alert = datetime.now(timezone.utc)

    while True:
        utc_hour = datetime.now(timezone.utc).hour
        if 19 <= utc_hour <= 23:
            sleep_duration = random.uniform(7200, 10800)
            logger.info("🌙 Ночной сон...")
            await alert_admin("🌙 Индексер ушёл в ночной сон.")
            await asyncio.sleep(sleep_duration)
            continue

        chat_offsets = await sync_chats_from_db({})
        time_limit = datetime.now(timezone.utc) - timedelta(days=60)

        # ЭТАП 1: Тестовые чаты
        for chat_link in TEST_CHATS:
            try:
                chat = await client.get_entity(chat_link)
                chat_name = f"@{chat.username}" if getattr(chat, 'username', None) else chat_link
                await index_chat(chat, chat_name, chat_offsets, time_limit, is_test=True)
            except Exception as e:
                logger.error(f"Тест-чат {chat_link}: {e}")

        # 🛡️ Дневной лимит
        if not can_process_any_chat():
            now = datetime.now(timezone.utc)
            seconds_to_midnight = ((24 - now.hour) * 3600) - (now.minute * 60) - now.second + random.randint(60, 1800)
            logger.info(f"🛡️ Лимит {MAX_CHATS_PER_DAY}/день исчерпан. Сон {seconds_to_midnight/3600:.1f}ч.")
            await alert_admin(f"🛡️ Суточный лимит выбран. Сон до завтра ({seconds_to_midnight/3600:.1f}ч).")
            await asyncio.sleep(seconds_to_midnight)
            continue

        # ЭТАП 2: Чаты из базы (не больше MAX_CHATS_PER_CYCLE за круг)
        known_chat_keys = list(chat_offsets.keys())
        random.shuffle(known_chat_keys)
        known_chat_keys = known_chat_keys[:MAX_CHATS_PER_CYCLE]
        chats_in_burst = random.randint(3, 5)

        cycle_saved = 0
        for idx, chat_key in enumerate(known_chat_keys, 1):
            if not can_process_any_chat():
                logger.info("🛡️ Лимит дня исчерпан посреди круга.")
                break
            try:
                try:
                    target_peer = int(chat_key)
                except ValueError:
                    target_peer = chat_key
                chat = await client.get_entity(target_peer)
                cycle_saved += await index_chat(chat, chat_key, chat_offsets, time_limit)
            except FloodWaitError as e:
                safe_sleep = e.seconds + random.uniform(15, 35)
                logger.warning(f"🛑 FloodWait! Сон {safe_sleep/60:.1f} мин.")
                await alert_admin(f"⚠️ FloodWait! Сон {safe_sleep/60:.1f} мин.")
                await asyncio.sleep(safe_sleep)
            except ValueError:
                logger.debug(f"Чат {chat_key} не разрешился, пропуск.")
            except Exception as e:
                logger.error(f"Сбой {chat_key}: {e}")
                await asyncio.sleep(random.uniform(1.0, 3.0))

            # Человеческий фактор
            if idx % chats_in_burst == 0:
                if random.random() < 0.30:
                    coffee = random.uniform(600, 1500)
                    logger.info(f"☕ Кофе-брейк: {coffee/60:.1f} мин")
                    await asyncio.sleep(coffee)
                else:
                    scroll = random.uniform(60.0, 180.0)
                    logger.debug(f"📱 Залипание {scroll:.0f} сек")
                    await asyncio.sleep(scroll)
                chats_in_burst = random.randint(2, 4)
            else:
                await asyncio.sleep(random.uniform(25.0, 50.0))

        # Алёрт раз в час
        now = datetime.now(timezone.utc)
        if (now - last_stats_alert).total_seconds() > 3600:
            stats = await get_corpus_stats()
            budget = _load_today_budget()
            await alert_admin(
                f"📊 <b>Корпус:</b> {stats['total']:,} сообщений\n"
                f"➕ За час: {stats['last_hour']} | За сутки: {stats['last_day']}\n"
                f"🗂 Чатов в базе: {stats['chats']}\n"
                f"📈 Бюджет дня: {budget['total_chats']}/{MAX_CHATS_PER_DAY} операций, "
                f"новых: {budget['new_chats']}/{MAX_NEW_CHATS_PER_DAY}"
            )
            last_stats_alert = now

        cycle_sleep = random.uniform(1800, 3000)
        logger.info(f"🛌 Круг: +{cycle_saved} в корпус. Сон {cycle_sleep/60:.1f} мин.")
        await asyncio.sleep(cycle_sleep)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Остановлено пользователем.")
