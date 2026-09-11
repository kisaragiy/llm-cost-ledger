"""命令行入口：serve / reconcile / ingest / summary / spend。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__, pricing
from .config import load_settings
from .reconcile import run_reconcile
from .store import Ledger


def _load_records(path: str) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(text)
        return list(data)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    settings = load_settings(args.config)
    if args.port:
        settings.port = args.port
    settings.require_upstream()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = Ledger(args.db or settings.ledger_db)
    report = run_reconcile(ledger)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render())
    return 0 if report.ok else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = Ledger(args.db or settings.ledger_db)
    records = _load_records(args.file)
    report = ledger.ingest(records, source=args.source or f"file:{Path(args.file).name}")
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = Ledger(args.db or settings.ledger_db)
    filters = {k: v for k, v in (("since", args.since), ("until", args.until)) if v}
    print(f"调用数 : {ledger.count_calls(**filters)}")
    print(f"总花费 : ${ledger.total_cost(**filters):.6f}")
    for dim in ("model", "user_id", "feature"):
        rows = ledger.spend_by(dim, **filters)
        if rows:
            print(f"\n按 {dim}:")
            for r in rows:
                print(f"  {r['key'] or '(空)':<28} ${r['cost_usd']:.6f}  {r['calls']} 次")
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    ledger = Ledger(args.db or settings.ledger_db)
    for r in ledger.spend_by(args.by, since=args.since):
        print(f"{r['key'] or '(空)':<28} ${r['cost_usd']:.6f}  {r['calls']} 次  {r['tokens']} tokens")
    return 0


def cmd_pricing(_: argparse.Namespace) -> int:
    print(f"价格表版本: {pricing.PRICING_VERSION}")
    for model, rate in sorted(pricing.PRICE_TABLE.items()):
        print(f"  {model:<22} in=${rate['input']:<6} out=${rate['output']:<6} cached=${rate['cached_input']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ledger", description="llm-cost-ledger —— LLM 成本账本")
    p.add_argument("--version", action="version", version=f"llm-cost-ledger {__version__}")
    p.add_argument("--config", default=None, help="config.json 路径")
    p.add_argument("--db", default=None, help="覆盖账本数据库路径")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="启动 OpenAI 兼容代理 + 账本 API")
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("reconcile", help="对账（不一致时非零退出）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("ingest", help="从 JSON/JSONL 文件批量导入调用记录")
    s.add_argument("file")
    s.add_argument("--source", default=None)
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("summary", help="汇总")
    s.add_argument("--since", default=None)
    s.add_argument("--until", default=None)
    s.set_defaults(func=cmd_summary)

    s = sub.add_parser("spend", help="按维度查看花费")
    s.add_argument("--by", default="user_id",
                   choices=["user_id", "feature", "model", "provider", "session_id", "agent_run", "ts"])
    s.add_argument("--since", default=None)
    s.set_defaults(func=cmd_spend)

    s = sub.add_parser("pricing", help="查看价格表")
    s.set_defaults(func=cmd_pricing)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
