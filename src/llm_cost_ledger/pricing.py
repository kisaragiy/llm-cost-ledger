"""模型价格表 + 计费。

原则：价格表是【可审计的输入】，不是魔法数字。
每次计费都记录 priced_by（命中的价格表键），对账时能倒查是按哪个价算的。
未知模型绝不按 0 元静默记账 —— 明确标记为 unpriced，让它在对账里现形。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# 单位：USD / 1M tokens。价格会漂移，所以带 version，对账时能发现漂移。
PRICING_VERSION = "2026-09-11"

PRICE_TABLE: dict[str, dict[str, float]] = {
    # model key -> {input, output, cached_input, reasoning}
    "deepseek-chat":      {"input": 0.27,  "output": 1.10, "cached_input": 0.07, "reasoning": 1.10},
    "deepseek-reasoner":  {"input": 0.55,  "output": 2.19, "cached_input": 0.14, "reasoning": 2.19},
    "deepseek-flash":     {"input": 0.10,  "output": 0.40, "cached_input": 0.02, "reasoning": 0.40},
    "gpt-4o":             {"input": 2.50,  "output": 10.00, "cached_input": 1.25, "reasoning": 10.00},
    "gpt-4o-mini":        {"input": 0.15,  "output": 0.60, "cached_input": 0.075, "reasoning": 0.60},
    "qwen3.5:9b":         {"input": 0.0,   "output": 0.0,  "cached_input": 0.0,   "reasoning": 0.0},
    "qwen3.5:0.8b":       {"input": 0.0,   "output": 0.0,  "cached_input": 0.0,   "reasoning": 0.0},
    "qwen3.8-flash":      {"input": 0.05,  "output": 0.20, "cached_input": 0.01,  "reasoning": 0.20},
    "claude-sonnet-4":    {"input": 3.00,  "output": 15.00, "cached_input": 0.30, "reasoning": 15.00},
}

PER_MILLION = 1_000_000.0


@dataclass(frozen=True)
class PriceQuote:
    """一次计费的结果。model_key 为空 = 未命中价格表。"""

    cost_usd: float
    priced_by: str
    model_key: str
    unpriced: bool
    breakdown: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_usd": round(self.cost_usd, 8),
            "priced_by": self.priced_by,
            "model_key": self.model_key,
            "unpriced": self.unpriced,
            "breakdown": {k: round(v, 8) for k, v in self.breakdown.items()},
        }


def resolve_model_key(model: str) -> str | None:
    """把上游模型名映射到价格表键。支持带前缀/后缀的变体。"""
    if not model:
        return None
    name = str(model).strip()
    if name in PRICE_TABLE:
        return name
    low = name.lower()
    for key in PRICE_TABLE:
        if key.lower() == low:
            return key
    # 前缀匹配（如 "deepseek-chat-v3" -> "deepseek-chat"），取最长匹配
    candidates = [k for k in PRICE_TABLE if low.startswith(k.lower()) or k.lower() in low]
    if candidates:
        return max(candidates, key=len)
    return None


def quote(record: Mapping[str, Any], table: Mapping[str, Mapping[str, float]] | None = None) -> PriceQuote:
    """按价格表给一条调用记录计费。"""
    table = table or PRICE_TABLE
    model = str(record.get("model") or "")
    key = resolve_model_key(model)
    if key is None or key not in table:
        return PriceQuote(
            cost_usd=0.0,
            priced_by="",
            model_key="",
            unpriced=True,
            breakdown={},
        )
    rate = table[key]
    pt = int(record.get("prompt_tokens") or 0)
    ct = int(record.get("completion_tokens") or 0)
    cached = int(record.get("cached_tokens") or 0)
    reasoning = int(record.get("reasoning_tokens") or 0)

    # 缓存命中的部分按 cached_input 计价，并从全价 input 里扣掉，避免重复计费。
    cached = max(0, min(cached, pt))
    billed_input = pt - cached
    # reasoning token 通常已计入 completion_tokens；只有独立上报时才单算，防双计。
    extra_reasoning = max(0, reasoning - ct)

    breakdown = {
        "input": billed_input / PER_MILLION * rate.get("input", 0.0),
        "cached_input": cached / PER_MILLION * rate.get("cached_input", 0.0),
        "output": ct / PER_MILLION * rate.get("output", 0.0),
        "reasoning": extra_reasoning / PER_MILLION * rate.get("reasoning", 0.0),
    }
    return PriceQuote(
        cost_usd=sum(breakdown.values()),
        priced_by=f"{PRICING_VERSION}:{key}",
        model_key=key,
        unpriced=False,
        breakdown=breakdown,
    )


def known_models() -> list[str]:
    return sorted(PRICE_TABLE)
