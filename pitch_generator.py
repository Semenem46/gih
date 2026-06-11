"""
pitch_generator.py — генератор холодных питчей (Llama 3 / Groq) + cron-safe рассыл.

============================================================================
DNS-НАСТРОЙКИ ДЛЯ ДОМЕНА apex.ai (чтобы письма железно долетали)
============================================================================
Попроси своего админа/DNS-провайдера прописать/проверить 3 TXT-записи.
Значения зависят от того, ЧЕРЕЗ ЧТО ты реально отправляешь (Google Workspace,
Amazon SES, Postmark, SendGrid и т.п.) — ниже шаблоны, подставь свой провайдер.

1) SPF — кто имеет право слать почту от имени apex.ai.
   Тип: TXT | Имя/Host: @ (то есть apex.ai)
   Значение (пример для Google Workspace):
       v=spf1 include:_spf.google.com ~all
   Если шлёшь через несколько сервисов — добавляй include для каждого, напр.:
       v=spf1 include:_spf.google.com include:amazonses.com ~all
   ⚠️ Должна быть РОВНО ОДНА SPF-запись на домен. ~all (softfail) для старта ок.

2) DKIM — криптоподпись писем. Ключ выдаёт твой почтовый провайдер.
   Тип: TXT | Имя/Host: <selector>._domainkey  (селектор даёт провайдер)
       - Google Workspace: google._domainkey.apex.ai
       - Amazon SES: <даёт 3 CNAME-записи вместо TXT — пропиши их как есть>
   Значение: v=DKIM1; k=rsa; p=<публичный_ключ_из_панели_провайдера>
   ⚠️ Ключ генерится в панели отправителя — скопируй оттуда 1-в-1.

3) DMARC — политика, что делать с письмами, не прошедшими SPF/DKIM.
   Тип: TXT | Имя/Host: _dmarc  (то есть _dmarc.apex.ai)
   Значение (безопасный старт — только мониторинг):
       v=DMARC1; p=none; rua=mailto:dmarc@apex.ai; ruf=mailto:dmarc@apex.ai; fo=1; adkim=s; aspf=s
   Когда отчёты покажут, что всё подписывается корректно — ужесточай:
       p=none  ->  p=quarantine  ->  p=reject

Проверить после настройки:
   - https://mxtoolbox.com/spf.aspx , /dkim.aspx , /dmarc.aspx
   - отправь тестовое письмо на check-auth@verifier.port25.com или mail-tester.com
============================================================================
"""

import sqlite3
import smtplib
import random
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo

import httpx
from groq import Groq

from config import (
    GROQ_API_KEY,
    GROQ_MODEL,            # напр. "llama-3.3-70b-versatile"
    PORTFOLIO_LINK,        # ссылка на твоё портфолио/шоурил
    SENDER_NAME,           # имя в поле From
    FROM_EMAIL,            # адрес отправителя
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
    PHYSICAL_ADDRESS,      # почтовый адрес для CAN-SPAM футера
    UNSUBSCRIBE_LINK,      # ссылка/инструкция для отписки (CAN-SPAM)
    APEX_DB,               # путь к sqlite-базе
    BOT_TOKEN,             # токен телеграм-бота для отчётов
    ADMIN_ID,              # chat_id админа для отчётов
    AUTO_SEND_EMAILS,
    REQUIRE_PRODUCTION_PORTFOLIO,
    logger,
)

# Сколько лидов обрабатывать за ОДИН запуск (cron повесим раз в час).
# Так получается естественный, безопасный троттлинг без вечного процесса.
DEFAULT_BATCH_SIZE = 2

groq_client = Groq(api_key=GROQ_API_KEY)

