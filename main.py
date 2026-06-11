import argparse
import random

from config import KEYWORDS, APEX_DB, logger, send_telegram_notification
import youtube_parser as yp
import pitch_generator as pg


def pick_batch_size() -> int:
    """Простой троттл: 1-3 лида за запуск (мягкое ограничение нагрузки)."""
    return random.choice([1, 2, 3])


def format_lead_telegram(lead: dict) -> str:
    """Формирует красивое структурированное Telegram-сообщение для одобренного лида."""
    name = lead.get("channel_name", "Unknown")
    custom_url = lead.get("custom_url", "")
    channel_id = lead.get("channel_id", "")
    subs = lead.get("subscriber_count", 0)
    latest_title = lead.get("latest_video_title", "")
    thumbnail_url = lead.get("thumbnail_url", "")
    vision_score = lead.get("vision_score", "N/A")
    socials = lead.get("social_links", [])
    email = lead.get("contact_email", "")

    # Ссылка на канал
    if custom_url:
        channel_link = f"https://youtube.com/{custom_url}"
    else:
        channel_link = f"https://youtube.com/channel/{channel_id}"

    # Прямая ссылка на последнее видео
    latest_video_id = lead.get("latest_video_id", "")
    video_link = f"https://youtube.com/watch?v={latest_video_id}" if latest_video_id else f"{channel_link}/videos"

    # Выделяем Instagram и Twitter/X
    instagram = ""
    twitter = ""
    for s in socials:
        s_lower = s.lower()
        if "instagram.com" in s_lower and not instagram:
            instagram = s
        elif ("twitter.com" in s_lower or "x.com" in s_lower) and not twitter:
            twitter = s

    lines = [
        f"🎯 НОВЫЙ СТУДИЙНЫЙ ЛИД",
        f"",
        f"📺 Канал: {name}",
        f"🔗 YouTube: {channel_link}",
        f"👥 Подписчики: {subs:,}",
        f"",
        f"🎬 Последнее видео: {latest_title}",
        f"▶️ Смотреть: {video_link}",
        f"🖼 Превью: {thumbnail_url}",
        f"",
    ]

    if instagram:
        lines.append(f"📸 Instagram: {instagram}")
    if twitter:
        lines.append(f"🐦 Twitter/X: {twitter}")
    if email:
        lines.append(f"📧 Email: {email}")

    lines.append(f"")
    lines.append(f"🤖 Vision AI: {vision_score}")
    lines.append(f"✅ Confidence: PASSED (≥70)")

    return "\n".join(lines)


def run_scan() -> None:
    """Режим --scan. Тяжёлый поиск по ключам. Cron: раз в сутки ночью."""
    yp.init_lead_tables(APEX_DB)
    youtube = yp.get_youtube_client()
    count = yp.scan_and_enqueue(youtube, KEYWORDS, APEX_DB)
    send_telegram_notification(f"🔍 Scan завершён: найдено и добавлено {count} новых каналов в очередь.")


def run_process(batch_size: int = None) -> None:
    """Режим --process (по умолчанию). НЕ ходит в дорогой search.list.
    Берёт N pending-лидов из очереди, валидирует,
    отбраковку -> в блеклист, прошедших -> питч + отправка."""
    if batch_size is None:
        batch_size = pick_batch_size()

    yp.init_lead_tables(APEX_DB)
    pg.init_outreach_table(APEX_DB)

    logger.info("Process run starting with batch_size=%d", batch_size)

    pending = yp.fetch_pending_leads(batch_size, APEX_DB)
    if not pending:
        logger.info("Queue empty. Nothing to process.")
        return

    youtube = yp.get_youtube_client()
    valid_leads = []

    for row in pending:
        cid = row["channel_id"]
        lead, reason = yp.validate_lead(youtube, cid, APEX_DB)
        if lead is None:
            logger.info("Dropped '%s': %s", row.get("channel_name") or cid, reason)
            yp.add_to_blacklist(cid, reason, APEX_DB)
            yp.mark_queue_status(cid, "rejected", APEX_DB)
        else:
            valid_leads.append(lead)
            yp.mark_queue_status(cid, "validated", APEX_DB)

            # Мгновенное уведомление в Telegram при прохождении Vision AI
            tg_message = format_lead_telegram(lead)
            send_telegram_notification(tg_message)
            logger.info("📱 Telegram notification sent for '%s'", lead["channel_name"])

    if valid_leads:
        logger.info("Processing %d valid leads...", len(valid_leads))
        pg.process_leads(valid_leads, batch_size=len(valid_leads), db_path=APEX_DB)
        for lead in valid_leads:
            yp.mark_queue_status(lead["channel_id"], "done", APEX_DB)

    logger.info(
        "Process run finished. validated=%d of %d pending pulled.",
        len(valid_leads), len(pending),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Apex leadbot orchestrator")
    parser.add_argument("--scan", action="store_true",
                        help="Ночной тяжёлый поиск по ключам -> raw_leads_queue")
    parser.add_argument("--process", action="store_true",
                        help="Часовой воркер: валидирует N лидов из очереди, шлёт питчи")
    parser.add_argument("--batch", type=int, default=None,
                        help="Жёстко задать размер порции. Если не указан — random 1-3.")
    args = parser.parse_args()

    if args.scan:
        run_scan()
    else:
        run_process(args.batch)


if __name__ == "__main__":
    main()
