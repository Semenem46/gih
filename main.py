import argparse
import random
import time
import sys
import os
import fcntl
import socket
from datetime import datetime
from zoneinfo import ZoneInfo

# Prevent any network request (e.g. from httplib2) from hanging forever
socket.setdefaulttimeout(60)

from config import KEYWORDS, APEX_DB, logger, send_telegram_notification
import youtube_parser as yp
import pitch_generator as pg


def pick_batch_size() -> int:
    """Простой троттл: 60 лидов за запуск (боевой режим)."""
    return 60


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
    preferred_channel = lead.get("preferred_channel", "none")
    x_url = lead.get("x_url", "")
    instagram = lead.get("instagram_url", "")
    website = lead.get("website_url", "")

    if custom_url:
        channel_link = f"https://youtube.com/{custom_url}"
    else:
        channel_link = f"https://youtube.com/channel/{channel_id}"

    latest_video_id = lead.get("latest_video_id", "")
    video_link = f"https://youtube.com/watch?v={latest_video_id}" if latest_video_id else f"{channel_link}/videos"

    lines = [
        f"🎯 НОВЫЙ СТУДИЙНЫЙ ЛИД",
        f"",
        f"📺 Канал: {name}",
        f"🔗 YouTube: {channel_link}",
        f"👥 Подписчики: {subs:,}",
        f"📬 Preferred: {preferred_channel}",
        f"",
        f"🎬 Последнее видео: {latest_title}",
        f"▶️ Смотреть: {video_link}",
        f"🖼 Превью: {thumbnail_url}",
        f"",
    ]

    if instagram:
        lines.append(f"📸 Instagram: {instagram}")
    if x_url:
        lines.append(f"🐦 Twitter/X: {x_url}")
    if website:
        lines.append(f"🌐 Website: {website}")
    if email:
        lines.append(f"📧 Email: {email}")

    lines.append("")
    lines.append(f"🤖 Vision AI: {vision_score}")
    lines.append("✅ Confidence: PASSED (≥70)")

    return "\n".join(lines)


def is_working_hours() -> bool:
    """
    Checks if current time is within working hours (Krasnoyarsk: 15:00 - 23:00).
    """
    tz = ZoneInfo("Asia/Krasnoyarsk")
    now = datetime.now(tz)
    return 15 <= now.hour < 23


def run_scan() -> None:
    """Режим --scan. Тяжёлый поиск по ключам. Cron: раз в сутки ночью."""
    send_telegram_notification("🔍 Сканирование запущено...")
    yp.init_lead_tables(APEX_DB)
    
    global youtube
    youtube = yp.get_youtube_client()
    
    count = 0
    errors = 0
    try:
        # To protect quota, pick 15 random keywords per scan
        # 10 kws * 2 queries * 100 quota = 3,000 quota per scan
        sampled_keywords = random.sample(KEYWORDS, min(10, len(KEYWORDS)))
        for kw in sampled_keywords:

            logger.info("Scanning keyword: %s", kw)
            while True:
                try:
                    new_leads = yp.search_channels(youtube, kw)
                    kw_count = 0
                    for cand in new_leads:
                        cid = cand["channel_id"]
                        if yp.is_known_channel(cid, APEX_DB):
                            continue
                        yp.enqueue_channel(cid, cand.get("channel_name"), kw, APEX_DB)
                        count += 1
                        kw_count += 1
                    logger.info("Found %d new leads for keyword '%s'", kw_count, kw)
                    break 
                except yp.QuotaExceededError:
                    if yp.rotate_youtube_key():
                        youtube = yp.get_youtube_client()
                        continue
                    else:
                        send_telegram_notification("⚠️ Сканирование прервано: все квоты YouTube API исчерпаны.")
                        send_telegram_notification(f"✅ Найдено и добавлено {count} новых каналов.")
                        return
                except Exception as kw_e:
                    logger.error("Error scanning keyword '%s': %s", kw, kw_e)
                    errors += 1
                    break # Move to next keyword
        
        msg = f"✅ Сканирование завершено: найдено {count} новых каналов."
        if errors > 0:
            msg += f" (Ошибок по ключам: {errors})"
        send_telegram_notification(msg)
    except Exception as e:
        logger.exception("Global scan failure")
        send_telegram_notification(f"❌ Критическая ошибка при сканировании: {e}")


