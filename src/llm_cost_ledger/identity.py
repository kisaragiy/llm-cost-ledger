"""调用身份键 —— 幂等去重的核心。

设计要点（这是本项目的立身之本，不是实现细节）：

    身份必须由【内容】决定，绝不能由【记录在文件里的位置】决定。

历史事故：旧实现用 UNIQUE(src_file, line_no) 做去重。日志轮转后行号整体位移，
同一条调用换了 line_no 就被当成新记录重新入库 —— 结果花费虚高 58.6%、调用数虚高 70%。

本实现：fingerprint（内容哈希）+ occurrence（同指纹在本次批次内的序号）。
与文件名、行号、路径完全无关，所以：
  - 同一份日志导入两次       -> 0 条新增
  - 日志轮转产生的重叠文件   -> 0 条新增
  - 同一批里真有 N 条完全相同的调用 -> N 条，一条不少

occurrence 只在【本次批次内】计算，保证行为可预测、可复现。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

# 参与指纹的字段。增删都会改变历史指纹 —— 改这里必须同时升 FINGERPRINT_VERSION。
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "provider",
    "model",
    "endpoint",
    "ts",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "user_id",
    "session_id",
    "feature",
    "agent_run",
    "status",
)

FINGERPRINT_VERSION = "v1"

# 时间归一到秒：上游日志的毫秒精度经常在导出时丢失，归一后才能稳定匹配。
_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S.%f",
)


def normalize_ts(value: Any) -> str:
    """把各种时间写法归一成 'YYYY-MM-DDTHH:MM:SS'（UTC，秒精度）。

    无法解析时返回原值的字符串形式 —— 绝不静默丢弃，宁可留脏值让人在对账时看见。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        # 秒 / 毫秒 / 微秒 自动判别
        v = float(value)
        if v > 1e17:
            v /= 1e6
        elif v > 1e14:
            v /= 1e3
        elif v > 1e11:
            v /= 1e3
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(value)
    text = str(value).strip()
    if not text:
        return ""
    cleaned = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return text


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def _canonical(record: Mapping[str, Any]) -> dict[str, Any]:
    """把一条记录收敛成固定的、与来源无关的字段集合。"""
    out: dict[str, Any] = {}
    for f in FINGERPRINT_FIELDS:
        v = record.get(f)
        if f == "ts":
            out[f] = normalize_ts(v)
        elif f.endswith("_tokens"):
            out[f] = _as_int(v)
        elif f == "status":
            out[f] = str(v or "ok").strip().lower()
        else:
            out[f] = ("" if v is None else str(v)).strip()
    return out


def fingerprint(record: Mapping[str, Any]) -> str:
    """内容指纹：同样内容 -> 同样指纹；换文件名/行号/路径 -> 指纹不变。"""
    canonical = _canonical(record)
    payload = json.dumps(
        {"v": FINGERPRINT_VERSION, "f": canonical},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class IdentifiedRecord:
    """带身份键的记录。"""

    call_key: str
    fingerprint: str
    occurrence: int
    payload: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        row = dict(self.payload)
        row["call_key"] = self.call_key
        row["fingerprint"] = self.fingerprint
        row["occurrence"] = self.occurrence
        return row


def assign_identities(records: Sequence[Mapping[str, Any]]) -> list[IdentifiedRecord]:
    """给一批记录分配身份键。

    - 显式带 request_id 的：直接用问 provider 要来的强身份，最可靠。
    - 其余：fingerprint + 批内出现序号。

    序号按批内第几次出现计数，从 0 开始。同一批里两条真·重复调用会拿到
    occurrence 0 / 1，都会被保留 —— 去重只针对【重复导入】，不针对【真重复调用】。
    """
    seen: dict[str, int] = {}
    out: list[IdentifiedRecord] = []
    for rec in records:
        req_id = str(rec.get("request_id") or "").strip()
        if req_id:
            fp = f"req:{req_id}"
            out.append(
                IdentifiedRecord(
                    call_key=fp,
                    fingerprint=fp,
                    occurrence=0,
                    payload=dict(record_of(rec)),
                )
            )
            continue
        fp = fingerprint(rec)
        occ = seen.get(fp, 0)
        seen[fp] = occ + 1
        out.append(
            IdentifiedRecord(
                call_key=f"{fp}:{occ}",
                fingerprint=fp,
                occurrence=occ,
                payload=dict(record_of(rec)),
            )
        )
    return out


def record_of(rec: Mapping[str, Any]) -> Mapping[str, Any]:
    """批次内保留的字段（不含 request_id —— 它已经并入 fingerprint 了）。"""
    return rec


def dedupe_keys(records: Iterable[Mapping[str, Any]]) -> list[str]:
    """便捷函数：直接拿一批记录的全部身份键（按原顺序，含重复项）。"""
    return [r.call_key for r in assign_identities(list(records))]
