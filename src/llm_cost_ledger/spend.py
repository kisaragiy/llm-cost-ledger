"""窗口计算 + 基于账本的已花费查询。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .budget import SCOPE_FEATURE, SCOPE_GLOBAL, SCOPE_USER, BudgetRule, WINDOW_ROLLING_24H


def window_start(window: str, now: datetime | None = None) -> str | None:
    """返回窗口起始时间（'YYYY-MM-DDTHH:MM:SS'，UTC）；WINDOW_TOTAL 返回 None 表示不限。"""
    now = now or datetime.now(timezone.utc)
    if window == "day":
        return now.strftime("%Y-%m-%dT00:00:00")
    if window == "month":
        return now.strftime("%Y-%m-01T00:00:00")
    if window == WINDOW_ROLLING_24H:
        return (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    return None


class LedgerSpendLookup:
    """把 BudgetRule 翻译成一次账本查询。"""

    def __init__(self, ledger) -> None:
        self.ledger = ledger

    def __call__(self, rule: BudgetRule, ctx: Mapping[str, Any]) -> float:
        since = window_start(rule.window)
        filters: dict[str, Any] = {"since": since} if since else {}
        if rule.scope == SCOPE_USER:
            filters["user_id"] = rule.key
        elif rule.scope == SCOPE_FEATURE:
            filters["feature"] = rule.key
        elif rule.scope != SCOPE_GLOBAL:
            raise ValueError(f"未知 scope: {rule.scope}")
        return self.ledger.total_cost(**filters)