def run_process(batch_size: int = None, bypass_hours: bool = False) -> bool:
    """Режим --process (по умолчанию). НЕ ходит в дорогой search.list.
    Берёт N pending-лидов из очереди, валидирует,
    отбраковку -> в блеклист, прошедших -> питч + отправка."""
    if not bypass_hours and not is_working_hours():
        logger.info("Bot is sleeping according to Krasnoyarsk schedule.")
        return False

    if batch_size is None:
        batch_size = pick_batch_size()

    yp.init_lead_tables(APEX_DB)
    pg.init_outreach_table(APEX_DB)

    logger.info("Process run starting with batch_size=%d", batch_size)
    
    pending = yp.fetch_pending_leads(batch_size, APEX_DB)
    if not pending:
        logger.info("Queue empty. Nothing to process.")
        return False

    send_telegram_notification(f"⚙️ Обработка запущена (batch={batch_size})...")

    global youtube
    youtube = yp.get_youtube_client()
    
    stats = {"valid": 0, "rejected": 0, "errors": 0}
    rejection_reasons = {}
    processed_count = 0

    # Оптимизация: получаем детали всех каналов одним пахом (Step 1)
    channel_ids = [row["channel_id"] for row in pending]
    all_channel_details = {}
    
    # Try to fetch channel details, handle potential QuotaExceededError and rotate keys if necessary
    while True:
        try:
            all_channel_details = yp.get_channel_details(youtube, channel_ids)
            break
        except yp.QuotaExceededError:
            if yp.rotate_youtube_key():
                youtube = yp.get_youtube_client()
                continue
            else:
                send_telegram_notification("⚠️ Обработка прервана: все квоты YouTube API исчерпаны (на этапе батч-запроса).")
                return
        except Exception as e:
            logger.error("Failed to fetch channel details in batch: %s", e)
            break
    
    for row in pending:
        cid = row["channel_id"]
        cname = row.get("channel_name") or cid
        processed_count += 1

        try:
            ch_detail = all_channel_details.get(cid)
            validated_lead = None
            
            # 1. Validation Step (with internal rotation logic)
            while True:
                try:
                    # Передаем уже полученные детали, чтобы не делать лишний API call
                    lead, reason = yp.validate_lead(youtube, cid, APEX_DB, channel_detail=ch_detail)
                    if lead is None:
                        logger.info("Dropped '%s': %s", cname, reason)
                        if reason.startswith("pending_error"):
                            yp.mark_queue_status(cid, "pending_error", APEX_DB, reason=reason)
                            stats["errors"] += 1
                        else:
                            yp.add_to_blacklist(cid, reason, APEX_DB)
                            yp.mark_queue_status(cid, "rejected", APEX_DB, reason=reason)
                            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                            stats["rejected"] += 1
                        
                        # Smart jitter: skip sleep for cheap steps (step1-3), short sleep for API steps
                        if reason and any(reason.startswith(p) for p in ("step4", "step5", "channel_not_found")):
                            delay = random.uniform(3, 7)
                            logger.info("API-step rejection jitter: sleeping for %.1f seconds...", delay)
                            time.sleep(delay)
                        else:
                            # step1/step2/step3 = free local checks, no API call, no sleep needed
                            pass
                    else:
                        validated_lead = lead
                        yp.mark_queue_status(cid, "validated", APEX_DB)
                        stats["valid"] += 1
                        
                        # Enrichment: free captions + summary
                        try:
                            validated_lead = yp.enrich_lead_video(validated_lead)
                        except Exception as enrich_e:
                            logger.warning("Enrichment failed for '%s': %s (continuing without)", cname, enrich_e)
                        
                        # Instant notification
                        tg_message = format_lead_telegram(lead)
                        send_telegram_notification(tg_message)
                        logger.info("📱 Telegram notification sent for '%s'", lead["channel_name"])
                        
                        # 2. Outreach Step (Pitch & Email) - immediate processing for robustness
                        try:
                            logger.info("Generating pitch and processing outreach for '%s'", cname)
                            pg_stats = pg.process_leads([validated_lead], batch_size=1, db_path=APEX_DB)
                            yp.mark_queue_status(cid, "done", APEX_DB)
                        except Exception as pg_e:
                            logger.error("Failed to process outreach for '%s': %s", cname, pg_e)

                        # ЖИТТЕР только после УСПЕШНОГО лида, чтобы не палиться перед YouTube
                        delay = random.uniform(45, 80)
                        logger.info("Success jitter: sleeping for %.1f seconds...", delay)
                        time.sleep(delay)

                    break
                except yp.QuotaExceededError:
                    if yp.rotate_youtube_key():
                        youtube = yp.get_youtube_client()
                        continue
                    else:
                        send_telegram_notification("⚠️ Обработка прервана: все квоты YouTube API исчерпаны.")
                        _send_summary_stats(stats, rejection_reasons)
                        return
                except Exception as inner_e:
                    logger.error("Error during validation/processing for lead '%s': %s", cname, inner_e)
                    yp.mark_queue_status(cid, "error", APEX_DB, reason=str(inner_e))
                    stats["errors"] += 1
                    break

            # Periodic progress update
            if processed_count % 10 == 0:
                send_telegram_notification(f"📊 Прогресс: обработано {processed_count}/{len(pending)}...")

        except Exception as e:
            logger.exception("Fatal error processing lead %s", cname)
            stats["errors"] += 1
            continue

    _send_summary_stats(stats, rejection_reasons)
    return True


