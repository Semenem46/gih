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

init_apex_db = query_engine.init_apex_db
analyze_niche = query_engine.analyze_niche
count_potential_leads = query_engine.count_potential_leads
find_leads = query_engine.find_leads
find_fresh_leads_for_subscriber = query_engine.find_fresh_leads_for_subscriber
claim_lead = query_engine.claim_lead
get_claimed_lead = query_engine.get_claimed_lead
set_subscription = query_engine.set_subscription
get_subscription = query_engine.get_subscription
cancel_subscription = query_engine.cancel_subscription

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
