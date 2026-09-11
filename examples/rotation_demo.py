"""对账演示：日志重复导入不会重复计费。

复现真实事故场景：
    同一个时间窗口被导出两次（定时任务跑重了 / 日志轮转后两个文件内容重叠），
    两份导出里的行号还因为文件头差异而整体位移。

  ✗ 旧做法（按 src_file + line_no 去重）：两份文件行号对不上 → 全量重复入库 → 花费虚高
  ✓ 本做法（按内容指纹 + 批内序号去重）：与文件名/行号无关 → 第二次导入 0 条新增

跑法：.venv/Scripts/python.exe examples/rotation_demo.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_cost_ledger.reconcile import run_reconcile  # noqa: E402
from llm_cost_ledger.store import Ledger  # noqa: E402

DEMO_DIR = ROOT / "examples" / "_demo"
N_CALLS = 25
MODEL = "deepseek-chat"
IN_TOKENS = 12_000
OUT_TOKENS = 1_500


def build_window() -> list[dict]:
    """真正的 25 次调用（这是唯一的真相）。"""
    calls = []
    for i in range(N_CALLS):
        calls.append(
            {
                "provider": "https://api.deepseek.com",
                "model": MODEL,
                "endpoint": "/v1/chat/completions",
                "ts": f"2026-09-10T{9 + i // 6:02d}:{(i * 7) % 60:02d}:{(i * 13) % 60:02d}",
                "prompt_tokens": IN_TOKENS + i,
                "completion_tokens": OUT_TOKENS + i,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "user_id": ["alice", "bob", "carol"][i % 3],
                "feature": ["chat", "rag", "summary"][i % 3],
                "session_id": f"sess-{i // 5}",
                "agent_run": "",
                "status": "ok",
                "raw_cost_usd": None,
            }
        )
    return calls


def write_export(path: Path, calls: list[dict], header_lines: int) -> None:
    """导出成 JSONL。header_lines 模拟文件头差异造成的行号位移。"""
    with path.open("w", encoding="utf-8") as fh:
        for i in range(header_lines):
            fh.write(json.dumps({"type": "export_header", "note": f"padding {i}"}, ensure_ascii=False) + "\n")
        for c in calls:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("type") == "export_header":
            continue
        out.append(obj)
    return out


def naive_positional_dedupe(files: list[tuple[Path, int]]) -> int:
    """旧做法：按 (src_file, line_no) 去重。文件不同/行号位移 -> 全部当新记录。"""
    seen = set()
    for path, start_line in files:
        for offset, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("type") == "export_header":
                continue
            key = (path.name, start_line + offset)  # ← 就是这个键害的
            seen.add(key)
    return len(seen)


def main() -> int:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DEMO_DIR.mkdir(parents=True)

    truth = build_window()
    export_a = DEMO_DIR / "export_2026-09-10_run1.jsonl"
    export_b = DEMO_DIR / "export_2026-09-10_run2.jsonl"
    write_export(export_a, truth, header_lines=0)
    write_export(export_b, truth, header_lines=7)  # 同一窗口，但行号整体位移 7 行

    db = DEMO_DIR / "ledger.db"
    ledger = Ledger(db)

    print("=" * 72)
    print("场景：同一时间窗口被导出两次，且第二次行号整体位移 7 行")
    print(f"      真实调用数 = {N_CALLS} 次")
    print("=" * 72)

    naive_count = naive_positional_dedupe([(export_a, 1), (export_b, 1)])
    print(f"\n✗ 旧做法（按 file+line_no 去重）")
    print(f"    入库行数 : {naive_count}    ← 真相是 {N_CALLS}")
    print(f"    虚高比例 : {(naive_count / N_CALLS - 1) * 100:.1f}%")

    print(f"\n✓ 本做法（按内容指纹去重）")
    rec_a = load_jsonl(export_a)
    rec_b = load_jsonl(export_b)

    r1 = ledger.ingest(rec_a, source=export_a.name)
    cost_after_first = ledger.total_cost()
    print(f"    第 1 次导入 [{export_a.name}]")
    print(f"        看到 {r1.seen} 条 / 入库 {r1.inserted} 条 / 压制 {r1.suppressed} 条")
    print(f"        累计花费 = ${cost_after_first:.6f}")

    r2 = ledger.ingest(rec_b, source=export_b.name)
    cost_after_second = ledger.total_cost()
    print(f"    第 2 次导入 [{export_b.name}]（同一窗口，行号位移）")
    print(f"        看到 {r2.seen} 条 / 入库 {r2.inserted} 条 / 压制 {r2.suppressed} 条")
    print(f"        累计花费 = ${cost_after_second:.6f}")

    print(f"\n    花费变化 : {r2.inserted} 条新增 -> ${cost_after_second - cost_after_first:.6f}")
    print(f"    账本总行数: {ledger.count_calls()}（真相 {N_CALLS}）")

    ok = ledger.count_calls() == N_CALLS and abs(cost_after_second - cost_after_first) < 1e-12
    print(f"\n{'✅ 幂等生效：重复导入未产生任何新花费' if ok else '❌ 幂等失效'}")

    print("\n" + "-" * 72)
    report = run_reconcile(ledger)
    print(report.render())

    ledger.close()
    return 0 if (ok and report.ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
