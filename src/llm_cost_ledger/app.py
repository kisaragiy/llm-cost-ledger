"""FastAPI 应用：OpenAI 兼容代理 + 账本查询 + 批量导入。

代理是透明转发 —— 调用方少改一行代码就能接入（只换 base_url）。
所有判定都发生在上游调用【之前】（预算熔断）或【之后】（记账），
绝不在中间改动响应内容。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Any, AsyncIterator, Mapping

import httpx
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import __version__, extract, insights, pricing
from .budget import load_rules, decide, TIER_STOP
from .config import Settings, load_settings
from .spend import LedgerSpendLookup
from .store import Ledger

STATIC_DIR = Path(__file__).resolve().parent / "static"
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def create_app(
    settings: Settings | None = None,
    ledger: Ledger | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    ledger = ledger or Ledger(settings.ledger_db)
    rules = load_rules(settings.budget_rules)
    spend_lookup = LedgerSpendLookup(ledger)

    app = FastAPI(title="llm-cost-ledger", version=__version__)
    app.state.settings = settings
    app.state.ledger = ledger
    app.state.rules = rules

    # ---------- 鉴权 ----------
    async def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not settings.proxy_auth_key:
            return
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if token != settings.proxy_auth_key:
            raise HTTPException(status_code=401, detail="代理鉴权失败：请提供正确的 Bearer token")

    def _attr(request: Request, body: Mapping[str, Any]) -> dict[str, str]:
        meta = body.get("metadata") if isinstance(body.get("metadata"), Mapping) else {}
        return {
            "user_id": str(
                request.headers.get("x-ledger-user")
                or meta.get("user_id")
                or settings.default_user
            ),
            "feature": str(
                request.headers.get("x-ledger-feature")
                or meta.get("feature")
                or settings.default_feature
            ),
            "session_id": str(request.headers.get("x-ledger-session") or meta.get("session_id") or ""),
            "agent_run": str(request.headers.get("x-ledger-agent-run") or meta.get("agent_run") or ""),
        }

    # ---------- 基础端点 ----------
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "upstream_configured": bool(settings.upstream_base_url),
            "budget_rules": len(rules),
            "calls_recorded": ledger.count_calls(),
            "total_cost_usd": round(ledger.total_cost(), 8),
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        """看板页面。单文件、零外部依赖 —— 断网也能打开。"""
        if not DASHBOARD_HTML.is_file():
            return HTMLResponse("<h1>看板文件缺失</h1><p>static/dashboard.html 未随包安装</p>", status_code=500)
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))

    @app.get("/v1/ledger/overview")
    async def overview(
        days: int = Query(default=30, ge=1, le=365, description="趋势天数"),
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        """看板一次拉取的全部数据（总量 / 趋势 / 三维榜单 / 预算 / 告警）。"""
        return insights.build_overview(ledger, rules, days=days)

    @app.get("/v1/ledger/budget")
    async def budget_view(_: None = Depends(require_auth)) -> dict[str, Any]:
        return {"rules": insights.budget_status(ledger, rules)}

    @app.get("/v1/ledger/alerts")
    async def alerts_view(_: None = Depends(require_auth)) -> dict[str, Any]:
        return insights.alerts(ledger)

    @app.get("/v1/ledger/config")
    async def show_config(_: None = Depends(require_auth)) -> dict[str, Any]:
        return settings.public()

    @app.get("/v1/ledger/summary")
    async def summary(
        since: str | None = None,
        until: str | None = None,
        user_id: str | None = None,
        feature: str | None = None,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        filters = {"since": since, "until": until, "user_id": user_id, "feature": feature}
        return {
            "total_cost_usd": round(ledger.total_cost(**filters), 8),
            "calls": ledger.count_calls(**filters),
            "by_model": ledger.spend_by("model", **filters),
            "by_user": ledger.spend_by("user_id", **filters),
            "by_feature": ledger.spend_by("feature", **filters),
        }

    @app.get("/v1/ledger/spend")
    async def spend_by(
        by: str = Query(default="user_id", description="user_id|feature|model|provider|session_id|agent_run|ts"),
        since: str | None = None,
        until: str | None = None,
        _: None = Depends(require_auth),
    ) -> dict[str, Any]:
        return {"dimension": by, "rows": ledger.spend_by(by, since=since, until=until)}

    @app.get("/v1/ledger/batches")
    async def batches(limit: int = 20, _: None = Depends(require_auth)) -> dict[str, Any]:
        return {"batches": ledger.batches(limit)}

    @app.get("/v1/ledger/pricing")
    async def show_pricing(_: None = Depends(require_auth)) -> dict[str, Any]:
        return {"version": pricing.PRICING_VERSION, "models": pricing.PRICE_TABLE}

    @app.post("/v1/ledger/ingest")
    async def ingest(payload: dict[str, Any], _: None = Depends(require_auth)) -> dict[str, Any]:
        records = payload.get("records")
        if not isinstance(records, list):
            raise HTTPException(status_code=400, detail="请求体需要包含 records 数组")
        report = ledger.ingest(records, source=str(payload.get("source") or "api"))
        return report.as_dict()

    # ---------- 代理 ----------
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, _: None = Depends(require_auth)):
        if not settings.upstream_base_url:
            return JSONResponse(
                status_code=503,
                content={"error": {
                    "type": "upstream_not_configured",
                    "message": "上游未配置：请设置环境变量 UPSTREAM_BASE_URL（例：https://api.deepseek.com）",
                }},
            )
        body = await request.json()
        attr = _attr(request, body)

        # 预算判定 —— 在上游调用之前，这是熔断唯一有效的位置
        verdict = decide(rules, attr, spend_lookup)
        if not verdict.allowed:
            return JSONResponse(
                status_code=402,
                content={"error": {"type": "budget_exceeded", "message": verdict.message,
                                   "ledger": verdict.as_dict()}},
            )
        warn_header: dict[str, str] = {}
        if verdict.tier != "ok":
            # HTTP 头只能是 latin-1 —— 中文提示必须百分号编码，客户端用 unquote 还原
            warn_header = {
                "x-ledger-budget-tier": verdict.tier,
                "x-ledger-budget-message": quote(verdict.message, safe=""),
                "x-ledger-budget-message-encoding": "percent",
            }

        url = settings.upstream_base_url.rstrip("/") + "/v1/chat/completions"
        headers = {"content-type": "application/json"}
        if settings.upstream_api_key:
            headers["authorization"] = f"Bearer {settings.upstream_api_key}"
        stream = bool(body.get("stream"))
        if stream:
            body.setdefault("stream_options", {"include_usage": True})

        client = httpx.AsyncClient(timeout=settings.upstream_timeout_s, transport=transport)

        def record(usage, status: str, raw_cost: Any = None, upstream_id: str | None = None) -> None:
            """每次实时转发都是一条真记录，身份一律用本地 UUID。

            为什么不信上游 id：实测 ollama 返回的是 `chatcmpl-222` 这种可复用的计数式 id，
            OpenAI 系虽唯一但无法对所有兼容后端打包票。上游 id 复用一次，就压掉一笔真实花费。
            代价只是：从本地日志重导时需要保留 UUID 字段才能去重 —— 而导出本身就带着它。
            """
            payload = {
                "provider": settings.upstream_base_url,
                "model": body.get("model", ""),
                "endpoint": "/v1/chat/completions",
                "ts": _now(),
                "status": status,
                "request_id": f"live-{uuid.uuid4().hex}",
                **attr,
                **(usage.as_dict() if usage else {}),
                "raw_cost_usd": raw_cost,
                "source": "proxy",
            }
            ledger.record_call(payload)

        if not stream:
            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                # 上游不可达也要记账 —— 否则「失败的调用」在账本里凭空消失
                record(None, status="upstream_unreachable")
                await client.aclose()
                return JSONResponse(
                    status_code=503,
                    content={"error": {
                        "type": "upstream_unreachable",
                        "message": f"上游不可达（{settings.upstream_base_url}）：{exc}",
                    }},
                )
            if resp.status_code >= 400:
                record(None, status=f"http_{resp.status_code}")
                await client.aclose()
                return JSONResponse(status_code=resp.status_code, content=_safe_json(resp.text))
            data = _safe_json(resp.text)
            usage = extract.parse_usage(data if isinstance(data, Mapping) else None)
            up_id = data.get("id") if isinstance(data, Mapping) else None
            record(usage, status="ok", upstream_id=up_id)
            await client.aclose()
            return JSONResponse(content=data, headers=warn_header)

        async def relay() -> AsyncIterator[bytes]:
            raw = bytearray()
            status = "ok"
            try:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        detail = await resp.aread()
                        record(None, status=f"http_{resp.status_code}")
                        yield detail
                        return
                    async for chunk in resp.aiter_bytes():
                        raw.extend(chunk)
                        yield chunk
            except Exception as exc:  # noqa: BLE001 - 断流也要记账，否则这笔花费凭空消失
                status = "stream_error"
                # 响应头已经发出去了，改不了状态码 —— 只能补一个可读的 SSE 错误事件
                err = {"error": {"type": "upstream_stream_error",
                                 "message": f"上游流中断：{exc}"}}
                yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode()
            finally:
                usage = extract.usage_from_sse(raw.decode("utf-8", errors="replace"))
                record(usage, status=status)
                await client.aclose()

        return StreamingResponse(relay(), media_type="text/event-stream", headers=warn_header)

    return app


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": {"message": text[:2000]}}
