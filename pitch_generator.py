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

# --- Lead filtering rules --------------------------------------------------
MIN_SUBSCRIBERS = 15_000
MAX_SUBSCRIBERS = 150_000

MIN_ENGLISH_RATIO = 0.85

# ==========================================================================
# SYSTEM PROMPT — живой, не-шаблонный питч от лица реального монтажёра
# ==========================================================================
SYSTEM_PROMPT = """You are Nikita, a short-form video editor. Write a personalized, casual cold email to a YouTube creator.
Goal: sound 100% human-written, friendly, natural. Not like a salesman or a bot.

MANDATORY RULES:
1. GREETING: Use real first name if obvious ("Hey Marc,"). If brand name or unclear — just "Hey,"
2. TONE: Relaxed, conversational, like a quick message from your phone. Lowercase is fine.
   BANNED: "loved your latest video", "super informative", "super insightful", "valuable content", "high-retention", "cinematic"
3. LENGTH: Under 90 words. Get straight to the point.
4. HOOK (CRITICAL): Prove you actually looked at their channel.
   - If video title is specific → reference it or a detail from it
   - If title is generic ("Vlog 45") → mention their niche/topic instead
   - NEVER invent facts about the channel
5. TRANSITION:
   - No shorts / few shorts → "noticed you don't really post shorts — ever thought about cutting some of your long videos into clips?"
   - Shorts exist but weak → "your shorts are a decent start but feel like they could hit harder with tighter pacing"
   - Always mention their product if they have one: "could drive more traffic to [product]"
6. OFFER: "happy to cut a test short for $15 so you can see the style"
7. PORTFOLIO: "here's some of my work: [Portfolio Link]"
8. SIGN OFF: Always end with exactly:
"— Nikita"
"""

X_DM_PROMPT = """You are Nikita, a short-form video editor reaching out to a YouTube creator on X (Twitter).

Write ONE short DM. Rules:
- Max 200 characters total
- No selling, no price, no links
- One specific observation about their shorts or content
- End with a soft open question
- Lowercase, casual, like a real person typed it
- NEVER say "loved your content", "amazing", "valuable"
- NEVER mention "high-retention", "cinematic", "engagement"
- DO mention their product name if they have one
- Prove you actually looked at their channel
"""

def build_user_prompt(lead: dict, portfolio_link: str) -> str:
    """Передаём модели конкретные данные лида для подстановки в шаблон."""
    channel_name = lead.get("channel_name") or "there"
    latest_video_title = lead.get("latest_video_title") or "your latest upload"
    
    long_form_count = lead.get("long_form_count", 0)
    shorts_count = lead.get("shorts_count", 0)
    product_name = lead.get("product_name", "None")
    views_gap = lead.get("views_gap", "normal")
    subs = lead.get("subscriber_count") or 0
    description = (lead.get("description") or "")[:400]
    
    # Explicitly calculate shorts status based on requirements
    if shorts_count <= 2:
        shorts_status = "few or no shorts"
    elif "lagging" in views_gap.lower() or "rare" in views_gap.lower():
        shorts_status = "shorts exist but weak/underperforming (poor editing/retention)"
    else:
        shorts_status = "shorts exist but weak/underperforming (poor editing/retention)"

    text = (
        "Write the cold email for this lead based on the following exact data:\n"
        f"- Channel/Creator Name: {channel_name}\n"
        f"- Subscribers: {subs}\n"
        f"- Niche/Bio preview: {description}\n"
        f"- Latest video title: {latest_video_title}\n"
        f"- Long videos (60 days): {long_form_count}\n"
        f"- Shorts (60 days): {shorts_count}\n"
        f"- Shorts status: {shorts_status}\n"
        f"- Product/SaaS name: {product_name}\n"
        f"- Portfolio Link: {portfolio_link}\n"
    )
    # Add enrichment data if available
    video_summary = lead.get("video_summary", "")
    video_hooks = lead.get("video_hooks", [])
    outreach_angle = lead.get("video_outreach_angle", "")
    if video_summary:
        text += f"- Video summary: {video_summary}\n"
    if video_hooks:
        text += f"- Potential short-form hooks: {', '.join(str(h) for h in video_hooks[:3])}\n"
    if outreach_angle:
        text += f"- Suggested outreach angle: {outreach_angle}\n"
    return text

def _sanitize_pitch(text: str, portfolio_link: str) -> str:
    """Подстраховка от модели:
    - убираем случайные обрамляющие кавычки,
    - ГАРАНТИРУЕМ подстановку реальной ссылки на место {portfolio_link}."""
    text = (text or "").strip().strip('"').strip()
    # dynamic portfolio substitution if LLM used placeholders
    text = text.replace("{portfolio_link}", portfolio_link)
    text = text.replace("{portfolio link}", portfolio_link)
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