# ==========================================================================
# SYSTEM PROMPT — живой, не-шаблонный питч от лица реального монтажёра
# ==========================================================================
SYSTEM_PROMPT = """you are a 19yo skilled video editor from the us. you are sending a super quick, casual line to a creator because you actually like their stuff.
hard rules:
- strictly lowercase, no caps at all.
- no exclamation marks.
- under 40 words. if it looks like a formal email, you fail.
- NEVER copy the video title verbatim. summarize it in 2-4 words maximum like a human would (e.g., if title is "How to Start a Service Business Step by Step", write "the service business tutorial").

flow:
hey [name], caught your video about [short summary of topic]. long form is dope but you could easily pull millions of views if you chopped it into shorts.

i do high-retention editing, clips look like this: {portfolio_link}

down to try 1 clip with a solid discount just to see the quality? let me know, no pressure"""

def build_user_prompt(lead: dict, portfolio_link: str) -> str:
    """Передаём модели конкретные данные лида для подстановки в шаблон."""
    channel_name = lead.get("channel_name") or "there"
    latest_video_title = lead.get("latest_video_title") or "your latest upload"
    return (
        "write the dm for this lead. remember: summarize the topic in 2-4 words, "
        "do NOT copy the title verbatim.\n"
        f"- creator name / channel: {channel_name}\n"
        f"- latest video title (paraphrase this, never copy it): {latest_video_title}\n"
        f"- portfolio_link: {portfolio_link}\n"
    )

def _sanitize_pitch(text: str, portfolio_link: str) -> str:
    """Подстраховка от модели:
    - принудительный lowercase,
    - убираем восклицательные знаки,
    - срезаем случайные обрамляющие кавычки,
    - ГАРАНТИРУЕМ подстановку реальной ссылки на место {portfolio_link}."""
    text = (text or "").strip().strip('"').strip()
    text = text.replace("!", ".")
    text = text.lower()
    # динамическая подстановка PORTFOLIO_LINK из config:
    text = text.replace("{portfolio_link}", portfolio_link)
    text = text.replace("{portfolio link}", portfolio_link)
    # если модель потеряла ссылку вовсе — дописываем её.
    if portfolio_link.lower() not in text:
        text = f"{text}\nclips look like this: {portfolio_link}"
    return text

def generate_pitch(lead: dict, portfolio_link: str = PORTFOLIO_LINK) -> str:
    """Генерирует короткий питч под конкретного блогера через Llama 3 (Groq)."""
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(lead, portfolio_link)},
            ],
            temperature=0.85,   # выше → меньше шаблонности, живее текст
            max_tokens=160,
        )
        pitch = response.choices[0].message.content
        return _sanitize_pitch(pitch, portfolio_link)
    except Exception as exc:
        logger.error("Pitch generation failed for '%s': %s", lead.get("channel_name"), exc)
        return ""

# ==========================================================================
# ОТПРАВКА ПИСЬМА (с обязательным CAN-SPAM футером)
# ==========================================================================
def email_sending_enabled() -> bool:
    return False


# ==========================================================================
# АНТИ-СПАМ ЗАЩИТА: US Business Hours + Рваный лимит + Time Jitter
# ==========================================================================
_US_EASTERN = ZoneInfo("America/New_York")
_US_PACIFIC = ZoneInfo("America/Los_Angeles")

# Рваный часовой график: динамический лимит писем в час (имитация человека)
_HOURLY_LIMITS = [1, 3, 0, 2, 1, 0, 3, 2, 1, 0, 2, 1]


def _is_us_business_hours() -> bool:
    """Проверяет, попадаем ли в рабочие часы US (8:00-18:00 EST или PST)."""
    now_est = datetime.now(_US_EASTERN)
    now_pst = datetime.now(_US_PACIFIC)
    # Считаем рабочим временем если хотя бы в одном часовом поясе 8-18
    est_ok = 8 <= now_est.hour < 18 and now_est.weekday() < 5  # Пн-Пт
    pst_ok = 8 <= now_pst.hour < 18 and now_pst.weekday() < 5
    return est_ok or pst_ok


def _get_hourly_send_limit() -> int:
    """Рваный лимит: разное кол-во писем каждый час (1, 3, 0, 2, ...)."""
    hour = datetime.now(_US_EASTERN).hour
    return _HOURLY_LIMITS[hour % len(_HOURLY_LIMITS)]


