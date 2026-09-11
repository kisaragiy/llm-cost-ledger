"""看板聚合层。

只做「账本 -> 结构化数据」的翻译，不碰 HTTP 也不碰 HTML —— 这样能纯函数测试，
前端换实现也不用动这里。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .budget import BudgetRule, evaluate_rule
from .spend import LedgerSpendLookup, window_start

MAX_DAYS = 365


def daily_series(ledger, days: int = 30, until: date | None = None) -> list[dict[str, Any]]:
    """按日聚合花费。

    **缺失的日期会补 0** —— 否则图表会把「那天没调用」直接跳过，
    视觉上把空档抹平，看趋势的人会误判。补零才有真实的形状。
    """
    days = max(1, min(int(days), MAX_DAYS))
    end = until or datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)

    rows = ledger._conn().execute(  # noqa: SLF001 - 聚合查询需要直连
        """SELECT substr(ts,1,10) AS d,
                  ROUND(SUM(cost_usd), 8) AS cost_usd,
                  COUNT(*) AS calls,
                  COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
             FROM calls
            WHERE ts >= ? AND ts <= ?
         GROUP BY d""",
        (f"{start.isoformat()}T00:00:00", f"{end.isoformat()}T23:59:59"),
    ).fetchall()
    by_day = {r["d"]: dict(r) for r in rows}

    out: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        row = by_day.get(key)
        out.append(
            {
                "date": key,
                "cost_usd": float(row["cost_usd"]) if row else 0.0,
                "calls": int(row["calls"]) if row else 0,
                "tokens": int(row["tokens"]) if row else 0,
            }
        )
        cursor += timedelta(days=1)
    return out


def totals(ledger) -> dict[str, Any]:
    """头部卡片用的四个窗口总量。"""
    lookup = LedgerSpendLookup(ledger)
    day = BudgetRule(scope="global", window="day", limit_usd=1.0)
    month = BudgetRule(scope="global", window="month", limit_usd=1.0)
    rolling = BudgetRule(scope="global", window="rolling_24h", limit_usd=1.0)
    return {
        "all_time": round(ledger.total_cost(), 8),
        "calls": ledger.count_calls(),
        "today": round(lookup(day, {}), 8),
        "this_month": round(lookup(month, {}), 8),
        "rolling_24h": round(lookup(rolling, {}), 8),
    }


def budget_status(ledger, rules: Sequence[BudgetRule]) -> list[dict[str, Any]]:
    """每条规则当前的 spent / limit / ratio / tier。"""
    lookup = LedgerSpendLookup(ledger)
    out: list[dict[str, Any]] = []
    for rule in rules:
        try:
            spent = float(lookup(rule, {}))
            tier, ratio, spent = evaluate_rule(rule, spent)
            err = None
        except Exception as exc:  # noqa: BLE001 - fail-closed：读不到就报 stop
            spent, ratio, tier, err = 0.0, 1.0, "stop", str(exc)
        out.append(
            {
                "scope": rule.scope,
                "key": rule.key,
                "window": rule.window,
                "spent_usd": round(spent, 8),
                "limit_usd": rule.limit_usd,
                "ratio": round(min(ratio, 1.0), 6),
                "tier": tier,
                "error": err,
            }
        )
    return out


def alerts(ledger, drift_threshold: float = 0.15, limit: int = 10) -> dict[str, Any]:
    """看板告警区：未计价调用 + 价格漂移 + 高压制批次。"""
    conn = ledger._conn()  # noqa: SLF001

    unpriced = [
        {"model": r["model"], "calls": r["n"]}
        for r in conn.execute(
            """SELECT model, COUNT(*) AS n FROM calls WHERE unpriced = 1
                GROUP BY model ORDER BY n DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ]

    drift_rows = conn.execute(
        """SELECT model, cost_usd, raw_cost_usd FROM calls
            WHERE raw_cost_usd IS NOT NULL AND raw_cost_usd > 0"""
    ).fetchall()
    # 按模型聚合：同一模型漂移一片是常态，逐条刷屏会把告警区淹掉。
    # 只保留每个模型的最差一条，并附出现次数。
    worst: dict[str, dict[str, Any]] = {}
    for row in drift_rows:
        ours, theirs = float(row["cost_usd"]), float(row["raw_cost_usd"])
        if theirs <= 0:
            continue
        diff = abs(ours - theirs) / theirs
        if diff <= drift_threshold:
            continue
        model = row["model"]
        cur = worst.get(model)
        if cur is None or diff > cur["drift"]:
            worst[model] = {
                "model": model,
                "ours_usd": round(ours, 6),
                "upstream_usd": round(theirs, 6),
                "drift": round(diff, 4),
                "occurrences": 1,
            }
        else:
            cur["occurrences"] += 1
    drift = sorted(worst.values(), key=lambda d: d["drift"], reverse=True)

    recent_batches = [
        {
            "batch_id": r["batch_id"],
            "source": r["source"],
            "seen": r["seen"],
            "inserted": r["inserted"],
            "suppressed": r["suppressed"],
            "unpriced": r["unpriced"],
            "started_at": r["started_at"],
        }
        for r in conn.execute(
            "SELECT * FROM ingest_batches ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    ]

    failed = [
        {"status": r["status"], "calls": r["n"]}
        for r in conn.execute(
            """SELECT status, COUNT(*) AS n FROM calls WHERE status != 'ok'
                GROUP BY status ORDER BY n DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ]

    return {
        "unpriced": unpriced,
        "unpriced_total": sum(u["calls"] for u in unpriced),
        "drift": drift[:limit],
        "failed_calls": failed,
        "recent_batches": recent_batches,
    }


def build_overview(ledger, rules: Sequence[BudgetRule], days: int = 30) -> dict[str, Any]:
    """看板一次拉取的全部数据。"""
    return {
        "totals": totals(ledger),
        "series": daily_series(ledger, days=days),
        "by_model": ledger.spend_by("model"),
        "by_user": ledger.spend_by("user_id"),
        "by_feature": ledger.spend_by("feature"),
        "budget": budget_status(ledger, rules),
        "alerts": alerts(ledger),
    }
