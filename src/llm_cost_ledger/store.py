"""SQLite 账本存储。

两条铁律：
  1. 写入靠【主键冲突】去重，不靠先查后插（先查后插在并发下必然漏）。
  2. 每次导入都留一条 batch 记录（看到多少 / 入库多少 / 压掉多少），
     这样「少收了钱」和「账目被压掉」都能在对账时被看见，不会静默。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import pricing
from .identity import assign_identities, normalize_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_key          TEXT PRIMARY KEY,
    fingerprint       TEXT NOT NULL,
    occurrence        INTEGER NOT NULL,
    ts                TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    endpoint          TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    user_id           TEXT NOT NULL DEFAULT '',
    session_id        TEXT NOT NULL DEFAULT '',
    feature           TEXT NOT NULL DEFAULT '',
    agent_run         TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'ok',
    cost_usd          REAL NOT NULL DEFAULT 0,
    raw_cost_usd      REAL,
    price_quote       TEXT NOT NULL DEFAULT '',
    unpriced          INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT '',
    ingested_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_ts      ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_user    ON calls(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_calls_feature ON calls(feature, ts);
CREATE INDEX IF NOT EXISTS idx_calls_model   ON calls(model, ts);
CREATE INDEX IF NOT EXISTS idx_calls_fp      ON calls(fingerprint);

CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id     TEXT PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    seen         INTEGER NOT NULL DEFAULT 0,
    inserted     INTEGER NOT NULL DEFAULT 0,
    suppressed   INTEGER NOT NULL DEFAULT 0,
    unpriced     INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0
);
"""

OUTCOME_INSERTED = "inserted"
OUTCOME_SUPPRESSED = "suppressed"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class IngestReport:
    batch_id: str
    seen: int
    inserted: int
    suppressed: int
    unpriced: int
    cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "seen": self.seen,
            "inserted": self.inserted,
            "suppressed": self.suppressed,
            "unpriced": self.unpriced,
            "cost_usd": round(self.cost_usd, 8),
        }


class Ledger:
    """线程安全的账本。每线程一条连接 + WAL（见 software-engineering skill 第 10 章）。"""

    def __init__(self, db_path: str | Path = "ledger.db") -> None:
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._ensure_schema()

    # ---------- 连接管理 ----------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        with self._init_lock:
            self._conn().executescript(SCHEMA)
            self._conn().commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------- 写入 ----------
    def ingest(
        self,
        records: Sequence[Mapping[str, Any]],
        source: str = "api",
        price_table: Mapping[str, Mapping[str, float]] | None = None,
    ) -> IngestReport:
        """导入一批调用记录。重复导入同一批 -> inserted=0，账目不变。"""
        conn = self._conn()
        batch_id = uuid.uuid4().hex[:16]
        started = _now()
        conn.execute(
            "INSERT INTO ingest_batches (batch_id, source, started_at) VALUES (?,?,?)",
            (batch_id, source, started),
        )

        identified = assign_identities(list(records))
        rows: list[tuple[Any, ...]] = []
        unpriced = 0
        cost_total = 0.0
        for item in identified:
            payload = dict(item.payload)
            q = pricing.quote(payload, price_table)
            if q.unpriced:
                unpriced += 1
            cost_total += q.cost_usd
            rows.append(
                (
                    item.call_key,
                    item.fingerprint,
                    item.occurrence,
                    normalize_ts(payload.get("ts")),
                    str(payload.get("provider") or ""),
                    str(payload.get("model") or ""),
                    str(payload.get("endpoint") or ""),
                    int(payload.get("prompt_tokens") or 0),
                    int(payload.get("completion_tokens") or 0),
                    int(payload.get("cached_tokens") or 0),
                    int(payload.get("reasoning_tokens") or 0),
                    str(payload.get("user_id") or ""),
                    str(payload.get("session_id") or ""),
                    str(payload.get("feature") or ""),
                    str(payload.get("agent_run") or ""),
                    str(payload.get("status") or "ok"),
                    round(q.cost_usd, 8),
                    payload.get("raw_cost_usd"),
                    json.dumps(q.as_dict(), ensure_ascii=False),
                    1 if q.unpriced else 0,
                    str(payload.get("source") or source),
                    _now(),
                )
            )

        before = conn.total_changes
        conn.executemany(
            """INSERT INTO calls (
                   call_key, fingerprint, occurrence, ts, provider, model, endpoint,
                   prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens,
                   user_id, session_id, feature, agent_run, status,
                   cost_usd, raw_cost_usd, price_quote, unpriced, source, ingested_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(call_key) DO NOTHING""",
            rows,
        )
        inserted = conn.total_changes - before
        suppressed = len(rows) - inserted
        conn.execute(
            """UPDATE ingest_batches
                  SET finished_at=?, seen=?, inserted=?, suppressed=?, unpriced=?, cost_usd=?
                WHERE batch_id=?""",
            (_now(), len(rows), inserted, suppressed, unpriced, round(cost_total, 8), batch_id),
        )
        conn.commit()
        return IngestReport(
            batch_id=batch_id,
            seen=len(rows),
            inserted=inserted,
            suppressed=suppressed,
            unpriced=unpriced,
            cost_usd=cost_total,
        )

    def record_call(self, payload: Mapping[str, Any], source: str = "proxy") -> IngestReport:
        return self.ingest([payload], source=source)

    # ---------- 查询 ----------
    def total_cost(self, **filters: Any) -> float:
        where, args = self._where(filters)
        row = self._conn().execute(
            f"SELECT COALESCE(SUM(cost_usd), 0) AS c FROM calls {where}", args
        ).fetchone()
        return float(row["c"])

    def count_calls(self, **filters: Any) -> int:
        where, args = self._where(filters)
        row = self._conn().execute(f"SELECT COUNT(*) AS n FROM calls {where}", args).fetchone()
        return int(row["n"])

    def spend_by(self, dimension: str, **filters: Any) -> list[dict[str, Any]]:
        if dimension not in {"user_id", "feature", "model", "provider", "session_id", "agent_run", "ts"}:
            raise ValueError(f"不支持的归因维度: {dimension}")
        where, args = self._where(filters)
        col = "substr(ts,1,10)" if dimension == "ts" else dimension
        rows = self._conn().execute(
            f"""SELECT {col} AS key, ROUND(SUM(cost_usd), 8) AS cost_usd,
                       COUNT(*) AS calls, SUM(prompt_tokens + completion_tokens) AS tokens
                  FROM calls {where}
              GROUP BY {col}
              ORDER BY cost_usd DESC""",
            args,
        ).fetchall()
        return [dict(r) for r in rows]

    def _where(self, filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        for key, val in filters.items():
            if val is None:
                continue
            if key == "since":
                clauses.append("ts >= ?")
                args.append(normalize_ts(val))
            elif key == "until":
                clauses.append("ts <= ?")
                args.append(normalize_ts(val))
            elif key in {"user_id", "feature", "model", "provider", "session_id", "agent_run", "status"}:
                clauses.append(f"{key} = ?")
                args.append(str(val))
            else:
                raise ValueError(f"不支持的过滤条件: {key}")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, args

    def batches(self, limit: int = 20) -> list[dict[str, Any]]:
        # started_at 是秒精度 —— 同一秒内的多个批次靠 rowid 定序，否则顺序不确定
        rows = self._conn().execute(
            "SELECT * FROM ingest_batches ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