def _apply_jitter() -> None:
    """Рандомная задержка 15-90 сек между отправками (имитация человека)."""
    delay = random.uniform(15, 90)
    logger.info("Jitter delay: %.1f sec before next email", delay)
    time.sleep(delay)


def should_send_email_now() -> bool:
    """Проверяет все условия антиспама перед отправкой.
    Возвращает True только если:
    1. Сейчас рабочие часы в US
    2. Часовой лимит > 0
    """
    if not _is_us_business_hours():
        logger.info("Outside US business hours — email deferred")
        return False
    limit = _get_hourly_send_limit()
    if limit == 0:
        logger.info("Hourly limit = 0 (cool-down hour) — email deferred")
        return False
    return True

def _build_email_body(pitch: str) -> str:
    """Тело письма: питч + минимальный compliance-футер (US CAN-SPAM)."""
    footer = (
        "\n\n---\n"
        f"{SENDER_NAME} · {PHYSICAL_ADDRESS}\n"
        f"not interested? just reply 'stop' or unsubscribe here: {UNSUBSCRIBE_LINK}"
    )
    return f"{pitch}{footer}"

def send_email(lead: dict, pitch: str) -> bool:
    """Отправляет одно письмо. Возвращает True при успехе."""
    to_email = lead.get("contact_email")
    if not to_email:
        logger.info("Skip '%s': no contact_email", lead.get("channel_name"))
        return False
    if not pitch:
        logger.info("Skip '%s': empty pitch", lead.get("channel_name"))
        return False

    if REQUIRE_PRODUCTION_PORTFOLIO and not is_production_portfolio_link(PORTFOLIO_LINK):
        logger.error("Send blocked: portfolio link is not production-ready")
        return False

    if not email_sending_enabled():
        logger.warning("Email sending is hard-disabled in code; skipping send for '%s'", lead.get("channel_name"))
        return False

    msg = EmailMessage()
    msg.set_content(_build_email_body(pitch))
    msg["Subject"] = "quick idea for your shorts"
    msg["From"] = formataddr((SENDER_NAME, FROM_EMAIL))
    msg["To"] = to_email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("SENT to '%s' <%s>", lead.get("channel_name"), to_email)
        return True
    except Exception as exc:
        logger.error("Send failed for '%s' <%s>: %s", lead.get("channel_name"), to_email, exc)
        return False

# ==========================================================================
# СОХРАНЕНИЕ В БД
# ==========================================================================
def init_outreach_table(db_path: str = APEX_DB) -> None:
    """Idempotent-создание таблицы лога аутрича."""
    with sqlite3.connect(db_path, timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS outreach_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id      TEXT,
                channel_name    TEXT,
                contact_email   TEXT,
                pitch           TEXT,
                status          TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel_id)
            )
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_log(status)"
        )
        db.commit()

def get_processed_channel_ids(db_path: str = APEX_DB) -> set:
    """Возвращает channel_id, которые уже в базе outreach_log."""
    with sqlite3.connect(db_path, timeout=30) as db:
        cur = db.execute("SELECT channel_id FROM outreach_log")
        return {row[0] for row in cur.fetchall() if row[0]}

