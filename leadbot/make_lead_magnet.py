"""
make_lead_magnet.py — генерация лид-магнита из реальных данных messages_corpus.

Берёт ТОП-32 живых B2B-чата (вручную отобранные категории), подтягивает актуальную
статистику msgs_30d / msgs_7d / last_msg из apex_ai.db и собирает красивую xlsx
в фирменном стиле (navy + золото).

Использование:
    cd /root/leadbot && source venv/bin/activate
    python3 make_lead_magnet.py
    # → /root/leadbot/База_TG-чатов_для_лидгена_2026.xlsx

Опции через переменные окружения:
    APEX_DB=/path/to/apex_ai.db
    OUT=/path/to/output.xlsx
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


# ============================================
#                   КОНФИГ
# ============================================
APEX_DB = os.environ.get("APEX_DB") or str(Path(__file__).resolve().parent / "apex_ai.db")
OUT_FILE = os.environ.get("OUT") or str(Path(__file__).resolve().parent / "База_TG-чатов_для_лидгена_2026.xlsx")

# Стиль (фирменный)
NAVY = "0B2545"
NAVY_DARK = "071A33"
GOLD = "C9A227"
GOLD_LIGHT = "E8C547"
WHITE = "FFFFFF"
LIGHT_BG = "FAF7EC"
GREY_BG = "F5F5F5"
DARK_TEXT = "1F2937"
SUBTLE = "6B7280"

THIN = Side(border_style="thin", color="D1D5DB")
THICK_GOLD = Side(border_style="medium", color=GOLD)
BORDER_ALL = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


# ============================================
#         РУЧНОЙ ОТБОР: 32 ЖИВЫХ B2B-ЧАТА
# ============================================
# Категория → список (chat_username_with_at, описание_для_оффера)

CATEGORIES = {
    "🤝 Бизнес-клубы и нетворкинг": [
        ("@biznes_v_krd", "Бизнес-чат Краснодара. Локальные предприниматели, обмен опытом, заявки от заказчиков на услуги."),
        ("@chatb2bnews", "B2B-новости и обсуждения. Один из самых активных предпринимательских чатов РФ."),
        ("@SharksBz", "Sharks Business — сообщество предпринимателей про масштабирование и сделки."),
        ("@biznesdvigekb", "Бизнес-движ Екатеринбурга. Нетворкинг между владельцами бизнеса."),
        ("@netvork_nn", "Нетворкинг Нижнего Новгорода. Знакомства, партнёрства, заказы услуг."),
        ("@GoNetworkingTLT", "Нетворкинг-сообщество Тольятти. Регулярные мероприятия и обсуждения."),
        ("@franchiserus", "Чат франшиз РФ. Здесь люди ищут IT, маркетинг, юр. сопровождение под открытие точек."),
        ("@bestbizevent", "Анонсы бизнес-событий и нетворкинг-встреч по РФ."),
        ("@ekbclub", "Бизнес-клуб Екатеринбурга. Деловые знакомства и партнёрства."),
        ("@perezagruzka_ekb", "«Перезагрузка» ЕКБ — для собственников и руководителей."),
    ],
    "🌐 Web / SEO / Реклама / Маркетинг": [
        ("@Web_SEO_Hub", "Хаб web-разработки и SEO. Заказчики ищут разработчиков и оптимизаторов."),
        ("@top_marketing_chat", "Топовый чат маркетологов. Здесь напрямую ищут подрядчиков под кампании."),
        ("@SEO_ARBITRAJ_CHAT", "SEO и арбитраж трафика. Обсуждение конкретных проектов и подрядов."),
        ("@WebDev_Plus", "Web-разработчики и заказчики. Запросы на сайты, лендинги, фронт."),
        ("@sochi_ads_chat", "Реклама и маркетинг Сочи. Локальные заказчики ищут digital-исполнителей."),
        ("@yandex1direct", "Чат по Яндекс.Директу. Заказчики и директологи."),
        ("@poisk_reklama", "Поиск рекламных подрядчиков. Прямые заявки от компаний."),
        ("@SeohandmadeChat", "SEO ручной работы, дискуссии и заказы."),
        ("@seochat", "Общий SEO-чат, обмен опытом и подрядами."),
        ("@permanentlinechat", "Линкбилдинг и постоянные ссылки. Ниша Pavel-стека."),
    ],
    "🏗 Строительство и проекты": [
        ("@stroy_proekt_chat", "Строительные проекты. Один из самых активных — здесь ищут подрядчиков на проектирование, макеты, сайты."),
        ("@stroiteliVsyaRF", "Строители всей РФ. Тендеры, подряды, поиск исполнителей."),
        ("@adstender", "Тендеры на рекламные и digital-услуги. Прямые заказчики."),
    ],
    "🏪 E-commerce / WB / OZON": [
        ("@reklama_tovar_wb", "Реклама товаров на Wildberries. Селлеры ищут специалистов по продвижению."),
        ("@wb_ozon_start", "WB и OZON старт. Новички и опытные селлеры — ищут маркетинг и сайты."),
        ("@ya_ecom", "Сообщество Яндекс E-commerce. Селлеры и подрядчики."),
        ("@marketplaces_chat", "Общий чат маркетплейсов. Спрос на дизайн карточек, рекламу, сайты."),
    ],
    "🏙 Городские бизнес-чаты": [
        ("@afichaspb", "Афиши и события СПб. Местный бизнес ищет digital-сопровождение."),
        ("@kazan_chat_ad", "Реклама в Казани. Очень живой, прямые заявки."),
        ("@ChatIrkutska", "Иркутский общий чат. Локальный B2B и услуги."),
        ("@ekb_chat_rus", "Многотемный чат ЕКБ. В том числе бизнес-запросы."),
        ("@newsam063", "Самара (063). Локальный бизнес и активные обсуждения."),
    ],
}

CATEGORY_TO_SHEET = {
    "🤝 Бизнес-клубы и нетворкинг": "🤝 Бизнес и нетворкинг",
    "🌐 Web / SEO / Реклама / Маркетинг": "🌐 Web · SEO · Маркетинг",
    "🏗 Строительство и проекты": "🏗 Строй и проекты",
    "🏪 E-commerce / WB / OZON": "🏪 E-commerce · WB · OZON",
    "🏙 Городские бизнес-чаты": "🏙 Городские чаты",
}


# ============================================
#              СБОР ДАННЫХ ИЗ БД
# ============================================
def load_stats_for_chats(chat_keys: list[str]) -> dict[str, dict]:
    """Возвращает {chat_key: {total, msgs_30d, msgs_7d, last_msg}}."""
    db = sqlite3.connect(APEX_DB)
    cur = db.cursor()
    out: dict[str, dict] = {}

    sql = """
        SELECT
            COUNT(*) AS total_msgs,
            SUM(CASE WHEN indexed_at > datetime('now', '-30 days') THEN 1 ELSE 0 END) AS msgs_30d,
            SUM(CASE WHEN indexed_at > datetime('now', '-7 days')  THEN 1 ELSE 0 END) AS msgs_7d,
            MAX(indexed_at) AS last_msg
        FROM messages_corpus
        WHERE chat_key = ?
    """
    for ck in chat_keys:
        cur.execute(sql, (ck,))
        row = cur.fetchone() or (0, 0, 0, None)
        out[ck] = {
            "total": row[0] or 0,
            "msgs_30d": row[1] or 0,
            "msgs_7d": row[2] or 0,
            "last_msg": row[3] or "—",
        }
    db.close()
    return out


# ============================================
#              ВСПОМОГАТЕЛЬНОЕ
# ============================================
def fmt_date(s: str | None) -> str:
    if not s or s == "—":
        return "—"
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return str(s)[:10]


def cell_link(chat_at: str) -> str:
    """t.me/chat (без @)."""
    return f"https://t.me/{chat_at.lstrip('@')}"


def liveness_emoji(msgs_7d: int, msgs_30d: int) -> str:
    if msgs_7d >= 10:
        return "🔥"
    if msgs_7d >= 3:
        return "🟢"
    if msgs_7d >= 1:
        return "🟡"
    if msgs_30d >= 10:
        return "🟠"
    return "⚪"


# ============================================
#               СТИЛИ
# ============================================
def style_h1(cell):
    cell.font = Font(name="Calibri", size=28, bold=True, color=GOLD)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.fill = PatternFill("solid", fgColor=NAVY)


def style_h2(cell):
    cell.font = Font(name="Calibri", size=14, bold=True, color=WHITE)
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    cell.fill = PatternFill("solid", fgColor=NAVY)


def style_subtitle(cell):
    cell.font = Font(name="Calibri", size=12, italic=True, color=GOLD)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    cell.fill = PatternFill("solid", fgColor=NAVY)


def style_body_navy(cell):
    cell.font = Font(name="Calibri", size=11, color=WHITE)
    cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True, indent=1)
    cell.fill = PatternFill("solid", fgColor=NAVY)


def style_body_light(cell):
    cell.font = Font(name="Calibri", size=11, color=DARK_TEXT)
    cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True, indent=1)
    cell.fill = PatternFill("solid", fgColor=LIGHT_BG)


def style_table_header(cell):
    cell.font = Font(name="Calibri", size=11, bold=True, color=WHITE)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.fill = PatternFill("solid", fgColor=NAVY)
    cell.border = BORDER_ALL


def style_zebra_row(row_cells, even=False):
    fill = PatternFill("solid", fgColor=GREY_BG if even else WHITE)
    for c in row_cells:
        c.font = Font(name="Calibri", size=11, color=DARK_TEXT)
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
        c.fill = fill
        c.border = BORDER_ALL


# ============================================
#         ОБЛОЖКА (лист «🎁 Лид-магнит»)
# ============================================
def build_cover(ws, chat_count: int, total_reach_msgs: int, hot_count: int):
    ws.title = "🎁 Лид-магнит"
    ws.sheet_view.showGridLines = False

    # ширина и высота
    for c in "ABCDEFGHIJ":
        ws.column_dimensions[c].width = 14
    for r in range(1, 50):
        ws.row_dimensions[r].height = 22

    # Большой блок-обложка
    ws.merge_cells("A1:J3")
    c = ws["A1"]
    c.value = "🎯  БАЗА TG-ЧАТОВ\nдля лидгена · 2026"
    style_h1(c)
    ws.row_dimensions[1].height = 50
    ws.row_dimensions[2].height = 50
    ws.row_dimensions[3].height = 50

    ws.merge_cells("A4:J5")
    c = ws["A4"]
    c.value = f"{chat_count} живых B2B-чатов · 5 категорий · отобрано вручную из 1400+"
    style_subtitle(c)
    ws.row_dimensions[4].height = 24
    ws.row_dimensions[5].height = 18

    # Что внутри
    ws.merge_cells("A7:J7")
    style_h2(ws["A7"])
    ws["A7"] = "Что внутри"

    rows_text = [
        f"💎  ТОП-{chat_count} живых чатов с реальной статистикой активности (за 30 дней и 7 дней)",
        f"🔥  {hot_count} прямо сейчас активных чатов (есть посты на этой неделе)",
        "🤝  5 направлений: бизнес-клубы · Web/SEO/реклама · стройка · e-commerce · городские",
        "💬  Готовый шаблон первого сообщения в чат — без бана и спама",
        "⚖️  Правила игры: что делать и чего не делать чтобы не словить блок",
        "📞  Прямой контакт автора: @nikita_Apex",
    ]
    for i, txt in enumerate(rows_text, start=8):
        ws.merge_cells(f"A{i}:J{i}")
        c = ws[f"A{i}"]
        c.value = txt
        style_body_light(c)
        ws.row_dimensions[i].height = 22

    # Цифры (выделенный блок)
    ws.merge_cells("A15:J15")
    style_h2(ws["A15"])
    ws["A15"] = "Свежесть базы"

    ws.merge_cells("A16:C18")
    a = ws["A16"]
    a.value = f"{chat_count}\nживых чата"
    a.font = Font(name="Calibri", size=24, bold=True, color=GOLD)
    a.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    a.fill = PatternFill("solid", fgColor=NAVY)

    ws.merge_cells("D16:F18")
    a = ws["D16"]
    a.value = f"{total_reach_msgs}\nсообщ./30 дней"
    a.font = Font(name="Calibri", size=24, bold=True, color=GOLD)
    a.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    a.fill = PatternFill("solid", fgColor=NAVY)

    ws.merge_cells("G16:J18")
    a = ws["G16"]
    a.value = f"{hot_count}\n🔥 активных сейчас"
    a.font = Font(name="Calibri", size=24, bold=True, color=GOLD)
    a.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    a.fill = PatternFill("solid", fgColor=NAVY)

    for r in (16, 17, 18):
        ws.row_dimensions[r].height = 28

    # Как использовать
    ws.merge_cells("A20:J20")
    style_h2(ws["A20"])
    ws["A20"] = "Как использовать"

    use_steps = [
        "1.  Открой лист «💎 ТОП-N» — это главная таблица со всеми чатами и реальной статистикой.",
        "2.  Зайди в любой чат через колонку «Ссылка» — в Telegram откроется t.me/...",
        "3.  Прочитай 20-30 свежих сообщений → пойми тон и тематику чата.",
        "4.  Открой лист «💬 Шаблон захода» → возьми текст и адаптируй под себя.",
        "5.  Напиши в чат свой пост-знакомство. Не спам, не продажи в лоб — экспертно.",
        "6.  Заявки прилетают в личку в течение 24-72 часов. Дальше — закрытие на созвон.",
    ]
    for i, t in enumerate(use_steps, start=21):
        ws.merge_cells(f"A{i}:J{i}")
        c = ws[f"A{i}"]
        c.value = t
        style_body_light(c)
        ws.row_dimensions[i].height = 22

    # Подпись внизу
    ws.merge_cells("A29:J30")
    c = ws["A29"]
    c.value = "Собрал: Никита · @nikita_Apex · Apex Automation\nБот с горячими лидами 24/7: @apex_lidgen_bot"
    c.font = Font(name="Calibri", size=12, italic=True, color=GOLD)
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[29].height = 28
    ws.row_dimensions[30].height = 28


# ============================================
#         ГЛАВНАЯ ТАБЛИЦА (💎 ТОП-N)
# ============================================
HEADERS = ["#", "Статус", "Чат", "Категория", "Активность 30д", "За 7д", "Последний пост", "Описание", "Ссылка"]


def build_main_sheet(ws, all_rows: list[dict]):
    ws.title = "💎 ТОП-32 живых"
    ws.sheet_view.showGridLines = False

    # Заголовок-баннер
    ws.merge_cells("A1:I2")
    c = ws["A1"]
    c.value = "💎  ТОП-32 живых B2B-чата для лидгена"
    c.font = Font(name="Calibri", size=18, bold=True, color=GOLD)
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 12

    # Header
    header_row = 4
    for col_i, h in enumerate(HEADERS, start=1):
        cell = ws.cell(row=header_row, column=col_i, value=h)
        style_table_header(cell)
    ws.row_dimensions[header_row].height = 32

    # Body
    for i, row in enumerate(all_rows, start=1):
        r = header_row + i
        even = (i % 2 == 0)

        ws.cell(row=r, column=1, value=i)
        ws.cell(row=r, column=2, value=row["status"])
        ws.cell(row=r, column=3, value=row["chat"])
        ws.cell(row=r, column=4, value=row["category"])
        ws.cell(row=r, column=5, value=row["msgs_30d"])
        ws.cell(row=r, column=6, value=row["msgs_7d"])
        ws.cell(row=r, column=7, value=fmt_date(row["last_msg"]))
        ws.cell(row=r, column=8, value=row["desc"])
        link_cell = ws.cell(row=r, column=9, value="t.me/" + row["chat"].lstrip("@"))
        link_cell.hyperlink = cell_link(row["chat"])
        link_cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")

        cells = [ws.cell(row=r, column=c) for c in range(1, 10)]
        style_zebra_row(cells, even=even)
        # выравнивание для числовых
        ws.cell(row=r, column=1).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row=r, column=2).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row=r, column=5).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row=r, column=6).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row=r, column=7).alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[r].height = 38

    # Ширины
    widths = [4, 8, 22, 28, 14, 8, 16, 60, 24]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Заморозка + автофильтр
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    last_row = header_row + len(all_rows)
    ws.auto_filter.ref = f"A{header_row}:I{last_row}"


# ============================================
#         КАТЕГОРИЙНЫЕ ЛИСТЫ
# ============================================
def build_category_sheet(wb, sheet_name: str, rows: list[dict]):
    ws = wb.create_sheet(title=sheet_name)
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:G2")
    c = ws["A1"]
    c.value = sheet_name
    c.font = Font(name="Calibri", size=18, bold=True, color=GOLD)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 12

    headers = ["#", "Статус", "Чат", "30д", "7д", "Описание", "Ссылка"]
    header_row = 4
    for i, h in enumerate(headers, start=1):
        style_table_header(ws.cell(row=header_row, column=i, value=h))
    ws.row_dimensions[header_row].height = 32

    for i, row in enumerate(rows, start=1):
        r = header_row + i
        even = (i % 2 == 0)
        ws.cell(row=r, column=1, value=i)
        ws.cell(row=r, column=2, value=row["status"])
        ws.cell(row=r, column=3, value=row["chat"])
        ws.cell(row=r, column=4, value=row["msgs_30d"])
        ws.cell(row=r, column=5, value=row["msgs_7d"])
        ws.cell(row=r, column=6, value=row["desc"])
        link_cell = ws.cell(row=r, column=7, value="t.me/" + row["chat"].lstrip("@"))
        link_cell.hyperlink = cell_link(row["chat"])
        link_cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")

        cells = [ws.cell(row=r, column=c) for c in range(1, 8)]
        style_zebra_row(cells, even=even)
        ws.cell(row=r, column=1).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row=r, column=2).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row=r, column=4).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row=r, column=5).alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[r].height = 36

    widths = [4, 8, 22, 8, 8, 60, 24]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)


# ============================================
#       ШАБЛОН ЗАХОДА · ПРАВИЛА · КОНТАКТЫ
# ============================================
def build_template_sheet(wb):
    ws = wb.create_sheet(title="💬 Шаблон захода")
    ws.sheet_view.showGridLines = False
    for c in "AB":
        ws.column_dimensions[c].width = 60

    ws.merge_cells("A1:B2")
    c = ws["A1"]
    c.value = "💬  Шаблон первого сообщения в чат"
    c.font = Font(name="Calibri", size=18, bold=True, color=GOLD)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 12

    sections = [
        (
            "🟢  Этап 1 — пост в чат (заход)",
            (
                "Привет, ребят. Я {КТО_ТЫ}, помогаю {ЦА} решать {ПРОБЛЕМА} "
                "через {РЕШЕНИЕ}.\n\n"
                "Заметил что у {аудитория_чата} часто всплывает вопрос про {КОНКРЕТНАЯ_БОЛЬ} — "
                "если кому интересно, готов разобрать на конкретике без воды. "
                "Пишите в личку — отвечу всем.\n\n"
                "P.S. Если админ не разрешает такие посты — снесу, не вопрос."
            ),
        ),
        (
            "🟢  Этап 2 — ответ в личку (когда написали)",
            (
                "Привет! Спасибо что отозвался(-лась).\n\n"
                "Опиши коротко что сейчас на руках:\n"
                "• Что за продукт / услуга\n"
                "• Какой канал лидов сейчас работает (или не работает)\n"
                "• На какую цель идём (заявок в месяц / выручка / другое)\n\n"
                "После этого скажу подходим ли друг другу — за 1 день буду в курсе."
            ),
        ),
        (
            "🟢  Этап 3 — закрытие на созвон",
            (
                "Окей, всё понял. Чтобы дальше говорить предметно — давай 20-минутный "
                "созвон. Я задам ещё пару точечных вопросов и расскажу, как бы я "
                "решал это на твоём месте.\n\n"
                "Удобнее: завтра до обеда (МСК) или после 17:00? Скину Calendly / "
                "просто согласуем время в личке."
            ),
        ),
    ]

    row = 4
    for title, body in sections:
        ws.merge_cells(start_row=row, end_row=row, start_column=1, end_column=2)
        c = ws.cell(row=row, column=1, value=title)
        style_h2(c)
        ws.row_dimensions[row].height = 28

        ws.merge_cells(start_row=row + 1, end_row=row + 1, start_column=1, end_column=2)
        c = ws.cell(row=row + 1, column=1, value=body)
        c.font = Font(name="Calibri", size=11, color=DARK_TEXT)
        c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True, indent=1)
        c.fill = PatternFill("solid", fgColor=LIGHT_BG)
        c.border = BORDER_ALL
        ws.row_dimensions[row + 1].height = 110
        row += 3

    # Финальный блок-напоминание
    ws.merge_cells(start_row=row, end_row=row, start_column=1, end_column=2)
    c = ws.cell(row=row, column=1, value="⚠️  3 правила чтобы не словить бан")
    style_h2(c)
    ws.row_dimensions[row].height = 28

    rules = [
        "1.  Один пост в чат за день. Не спамь — модераторы видят повторы.",
        "2.  Не вставляй ссылки в первом сообщении. Только текст и приглашение в личку.",
        "3.  Перед постом ВСЕГДА читай правила чата (закрепы) — где-то реклама строго запрещена.",
    ]
    for rule in rules:
        row += 1
        ws.merge_cells(start_row=row, end_row=row, start_column=1, end_column=2)
        c = ws.cell(row=row, column=1, value=rule)
        c.font = Font(name="Calibri", size=11, color=DARK_TEXT)
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
        c.fill = PatternFill("solid", fgColor=LIGHT_BG)
        c.border = BORDER_ALL
        ws.row_dimensions[row].height = 24


def build_rules_sheet(wb):
    ws = wb.create_sheet(title="⚖️ Правила игры")
    ws.sheet_view.showGridLines = False
    for c in "AB":
        ws.column_dimensions[c].width = 50

    ws.merge_cells("A1:B2")
    c = ws["A1"]
    c.value = "⚖️  Что делать ✅  /  Что не делать 🚫"
    c.font = Font(name="Calibri", size=18, bold=True, color=GOLD)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 12

    pairs = [
        ("Читать чат 20-30 сообщений ДО публикации", "Кидать пост сходу не разобравшись в тоне"),
        ("Писать как живой человек, своими словами", "Копировать «продающий шаблон» и менять в нём ниши"),
        ("Описывать конкретную пользу для аудитории", "Продавать в лоб «закажи у меня услугу»"),
        ("Отвечать на каждое сообщение в чате как помощь", "Игнорировать чат после своего поста"),
        ("Закрывать на личку и созвон", "Гнать через 5 минут к оплате"),
        ("Делать 1 пост в чат в неделю максимум", "Спамить ежедневно одно и то же"),
        ("Заходить в 5-10 чатов параллельно", "Сидеть в 50 чатах и ничего не делать"),
        ("Если бан — извиняться, удалять пост", "Создавать новый акк и снова заходить в тот же чат"),
    ]

    header_row = 4
    style_table_header(ws.cell(row=header_row, column=1, value="✅  Делай"))
    style_table_header(ws.cell(row=header_row, column=2, value="🚫  Не делай"))
    ws.row_dimensions[header_row].height = 32

    for i, (good, bad) in enumerate(pairs, start=1):
        r = header_row + i
        ws.cell(row=r, column=1, value=good)
        ws.cell(row=r, column=2, value=bad)
        for col in (1, 2):
            cell = ws.cell(row=r, column=col)
            cell.font = Font(name="Calibri", size=11, color=DARK_TEXT)
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
            cell.border = BORDER_ALL
            cell.fill = PatternFill("solid", fgColor=GREY_BG if (i % 2 == 0) else WHITE)
        ws.row_dimensions[r].height = 32


def build_contact_sheet(wb):
    ws = wb.create_sheet(title="📞 Связаться")
    ws.sheet_view.showGridLines = False
    for c in "ABCDEFGH":
        ws.column_dimensions[c].width = 14

    ws.merge_cells("A1:H3")
    c = ws["A1"]
    c.value = "📞  Хочешь систему лидгена под ключ?"
    c.font = Font(name="Calibri", size=24, bold=True, color=GOLD)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[1].height = 30
    ws.row_dimensions[2].height = 30
    ws.row_dimensions[3].height = 30

    blocks = [
        (
            "⚡  Бесплатный 20-минутный разбор",
            "Покажу как именно ТЫ можешь использовать эту базу под свою нишу. "
            "Без впаривания — разбор твоей ситуации и 1-2 конкретных шага.",
        ),
        (
            "🎯  Запуск системы лидгена под ключ",
            "Делаем оффер, тексты, воронку, бота. На выходе — стабильный поток "
            "заявок из TG-чатов и личек, без тестов и воды.",
        ),
        (
            "🤖  Бот, который ловит горячие лиды 24/7",
            "AI-парсер 1400+ Telegram-чатов: каждый день в личку приходят люди, "
            "которые прямо сейчас ищут услугу как у тебя. Под твою нишу, эксклюзивно.",
        ),
    ]

    row = 5
    for title, body in blocks:
        ws.merge_cells(start_row=row, end_row=row, start_column=1, end_column=8)
        c = ws.cell(row=row, column=1, value=title)
        style_h2(c)
        ws.row_dimensions[row].height = 26

        ws.merge_cells(start_row=row + 1, end_row=row + 1, start_column=1, end_column=8)
        c = ws.cell(row=row + 1, column=1, value=body)
        c.font = Font(name="Calibri", size=12, color=DARK_TEXT)
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
        c.fill = PatternFill("solid", fgColor=LIGHT_BG)
        c.border = BORDER_ALL
        ws.row_dimensions[row + 1].height = 50
        row += 3

    # Контакты
    ws.merge_cells(start_row=row, end_row=row, start_column=1, end_column=8)
    c = ws.cell(row=row, column=1, value="✉️  Контакты")
    style_h2(c)
    ws.row_dimensions[row].height = 26
    row += 1

    contacts = [
        ("Telegram", "@nikita_Apex", "https://t.me/nikita_Apex"),
        ("Бот с лидами", "@apex_lidgen_bot", "https://t.me/apex_lidgen_bot"),
        ("Имя", "Никита (Apex Automation)", None),
    ]
    for label, value, url in contacts:
        ws.merge_cells(start_row=row, end_row=row, start_column=1, end_column=2)
        ws.cell(row=row, column=1, value=label).font = Font(bold=True, size=12, color=NAVY)
        ws.cell(row=row, column=1).alignment = Alignment(horizontal="left", indent=1, vertical="center")
        ws.merge_cells(start_row=row, end_row=row, start_column=3, end_column=8)
        cv = ws.cell(row=row, column=3, value=value)
        cv.alignment = Alignment(horizontal="left", indent=1, vertical="center")
        if url:
            cv.hyperlink = url
            cv.font = Font(size=12, color="0563C1", underline="single")
        else:
            cv.font = Font(size=12, color=DARK_TEXT)
        ws.row_dimensions[row].height = 28
        row += 1

    # CTA
    row += 1
    ws.merge_cells(start_row=row, end_row=row + 1, start_column=1, end_column=8)
    c = ws.cell(row=row, column=1, value="📞  Написать напрямую → t.me/nikita_Apex\n🤖  Получать лиды в боте → t.me/apex_lidgen_bot")
    c.font = Font(name="Calibri", size=14, bold=True, color=GOLD)
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c.fill = PatternFill("solid", fgColor=NAVY)
    ws.row_dimensions[row].height = 28
    ws.row_dimensions[row + 1].height = 28


# ============================================
#                  ОСНОВНОЕ
# ============================================
def main():
    # 1. Все ключи чатов
    all_chat_keys: list[str] = []
    for chats in CATEGORIES.values():
        all_chat_keys.extend([c[0] for c in chats])

    # 2. Реальная статистика
    print(f"📂 Читаю статистику по {len(all_chat_keys)} чатам из {APEX_DB}...")
    stats = load_stats_for_chats(all_chat_keys)

    # 3. Сборка строк (с категорией)
    all_rows: list[dict] = []
    rows_by_category: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}

    for cat, chats in CATEGORIES.items():
        for chat_at, desc in chats:
            st = stats.get(chat_at, {"total": 0, "msgs_30d": 0, "msgs_7d": 0, "last_msg": None})
            row = {
                "chat": chat_at,
                "category": cat,
                "desc": desc,
                "msgs_30d": st["msgs_30d"],
                "msgs_7d": st["msgs_7d"],
                "last_msg": st["last_msg"],
                "status": liveness_emoji(st["msgs_7d"], st["msgs_30d"]),
            }
            all_rows.append(row)
            rows_by_category[cat].append(row)

    # Сортируем главную таблицу по горячести (msgs_7d desc, msgs_30d desc)
    all_rows.sort(key=lambda r: (-r["msgs_7d"], -r["msgs_30d"]))

    # 4. Метрики для обложки
    chat_count = len(all_rows)
    total_reach_msgs = sum(r["msgs_30d"] for r in all_rows)
    hot_count = sum(1 for r in all_rows if r["msgs_7d"] >= 1)

    print(f"  ✅ {chat_count} чатов · {total_reach_msgs} сообщ./30д · {hot_count} активны прямо сейчас")

    # 5. Книга
    wb = Workbook()
    cover = wb.active
    build_cover(cover, chat_count, total_reach_msgs, hot_count)

    main_ws = wb.create_sheet(title="💎 ТОП-32 живых")
    build_main_sheet(main_ws, all_rows)

    for cat, rows in rows_by_category.items():
        sheet_name = CATEGORY_TO_SHEET.get(cat, cat)
        # сортируем категорийные по той же логике
        rows_sorted = sorted(rows, key=lambda r: (-r["msgs_7d"], -r["msgs_30d"]))
        build_category_sheet(wb, sheet_name, rows_sorted)

    build_template_sheet(wb)
    build_rules_sheet(wb)
    build_contact_sheet(wb)

    wb.save(OUT_FILE)
    print(f"📦 Сохранено: {OUT_FILE}")
    print(f"   Размер: {os.path.getsize(OUT_FILE):,} байт")


if __name__ == "__main__":
    main()
