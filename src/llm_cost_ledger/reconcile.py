"""对账 —— 这个项目存在的理由。

市面上做「花费展示」的工具很多，做「账目校验」的几乎没有。
本模块回答一个问题：**账本里的数字，凭什么让人信？**

五道检查，任一条不过就以非零码退出（可以直接挂进 CI）：
  C1 身份唯一性   同一 (fingerprint, occurrence) 不得出现两行 —— 结构性地证明去重生效
  C2 逐行重算     用每行的 price_quote 重算总额，与 cost_usd 对平
  C3 未计价暴露   命中不了价格表的调用必须显式计数，不允许静默按 0 元入账
  C4 压制可追溯   每个导入批次的「压掉多少条」必须留痕，防止去重去过头没人知道
  C5 价格漂移     上游返回的原始花费与本系统计价差异超阈值要报警（价格表该更新了）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from .store import Ledger

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"

DRIFT_THRESHOLD = 0.15  # 15% —— 超过就该怀疑价格表过期


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconcileReport:
    ok: bool
    totals: dict[str, Any]
    findings: list[Finding]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "totals": self.totals,
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message, "detail": f.detail}
                for f in self.findings
            ],
        }

    def render(self) -> str:
        lines = ["== 账目对账 =="]
        t = self.totals
        lines.append(f"  调用数        : {t['calls']}")
        lines.append(f"  总花费        : ${t['cost_usd']:.6f}")
        lines.append(f"  未计价调用    : {t['unpriced_calls']}")
        lines.append(f"  导入批次      : {t['batches']}（压掉 {t['suppressed']} 条）")
        lines.append("")
        if not self.findings:
            lines.append("  ✅ 五道检查全部通过")
        for f in self.findings:
            icon = {"error": "❌", "warn": "⚠️ ", "info": "ℹ️ "}[f.severity]
            lines.append(f"  {icon} [{f.code}] {f.message}")
            for k, v in f.detail.items():
                lines.append(f"        {k}: {v}")
        return "\n".join(lines)


def run_reconcile(ledger: Ledger, drift_threshold: float = DRIFT_THRESHOLD) -> ReconcileReport:
    conn = ledger._conn()  # noqa: SLF001 - 对账需要直连做聚合
    findings: list[Finding] = []

    total_calls = ledger.count_calls()
    total_cost = ledger.total_cost()
    unpriced = int(
        conn.execute("SELECT COUNT(*) AS n FROM calls WHERE unpriced = 1").fetchone()["n"]
    )
    batch_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(suppressed),0) AS s FROM ingest_batches"
    ).fetchone()
    batches = int(batch_row["n"])
    suppressed = int(batch_row["s"])

    # --- C1 身份唯一性 ---
    dup = conn.execute(
        """SELECT fingerprint, occurrence, COUNT(*) AS n
             FROM calls GROUP BY fingerprint, occurrence HAVING n > 1"""
    ).fetchall()
    if dup:
        findings.append(
            Finding(
                "C1", SEVERITY_ERROR,
                f"发现 {len(dup)} 组重复身份 —— 去重失效",
                {"示例": [dict(r) for r in dup[:5]]},
            )
        )

    # --- C2 逐行重算 ---
    mismatch = 0
    recomputed = 0.0
    for row in conn.execute("SELECT cost_usd, price_quote FROM calls"):
        try:
            quote = json.loads(row["price_quote"] or "{}")
            bd = quote.get("breakdown")
            val = float(sum(bd.values())) if isinstance(bd, Mapping) else float(row["cost_usd"])
        except (json.JSONDecodeError, TypeError, ValueError):
            val = float(row["cost_usd"])
        recomputed += val
        if abs(val - float(row["cost_usd"])) > 1e-9:
            mismatch += 1
    if mismatch:
        findings.append(
            Finding(
                "C2", SEVERITY_ERROR,
                f"{mismatch} 行的明细与本行 cost_usd 对不上",
                {"重算总额": round(recomputed, 8), "账本总额": round(total_cost, 8)},
            )
        )

    # --- C3 未计价暴露 ---
    if unpriced:
        rows = conn.execute(
            """SELECT model, COUNT(*) AS n FROM calls WHERE unpriced = 1
                GROUP BY model ORDER BY n DESC LIMIT 10"""
        ).fetchall()
        findings.append(
            Finding(
                "C3", SEVERITY_WARN,
                f"{unpriced} 条调用未命中价格表，按 0 元入账（总额被低估）",
                {"涉及模型": [f"{r['model']}×{r['n']}" for r in rows]},
            )
        )

    # --- C4 压制可追溯 ---
    if batches == 0 and total_calls > 0:
        findings.append(
            Finding("C4", SEVERITY_WARN, "有调用记录但没有导入批次留痕 —— 无法追溯去重行为", {})
        )
    if total_calls and suppressed / max(total_calls + suppressed, 1) > 0.5:
        findings.append(
            Finding(
                "C4", SEVERITY_WARN,
                f"压制比例偏高（{suppressed}/{total_calls + suppressed}）—— 确认不是去重去过头",
                {},
            )
        )

    # --- C5 价格漂移 ---
    drift_rows = conn.execute(
        "SELECT model, cost_usd, raw_cost_usd FROM calls WHERE raw_cost_usd IS NOT NULL AND raw_cost_usd > 0"
    ).fetchall()
    if drift_rows:
        worst: dict[str, Any] = {}
        for row in drift_rows:
            ours = float(row["cost_usd"])
            theirs = float(row["raw_cost_usd"])
            if theirs <= 0:
                continue
            diff = abs(ours - theirs) / theirs
            if diff > drift_threshold:
                key = row["model"]
                prev = worst.get(key)
                if prev is None or diff > prev["最大偏差"]:
                    worst[key] = {"最大偏差": round(diff, 4), "我们": round(ours, 6), "上游": round(theirs, 6)}
        if worst:
            findings.append(
                Finding("C5", SEVERITY_WARN, f"{len(worst)} 个模型计价与上游账单偏差 >{drift_threshold:.0%}", worst)
            )

    has_error = any(f.severity == SEVERITY_ERROR for f in findings)
    totals = {
        "calls": total_calls,
        "cost_usd": round(total_cost, 8),
        "unpriced_calls": unpriced,
        "batches": batches,
        "suppressed": suppressed,
        "recomputed_cost_usd": round(recomputed, 8),
    }
    return ReconcileReport(ok=not has_error, totals=totals, findings=findings)
