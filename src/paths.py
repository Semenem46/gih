"""
paths.py — единый конфиг путей проекта.

Все скрипты импортируют пути отсюда. Работает и из src/, и из src/bot/.
Структура:
    PROJECT_ROOT/
    ├── src/          ← Python-код
    ├── data/         ← БД, сессии, JSON-стейт
    ├── logs/         ← логи
    ├── tests/        ← тесты
    └── archive/      ← старые версии
"""
from __future__ import annotations

import os
from pathlib import Path

# PROJECT_ROOT = папка НАД src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SRC_DIR = PROJECT_ROOT / "src"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
ARCHIVE_DIR = PROJECT_ROOT / "archive"
BOT_DIR = SRC_DIR / "bot"

# ── Базы данных ──
APEX_DB = os.environ.get("APEX_DB") or str(DATA_DIR / "apex_ai.db")
LEADS_DB = os.environ.get("LEADS_DB") or str(DATA_DIR / "leads.db")

# ── Telethon-сессии ──
PARSER_SESSION = os.environ.get("PARSER_SESSION") or str(DATA_DIR / "apex_discoverer_v")
DISCOVERER_SESSION = os.environ.get("DISCOVERER_SESSION") or str(DATA_DIR / "apex_discoverer_v")
DISCOVERER_V4_SESSION = os.environ.get("DISCOVERER_V4_SESSION") or str(DATA_DIR / "apex_discoverer_v4")

# ── JSON state ──
PARSER_BUDGET_FILE = str(DATA_DIR / "parser_budget.json")
DISCOVERER_BUDGET_FILE = str(DATA_DIR / "discoverer_budget.json")

# ── Bot .env ──
BOT_ENV = BOT_DIR / ".env"

# ── Ensure dirs exist ──
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
