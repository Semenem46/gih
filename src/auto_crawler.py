import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
import aiosqlite
from openai import AsyncOpenAI
from telethon import TelegramClient
from telethon.tl.functions.messages import GetHistoryRequest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import APEX_DB as _PATHS_APEX_DB, DATA_DIR, PARSER_SESSION as _PATHS_SESSION

try:
    from dotenv import load_dotenv
    load_dotenv(str(Path(__file__).resolve().parent.parent / "src" / "bot" / ".env"))
except ImportError:
    pass

APEX_DB = os.environ.get("APEX_DB") or _PATHS_APEX_DB
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
API_ID = int(os.environ.get('API_ID', '0'))
API_HASH = os.environ.get('API_HASH', '')

session_path = os.environ.get("PARSER_SESSION") or _PATHS_SESSION
for p in DATA_DIR.glob('*.session'):
    if 'venv' not in str(p):
        session_path = str(p).replace('.session', '')
        break

groq_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url='https://api.groq.com/openai/v1')
TG_LINK_RX = re.compile(r'(?:t\.me/|@)([a-zA-Z0-9_]{5,32})')

async def init_tables():
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS target_chats (username TEXT PRIMARY KEY, title TEXT, last_msg_id INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.commit()

async def discover_new_chats():
    print('🔎 Ищу упоминания чатов в базе...')
    usernames = set()
    async with aiosqlite.connect(APEX_DB) as db:
        async with db.execute("SELECT text FROM messages_corpus WHERE text LIKE '%t.me/%' OR text LIKE '%@%' ORDER BY id DESC LIMIT 1000") as cur:
            async for row in cur:
                text = row[0] or ''
                for m in TG_LINK_RX.findall(text):
                    u = m.lower().strip()
                    if u not in ['telegram', 'bot', 'joinchat', 'apex', 'admin', 'channel', 'pro', 'dev']:
                        usernames.add(u)
        clean_usernames = []
        for u in usernames:
            async with db.execute('SELECT 1 FROM target_chats WHERE username = ?', (u,)) as cur:
                if not await cur.fetchone():
                    clean_usernames.append(u)
    return clean_usernames

async def validate_chat(client: TelegramClient, username: str) -> str:
    try:
        history = await client(GetHistoryRequest(peer=username, offset_id=0, offset_date=None, add_offset=0, limit=35, max_id=0, min_id=0, hash=0))
        if not history.messages:
            return 'rejected'
        texts = [msg.message for msg in history.messages if msg.message]
        chat_title = getattr(history.chats[0], 'title', username) if history.chats else username
        prompt = f'You are a B2B Chat Auditor. Analyze the last messages from Telegram chat @{username}.\nDetermine if this is a high-quality active community where real business owners, freelancers, or digital specialists post real jobs/leads, discuss marketing, or look for hire.\nREJECT if it is 100% automated bot spam, crypto signals, adult content, or link dump.\nRespond STRICTLY with JSON format: {{"is_good_b2b_chat": true/false, "reason": "explanation in russian"}}\nMESSAGES: {texts[:15]}'
        res = await groq_client.chat.completions.create(model='llama3-70b-8192', messages=[{'role': 'user', 'content': prompt}], response_format={'type': 'json_object'}, temperature=0.0)
        data = json.loads(res.choices[0].message.content)
        is_good = bool(data.get('is_good_b2b_chat', False))
        print(f'🤖 Groq вердикт для @{username}: {"APPROVE" if is_good else "REJECT"} ({data.get("reason")})')
        return 'approved' if is_good else 'rejected'
    except Exception:
        return 'failed'

async def discovery_loop(client: TelegramClient):
    while True:
        try:
            new_links = await discover_new_chats()
            print(f'📥 Найдено {len(new_links)} новых линков на аудит.')
            for u in new_links[:20]:
                status = await validate_chat(client, u)
                async with aiosqlite.connect(APEX_DB) as db:
                    await db.execute('INSERT OR REPLACE INTO target_chats (username, status) VALUES (?, ?)', (u, status))
                    await db.commit()
                await asyncio.sleep(10)
        except Exception as e:
            print(f'Ошибка поиска: {e}')
        await asyncio.sleep(3600)

async def peeking_scraper_loop(client: TelegramClient):
    while True:
        try:
            async with aiosqlite.connect(APEX_DB) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT username, last_msg_id FROM target_chats WHERE status = 'approved'") as cur:
                    chats = [dict(r) for r in await cur.fetchall()]
            print(f'📡 Обхожу {len(chats)} одобренных B2B-чатов...')
            for c in chats:
                try:
                    history = await client(GetHistoryRequest(peer=c['username'], offset_id=0, offset_date=None, add_offset=0, limit=50, max_id=0, min_id=c['last_msg_id'], hash=0))
                    if not history.messages:
                        continue
                    chat_title = getattr(history.chats[0], 'title', c['username']) if history.chats else c['username']
                    max_id = c['last_msg_id']
                    async with aiosqlite.connect(APEX_DB) as db:
                        for msg in reversed(history.messages):
                            if msg.id > max_id: max_id = msg.id
                            if not msg.message: continue
                            link = f"https://t.me/{c['username']}/{msg.id}"
                            date_str = msg.date.isoformat() if msg.date else datetime.utcnow().isoformat()
                            cur = await db.execute('INSERT OR IGNORE INTO messages_corpus (chat_key, chat_title, text, msg_date, link, indexed_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)', (c['username'], chat_title, msg.message, date_str, link))
                            if cur.lastrowid:
                                try: await db.execute('INSERT OR IGNORE INTO messages_fts (rowid, text) VALUES (?, ?)', (cur.lastrowid, msg.message))
                                except: pass
                        await db.execute('UPDATE target_chats SET last_msg_id = ?, title = ? WHERE username = ?', (max_id, chat_title, c['username']))
                        await db.commit()
                    await asyncio.sleep(5)
                except Exception:
                    continue
        except Exception as e:
            print(f'Ошибка скрапера: {e}')
        await asyncio.sleep(600)

async def main():
    if not API_ID:
        print('❌ ОШИБКА: Ключи API_ID не найдены в .env файле!')
        return
    await init_tables()
    print('🔌 Запуск Telethon клиента...')
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start()
    asyncio.create_task(discovery_loop(client))
    await peeking_scraper_loop(client)

if __name__ == '__main__':
    asyncio.run(main())
