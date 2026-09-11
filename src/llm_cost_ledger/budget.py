"""三层预算 + 熔断。

三层不是三个功能，是三个不同后果：
  WARN -> 照常放行，只记账 + 打标（人该知道了）
  ASK  -> 需要显式批准才继续（默认拒绝，因为无人值守时「静默继续」就是失控）
  STOP -> 直接拒绝请求，返回明确原因

设计取舍：默认 fail-closed。评估不出来（比如账本读不到）时按 STOP 处理，
宁可拒绝一次请求，也不要在一个已经失控的预算上继续烧钱。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

TIER_OK = "ok"
TIER_WARN = "warn"
TIER_ASK = "ask"
TIER_STOP = "stop"

_TIER_ORDER = {TIER_OK: 0, TIER_WARN: 1, TIER_ASK: 2, TIER_STOP: 3}

WINDOW_DAY = "day"
WINDOW_MONTH = "month"
WINDOW_ROLLING_24H = "rolling_24h"
WINDOW_TOTAL = "total"
VALID_WINDOWS = {WINDOW_DAY, WINDOW_MONTH, WINDOW_ROLLING_24H, WINDOW_TOTAL}

SCOPE_GLOBAL = "global"
SCOPE_USER = "user"
SCOPE_FEATURE = "feature"
VALID_SCOPES = {SCOPE_GLOBAL, SCOPE_USER, SCOPE_FEATURE}


@dataclass(frozen=True)
class BudgetRule:
    scope: str
    window: str
    limit_usd: float
    key: str = ""
    warn_ratio: float = 0.80
    ask_ratio: float = 0.95

    def __post_init__(self) -> None:
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"非法 scope: {self.scope}（可选 {sorted(VALID_SCOPES)}）")
        if self.window not in VALID_WINDOWS:
            raise ValueError(f"非法 window: {self.window}（可选 {sorted(VALID_WINDOWS)}）")
        if self.limit_usd <= 0:
            raise ValueError("limit_usd 必须大于 0")
        if not (0 < self.warn_ratio <= self.ask_ratio <= 1.0):
            raise ValueError("要求 0 < warn_ratio <= ask_ratio <= 1")
        if self.scope in {SCOPE_USER, SCOPE_FEATURE} and not self.key:
            raise ValueError(f"scope={self.scope} 必须给 key")

    def matches(self, ctx: Mapping[str, Any]) -> bool:
        if self.scope == SCOPE_GLOBAL:
            return True
        if self.scope == SCOPE_USER:
            return str(ctx.get("user_id") or "") == self.key
        if self.scope == SCOPE_FEATURE:
            return str(ctx.get("feature") or "") == self.key
        return False


@dataclass
class Decision:
    tier: str
    allowed: bool
    spent_usd: float
    limit_usd: float
    ratio: float
    message: str
    rule: BudgetRule | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "allowed": self.allowed,
            "spent_usd": round(self.spent_usd, 8),
            "limit_usd": self.limit_usd,
            "ratio": round(self.ratio, 6),
            "message": self.message,
            "rule": (
                None
                if self.rule is None
                else {
                    "scope": self.rule.scope,
                    "key": self.rule.key,
                    "window": self.rule.window,
                    "limit_usd": self.rule.limit_usd,
                }
            ),
        }


def evaluate_rule(rule: BudgetRule, spent_usd: float) -> tuple[str, float, float]:
    """纯函数：给定已花费与规则，返回 (tier, ratio, spent)。"""
    limit = rule.limit_usd
    spent = max(0.0, float(spent_usd))
    ratio = spent / limit if limit > 0 else 0.0
    if ratio >= 1.0:
        return TIER_STOP, ratio, spent
    if ratio >= rule.ask_ratio:
        return TIER_ASK, ratio, spent
    if ratio >= rule.warn_ratio:
        return TIER_WARN, ratio, spent
    return TIER_OK, ratio, spent


def decide(
    rules: Sequence[BudgetRule],
    ctx: Mapping[str, Any],
    spend_lookup,
) -> Decision:
    """对一次即将发生的调用做预算判定。

    spend_lookup(rule, ctx) -> 该规则窗口内已花费（美元）。
    命中多条规则时取最严的一条 —— 任一维度触顶都算触顶。
    """
    candidates = [r for r in rules if r.matches(ctx)]
    if not candidates:
        return Decision(
            tier=TIER_OK, allowed=True, spent_usd=0.0, limit_usd=0.0,
            ratio=0.0, message="未配置预算规则，放行",
        )

    worst: Decision | None = None
    for rule in candidates:
        try:
            spent = float(spend_lookup(rule, ctx))
        except Exception as exc:  # noqa: BLE001 - fail-closed：读不到账目就必须拦
            return Decision(
                tier=TIER_STOP, allowed=False, spent_usd=0.0, limit_usd=rule.limit_usd,
                ratio=1.0, rule=rule,
                message=f"预算判定失败（读取账目异常：{exc}）——按保守策略拒绝放行",
            )
        tier, ratio, spent_val = evaluate_rule(rule, spent)
        label = rule.key or "全局"
        if tier == TIER_STOP:
            msg = f"预算已超限：{rule.scope}={label} {rule.window} 已花 ${spent_val:.4f} / 上限 ${rule.limit_usd:.2f}"
        elif tier == TIER_ASK:
            msg = f"预算接近上限：{rule.scope}={label} {rule.window} 已花 ${spent_val:.4f} / 上限 ${rule.limit_usd:.2f}，需批准"
        elif tier == TIER_WARN:
            msg = f"预算告警：{rule.scope}={label} {rule.window} 已花 ${spent_val:.4f} / 上限 ${rule.limit_usd:.2f}"
        else:
            msg = f"预算正常：{rule.scope}={label} {rule.window} ${spent_val:.4f} / ${rule.limit_usd:.2f}"
        cand = Decision(
            tier=tier,
            allowed=(tier != TIER_STOP),
            spent_usd=spent_val,
            limit_usd=rule.limit_usd,
            ratio=ratio,
            message=msg,
            rule=rule,
        )
        if worst is None or _TIER_ORDER[cand.tier] > _TIER_ORDER[worst.tier]:
            worst = cand
    assert worst is not None
    return worst


def load_rules(raw: Sequence[Mapping[str, Any]] | None) -> list[BudgetRule]:
    """从配置字典加载规则。格式错误直接抛 —— 配置错不许静默降级成「无预算」。"""
    rules: list[BudgetRule] = []
    for item in raw or []:
        rules.append(
            BudgetRule(
                scope=str(item["scope"]),
                window=str(item.get("window", WINDOW_DAY)),
                limit_usd=float(item["limit_usd"]),
                key=str(item.get("key", "")),
                warn_ratio=float(item.get("warn_ratio", 0.80)),
                ask_ratio=float(item.get("ask_ratio", 0.95)),
            )
        )
    return rules