def _send_summary_stats(stats, rejection_reasons):
    summary_lines = [f"🏁 Обработка завершена:"]
    summary_lines.append(f"✅ Валидно: {stats['valid']}")
    if stats['rejected'] > 0:
        summary_lines.append(f"❌ Отсеяно: {stats['rejected']}")
        for r, count in rejection_reasons.items():
            summary_lines.append(f"   └ {r}: {count}")
    if stats['errors'] > 0:
        summary_lines.append(f"⚠️ Ошибок: {stats['errors']}")

    send_telegram_notification("\n".join(summary_lines))


def run_daemon(batch_size: int = 60):
    """
    Бесконечный цикл-воркер. Проверяет очередь раз в 15-30 минут в рабочее время.
    Если в очереди ещё много лидов, переходит к следующей пачке быстрее.
    """
    logger.info("🚀 Leadbot DAEMON started (Working Hours Mode: 15:00-23:00 KRAT)")
    send_telegram_notification("🚀 Leadbot запущен и готов к работе (График: 15:00-23:00 KRAT).")
    
    last_scan_date = None
    queue_was_empty = False

    while True:
        try:
            tz = ZoneInfo("Asia/Krasnoyarsk")
            now = datetime.now(tz)
            today_str = now.strftime("%Y-%m-%d")
            hour = now.hour

            # 1. Scheduled Scans (every hour during work hours)
            scan_hours = list(range(15, 24)) + [0, 1]
            current_scan_slot = (today_str, hour)
            
            if hour in scan_hours and last_scan_date != current_scan_slot:
                logger.info("⏰ Scheduled scan time reached (%d:00 KRAT).", hour)
                try:
                    run_scan()
                    last_scan_date = current_scan_slot
                except Exception as scan_e:
                    logger.error("Error during scheduled scan: %s", scan_e)

            # 2. Regular lead processing
            if is_working_hours():
                logger.info("Starting processing batch...")
                pending_count = yp.get_pending_count(APEX_DB)
                
                if pending_count > 0:
                    logger.info("Queue has %d leads. Starting run_process.", pending_count)
                    # run_process возвращает True если была попытка обработки
                    run_process(batch_size=batch_size)
                    queue_was_empty = False
                    
                    # Если после обработки в очереди ВСЁ ЕЩЁ есть лиды, не спим долго!
                    remaining = yp.get_pending_count(APEX_DB)
                    if remaining > 0:
                        wait_time = random.randint(60, 180) # 1-3 минуты перекура и в бой
                        logger.info("Queue still has %d leads. Quick break: %ds", remaining, wait_time)
                        time.sleep(wait_time)
                        continue # Сразу на следующий круг
                else:
                    if not queue_was_empty:
                        logger.info("Queue is empty.")
                        send_telegram_notification("ℹ️ Очередь пуста. Все найденные лиды обработаны. Бот ждет новых находок.")
                        queue_was_empty = True
                
                # Если очередь пуста, спим стандартно
                wait_time = random.randint(900, 1800) 
                logger.info(f"Idle. Waiting {wait_time}s until next check...")
                time.sleep(wait_time)
            else:
                # В нерабочее время спим дольше
                logger.info("Outside working hours. Sleeping for 30 minutes...")
                time.sleep(1800)
        except Exception as e:
            logger.exception("Daemon loop encountered an error")
            send_telegram_notification(f"❌ КРИТИЧЕСКИЙ СБОЙ: {e}. Перезапуск...")
            time.sleep(2)
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apex leadbot orchestrator")
    parser.add_argument("--scan", action="store_true",
                        help="Ночной тяжёлый поиск по ключам -> raw_leads_queue")
    parser.add_argument("--process", action="store_true",
                        help="Часовой воркер: валидирует N лидов из очереди, шлёт питчи")
    parser.add_argument("--batch", type=int, default=60,
                        help="Жёстко задать размер порции. Если не указан — 60.")
    parser.add_argument("--daemon", action="store_true",
                        help="Запустить в режиме фонового воркера (рекомендуется для стабильности)")
    args = parser.parse_args()

    # Lock file to prevent multiple instances
    lock_name = "scan" if args.scan else "daemon"
    lock_file = f"/tmp/apex_leadbot_{lock_name}.lock"
    
    try:
        f = open(lock_file, 'a')
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print(f"Another instance of Leadbot ({lock_name}) is already running. Exiting.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to acquire lock: {e}")

    if args.scan:
        run_scan()
    elif args.daemon:
        # Дефолт 60 для демона, чтобы выходить на 50-60 лидов/час
        batch = args.batch if args.batch != 60 else 60
        run_daemon(batch)
    else:
        # For manual single runs, bypass the hour check
        run_process(args.batch, bypass_hours=True)


if __name__ == "__main__":
    main()