def generate_x_dm(lead: dict) -> str:
    """Генерирует короткий X DM под конкретного блогера."""
    channel_name = lead.get("channel_name") or "there"
    latest_video = lead.get("latest_video_title") or ""
    shorts_count = lead.get("shorts_count", 0)
    product_name = lead.get("product_name", "None")
    description = (lead.get("description") or "")[:300]
    shorts_opportunity = lead.get("shorts_opportunity", "unknown")

    if shorts_count <= 2:
        shorts_status = "almost no shorts"
    else:
        shorts_status = "shorts exist but look underedited"

    user_prompt = (
        f"Channel: {channel_name}\n"
        f"Niche/Bio: {description}\n"
        f"Latest video: {latest_video}\n"
        f"Shorts status: {shorts_status}\n"
        f"Product: {product_name}\n"
        f"Shorts opportunity: {shorts_opportunity}\n"
        f"\nWrite a single casual X DM. No greeting needed, just jump in naturally."
    )

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": X_DM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
            max_tokens=80,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.error("X DM generation failed for '%s': %s", channel_name, exc)
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

def save_lead_to_db(lead: dict, pitch: str, status: str, db_path: str = APEX_DB, x_dm: str = None) -> None:
    """Пишет результат обработки лида в outreach_log."""
    try:
        with sqlite3.connect(db_path, timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            import json as _json
            _hooks = lead.get("video_hooks", [])
            _hooks_str = _json.dumps(_hooks) if _hooks else None
            db.execute(
                """
                INSERT INTO outreach_log
                    (channel_id, channel_name, contact_email, pitch, status, created_at,
                     video_summary, video_hooks, video_has_captions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    pitch=excluded.pitch,
                    status=excluded.status,
                    created_at=excluded.created_at,
                    video_summary=excluded.video_summary,
                    video_hooks=excluded.video_hooks,
                    video_has_captions=excluded.video_has_captions
                """,
                (
                    lead.get("channel_id"),
                    lead.get("channel_name"),
                    lead.get("contact_email"),
                    pitch,
                    status,
                    datetime.utcnow().isoformat(),
                    lead.get("video_summary"),
                    _hooks_str,
                    1 if lead.get("video_has_captions") else 0,
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
    """Отправляет HTML-отчёт по обработанному лиду в Telegram."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    emoji = _STATUS_EMOJI.get(status, "•")
    preferred = lead.get("preferred_channel") or "—"
    x_url = lead.get("x_url") or "—"
    x_dm = lead.get("x_dm") or ""

    custom_url = lead.get("custom_url", "")
    channel_id = lead.get("channel_id", "")
    if custom_url:
        channel_link = f"https://youtube.com/{custom_url}"
    elif channel_id:
        channel_link = f"https://youtube.com/channel/{channel_id}"
    else:
        channel_link = ""

    channel_name = lead.get('channel_name') or '—'
    name_part = f'<a href="{channel_link}">{channel_name}</a>' if channel_link else f"<b>{channel_name}</b>"

    text = (
        f"{emoji} <b>Outreach · {status}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📺 {name_part}\n"
        f"👥 <b>Subs:</b> {lead.get('subscriber_count', '?')}\n"
        f"📦 <b>Product:</b> {lead.get('product_name') or '—'}\n"
        f"📬 <b>Preferred:</b> {preferred}\n"
        f"✉️ <b>Email:</b> {lead.get('contact_email') or '—'}\n"
        f"🐦 <b>X:</b> {x_url}\n"
        f"🎬 <b>Last video:</b> {lead.get('latest_video_title') or '—'}\n"
        f"📊 <b>Views:</b> {lead.get('video_views', '—'):,}\n"
        f"📝 <b>Summary:</b> {(lead.get('video_summary') or '—')[:200]}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📝 <b>Email pitch:</b>\n<blockquote>{(pitch or '—')[:500]}</blockquote>"
    )
    if x_dm:
        text += f"\n\n🐦 <b>X DM:</b>\n<blockquote>{x_dm[:300]}</blockquote>"

    try:
        with httpx.Client(timeout=10.0) as cli:
            cli.post(url, json={
                "chat_id": ADMIN_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
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
        # 1) email pitch + X DM — генерируем оба сразу
        pitch = generate_pitch(lead)
        x_dm = generate_x_dm(lead)
        lead["x_dm"] = x_dm  # прокидываем в lead для сохранения и TG-отчёта

        preferred = lead.get("preferred_channel", "none")

        # 2) отправка письма + статус
        if not pitch:
            status = "pitch_failed"
            stats["failed"] += 1
        elif not lead.get("contact_email"):
            status = "no_email"
            stats["skipped"] += 1
            # Сохраняем в manual outreach вместе с X DM
            try:
                import youtube_parser as yp
                lead["x_dm"] = x_dm
                yp.save_manual_social(lead, db_path)
            except Exception as e:
                logger.warning("Failed to save to manual_social_outreach: %s", e)
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
        save_lead_to_db(lead, pitch, status, db_path, x_dm=x_dm)

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
