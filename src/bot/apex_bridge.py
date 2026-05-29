"""Мост к query_engine: импорт + расширение sys.path.
Лежит в /bot/, query_engine.py — в родительском каталоге /lead_magnet/.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

import query_engine  # noqa: E402

_MODULE_ATTRS = [
    "init_apex_db", "analyze_niche", "count_potential_leads",
    "find_leads", "find_fresh_leads_for_subscriber", "claim_lead",
    "get_claimed_lead", "set_subscription", "get_subscription", "cancel_subscription"
]

def __getattr__(name):
    if name in _MODULE_ATTRS:
        return getattr(query_engine, name)
    raise AttributeError(name)

__all__ = [
    "analyze_niche",
    "count_potential_leads",
    "find_leads",
    "find_fresh_leads_for_subscriber",
    "claim_lead",
    "get_claimed_lead",
    "set_subscription",
    "get_subscription",
    "cancel_subscription",
]
