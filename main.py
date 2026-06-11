import argparse
import random

from config import KEYWORDS, APEX_DB, logger
import youtube_parser as yp
import pitch_generator as pg

def pick_batch_size() -> int:
    """Простой троттл: 1-3 лида за запуск (мягкое ограничение нагрузки)."""
    return random.choice([1, 2, 3])

def run_scan() -> None:
    """Режим --scan. Тяжёлый поиск по ключам. Cron: раз в сутки ночью."""
    yp.init_lead_tables(APEX_DB)
    youtube = yp.get_youtube_client()
    yp.scan_and_enqueue(youtube, KEYWORDS, APEX_DB)

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

    if valid_leads:
        logger.info("Processing %d valid leads...", len(valid_leads))
        pg.process_leads(valid_leads, batch_size=len(valid_leads), db_path=APEX_DB)
        for lead in valid_leads:
            yp.mark_queue_status(lead["channel_id"], "done", APEX_DB)
            # Дополнительный вывод для ручного аудита
            print(f"--- AUDIT ---")
            print(f"Channel: {lead['channel_name']}")
            print(f"Thumbnail: {lead.get('thumbnail_url')}")
            print(f"---")

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