def save_lead_to_db(lead: dict, pitch: str, status: str, db_path: str = APEX_DB) -> None:
    """Пишет результат обработки лида в outreach_log."""
    try:
        with sqlite3.connect(db_path, timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                INSERT INTO outreach_log
                    (channel_id, channel_name, contact_email, pitch, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    pitch=excluded.pitch,
                    status=excluded.status,
                    created_at=excluded.created_at
                """,
                (
                    lead.get("channel_id"),
                    lead.get("channel_name"),
                    lead.get("contact_email"),
                    pitch,
                    status,
                    datetime.utcnow().isoformat(),
                ),
            )
            db.commit()
    except Exception as exc:
        logger.error("DB save failed for '%s': %s", lead.get("channel_name"), exc)

# ==========================================================================
# КРАСИВЫЙ HTML-ОТЧЁТ В TELEGRAM
# ==========================================================================
_STATUS_EMOJI = {
    "sent": "✅",
    "no_email": "📭",
    "send_failed": "⚠️",
    "pitch_failed": "❌",
    "pending_review": "⏳",
}

def send_telegram_report(lead: dict, pitch: str, status: str) -> None:
    """Отправляет аккуратный HTML-отчёт по обработанному лиду админу в Telegram."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    emoji = _STATUS_EMOJI.get(status, "•")
    socials = lead.get("social_links") or []
    socials_str = ", ".join(socials) if socials else "—"

    text = (
        f"{emoji} <b>Outreach · {status}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📺 <b>{lead.get('channel_name') or '—'}</b>\n"
        f"👥 <b>Subs:</b> {lead.get('subscriber_count', '?')}\n"
        f"✉️ <b>Email:</b> {lead.get('contact_email') or '—'}\n"
        f"🔗 <b>Socials:</b> {socials_str}\n"
        f"🎬 <b>Last video:</b> {lead.get('latest_video_title') or '—'}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📝 <b>Pitch:</b>\n<blockquote>{(pitch or '—')[:600]}</blockquote>"
    )
    try:
        with httpx.Client(timeout=10.0) as cli:
            cli.post(
                url,
                json={
                    "chat_id": ADMIN_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        logger.warning("Telegram report failed for '%s': %s", lead.get("channel_name"), exc)

# ==========================================================================
# CRON-SAFE ОБРАБОТКА: берём batch_size новых лидов, шлём, выходим
# ==========================================================================
def process_leads(leads: list, batch_size: int = DEFAULT_BATCH_SIZE,
                  db_path: str = APEX_DB) -> dict:
    """Один запуск (вешается на cron, напр. раз в час):
       1) инициализирует БД,
       2) отбирает до batch_size ещё НЕ обработанных лидов,
       3) для каждого: генерит питч -> шлёт письмо -> пишет в БД -> репорт в TG,
       4) ЗАВЕРШАЕТСЯ. Никакого вечного sleep — троттлинг делает сам cron.
    """
    init_outreach_table(db_path)

    batch = leads[:batch_size]

    stats = {"sent": 0, "skipped": 0, "failed": 0, "picked": len(batch)}

    if not batch:
        logger.info("No new leads to process. Exiting cleanly.")
        return stats

    logger.info("Processing batch of %d lead(s).", len(batch))

    for lead in batch:
        # 1) генерация питча (с подстановкой PORTFOLIO_LINK)
        pitch = generate_pitch(lead)

        # 2) отправка письма + статус
        if not pitch:
            status = "pitch_failed"
            stats["failed"] += 1
        elif not lead.get("contact_email"):
            status = "no_email"
            stats["skipped"] += 1
        else:
            if AUTO_SEND_EMAILS:
                if not is_production_portfolio_link(PORTFOLIO_LINK):
                    status = "pending_review"
                    stats["skipped"] += 1
                elif not should_send_email_now():
                    status = "pending_review"
                    stats["skipped"] += 1
                else:
                    _apply_jitter()
                    ok = send_email(lead, pitch)
                    status = "sent" if ok else "send_failed"
                    if ok:
                        stats["sent"] += 1
                    else:
                        if not email_sending_enabled():
                            status = "pending_review"
                            stats["skipped"] += 1
                        else:
                            stats["failed"] += 1
            else:
                status = "pending_review"
                stats["skipped"] += 1

        # 3) сохранение лида в БД
        save_lead_to_db(lead, pitch, status, db_path)

        # 4) HTML-отчёт в Telegram
        send_telegram_report(lead, pitch, status)

    return stats

def is_production_portfolio_link(portfolio_link: str) -> bool:
    """Проверяет, является ли ссылка на портфолио реальной (не placeholder/shortlink)."""
    if not portfolio_link:
        return False
    shortlink_domains = {"clck.ru", "bit.ly", "tinyurl.com", "placeholder", "example.com"}
    if any(portfolio_link.startswith(d) for d in shortlink_domains):
        return False
    if len(portfolio_link) < 20:
        return False
    return True
