"""造一批看板演示数据（30 天，含三维归因、未计价、失败调用、价格漂移）。

跑法：.venv/Scripts/python.exe scripts/seed_demo.py --db examples/_demo/ledger.db

仅用于本地看板预览与截图，不参与任何生产路径。
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_cost_ledger.store import Ledger  # noqa: E402

MODELS = [
    ("deepseek-chat", 0.62),
    ("deepseek-reasoner", 0.18),
    ("qwen3.8-flash", 0.12),
    ("gpt-4o-mini", 0.05),
]
USERS = [("alice", 0.38), ("bob", 0.26), ("carol", 0.19), ("dave", 0.11), ("", 0.06)]
FEATURES = [("rag", 0.34), ("chat", 0.29), ("summary", 0.16), ("vision", 0.13), ("batch-import", 0.08)]


def weighted(pairs):
    names = [p[0] for p in pairs]
    weights = [p[1] for p in pairs]
    return random.choices(names, weights=weights, k=1)[0]


def build(days: int = 30, seed: int = 20260911) -> list[dict]:
    random.seed(seed)
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for d in range(days, -1, -1):
        day = now - timedelta(days=d)
        # 周末少一些，工作日多一些；最近几天略微上量
        base = 6 if day.weekday() >= 5 else 16
        base += max(0, (days - d) // 6)
        for _ in range(random.randint(base // 2, base)):
            model = weighted(MODELS)
            user = weighted(USERS)
            feature = weighted(FEATURES)
            big = random.random() < 0.18
            prompt = random.randint(400, 2600) * (5 if big else 1)
            completion = random.randint(120, 900) * (4 if big else 1)
            cached = int(prompt * random.choice([0, 0, 0.3, 0.6]))
            ts = day.replace(hour=random.randint(0, 23), minute=random.randint(0, 59),
                             second=random.randint(0, 59))
            status = "ok"
            if random.random() < 0.022:
                status = random.choice(["http_429", "upstream_unreachable", "stream_error"])
            rec = {
                "provider": "https://api.deepseek.com",
                "model": model,
                "endpoint": "/v1/chat/completions",
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%S"),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_tokens": cached,
                "reasoning_tokens": int(completion * 0.5) if model == "deepseek-reasoner" else 0,
                "user_id": user,
                "feature": feature,
                "session_id": f"sess-{random.randint(1, 40)}",
                "agent_run": "",
                "status": status,
            }
            if status == "ok":
                rec["request_id"] = f"live-{random.getrandbits(64):016x}"
            out.append(rec)

    # 未计价：新上线模型还没进价格表
    for _ in range(14):
        out.append({
            "provider": "https://api.moonshot.cn", "model": "kimi-k2-0905-preview",
            "endpoint": "/v1/chat/completions",
            "ts": (now - timedelta(days=random.randint(0, 5))).strftime("%Y-%m-%dT%H:%M:%S"),
            "prompt_tokens": 3200, "completion_tokens": 700,
            "user_id": "alice", "feature": "chat", "status": "ok",
            "request_id": f"live-{random.getrandbits(64):016x}",
        })
    # 价格漂移：上游账单比本系统算的高一截
    for _ in range(6):
        out.append({
            "provider": "https://api.deepseek.com", "model": "deepseek-chat",
            "endpoint": "/v1/chat/completions",
            "ts": (now - timedelta(days=random.randint(0, 9))).strftime("%Y-%m-%dT%H:%M:%S"),
            "prompt_tokens": 240_000, "completion_tokens": 8_000, "cached_tokens": 0,
            "user_id": "bob", "feature": "batch-import", "status": "ok",
            "raw_cost_usd": 0.32,          # 上游口径明显高于本地价格表
            "request_id": f"live-{random.getrandbits(64):016x}",
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "examples" / "_demo" / "ledger.db"))
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    db = Path(args.db)
    if db.exists():
        db.unlink()
    ledger = Ledger(db)
    records = build(days=args.days)
    report = ledger.ingest(records, source="demo-seed")
    print(f"造数完成：看到 {report.seen} / 入库 {report.inserted} / 压制 {report.suppressed}")
    print(f"累计花费 ${ledger.total_cost():.4f} · 调用 {ledger.count_calls()} 次")
    print(f"数据库：{db}")
    ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
