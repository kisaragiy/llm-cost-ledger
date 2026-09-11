"""从上游响应里提取用量。

不同厂商口径不一样，这里是唯一的翻译层：
  - OpenAI 系: prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens
               completion_tokens_details.reasoning_tokens
  - DeepSeek  : prompt_cache_hit_tokens / prompt_cache_miss_tokens（没有 details 嵌套）
  - Ollama    : prompt_eval_count / eval_count

提取不到用量时返回 None，而不是回退成 0 —— 0 会在账本里伪装成「免费调用」，
把问题藏起来；None 会在对账里现形。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def is_empty(self) -> bool:
        return self.total_tokens == 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_usage(payload: Mapping[str, Any] | None) -> Usage | None:
    """从响应体（或响应里的 usage 段）解析用量。无法解析返回 None。"""
    if not isinstance(payload, Mapping):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        # Ollama 风格：用量在顶层
        if "prompt_eval_count" in payload or "eval_count" in payload:
            usage = payload
        else:
            return None

    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    if prompt == 0 and completion == 0:
        prompt = _int(usage.get("prompt_eval_count"))
        completion = _int(usage.get("eval_count"))

    cached = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, Mapping):
        cached = _int(details.get("cached_tokens"))
    if cached == 0:
        cached = _int(usage.get("prompt_cache_hit_tokens"))
    if cached == 0:
        cached = _int(usage.get("cache_read_input_tokens"))

    reasoning = 0
    cdetails = usage.get("completion_tokens_details")
    if isinstance(cdetails, Mapping):
        reasoning = _int(cdetails.get("reasoning_tokens"))

    result = Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
    )
    return None if result.is_empty() else result


def usage_from_sse(raw_body: str) -> Usage | None:
    """从 SSE 流里捞最后一条带 usage 的 chunk。

    很多后端只在最后一个 chunk 里带 usage（且需要 stream_options.include_usage=true）。
    流式请求不拿到 usage 就没法计费 —— 所以这里宁可多扫一遍，也不能默认按 0 记账。
    """
    found: Usage | None = None
    for line in raw_body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        parsed = parse_usage(chunk)
        if parsed is not None:
            found = parsed
    return found


def request_meta(body: Mapping[str, Any]) -> dict[str, Any]:
    """从请求体里取模型、是否流式等信息。"""
    return {
        "model": str(body.get("model") or ""),
        "stream": bool(body.get("stream")),
    }
