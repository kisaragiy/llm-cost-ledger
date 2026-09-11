"""端到端：代理转发 + 记账 + 预算熔断 + 账本 API。

用 httpx.MockTransport 伪造上游 —— 不打真实 API，测试可复现且不花钱。
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_cost_ledger.app import create_app
from llm_cost_ledger.config import Settings
from llm_cost_ledger.store import Ledger


class FakeUpstream:
    """可控的上游：记录收到的请求，返回预设响应。"""

    def __init__(self, payload=None, status=200, sse=None):
        self.payload = payload or {
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "你好"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        }
        self.status = status
        self.sse = sse
        self.calls = []
        self.headers_seen = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append(body)
        self.headers_seen.append(dict(request.headers))
        if self.sse is not None:
            return httpx.Response(200, content=self.sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(self.status, json=self.payload)


def make_client(tmp_path, fake=None, rules=None, **setting_kwargs):
    fake = fake or FakeUpstream()
    settings = Settings(
        upstream_base_url="https://fake.upstream",
        upstream_api_key="sk-test",
        ledger_db=str(tmp_path / "app.db"),
        budget_rules=rules or [],
        **setting_kwargs,
    )
    ledger = Ledger(settings.ledger_db)
    app = create_app(settings, ledger, transport=httpx.MockTransport(fake.handler))
    return TestClient(app), ledger, fake


class TestHealthAndConfig:
    def test_health(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_reports_costs(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        r = client.get("/health")
        assert r.json()["calls_recorded"] == 1
        assert r.json()["total_cost_usd"] == pytest.approx(0.00027 + 0.00055)

    def test_config_hides_key(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        body = client.get("/v1/ledger/config").json()
        assert "upstream_api_key" not in body
        assert body["upstream_key_set"] is True


class TestProxyForwarding:
    def test_forwards_and_returns_body(self, tmp_path):
        client, _, fake = make_client(tmp_path)
        r = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "你好"
        assert fake.calls[0]["model"] == "deepseek-chat"

    def test_injects_upstream_auth_header(self, tmp_path):
        client, _, fake = make_client(tmp_path)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert fake.headers_seen[0]["authorization"] == "Bearer sk-test"

    def test_records_cost_from_real_usage(self, tmp_path):
        client, ledger, _ = make_client(tmp_path)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert ledger.count_calls() == 1
        # 1000 in * 0.27/1M + 500 out * 1.10/1M
        assert ledger.total_cost() == pytest.approx(0.00027 + 0.00055)

    def test_attribution_from_headers(self, tmp_path):
        client, ledger, _ = make_client(tmp_path)
        client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": []},
            headers={"x-ledger-user": "alice", "x-ledger-feature": "rag"},
        )
        assert ledger.count_calls(user_id="alice", feature="rag") == 1

    def test_attribution_from_body_metadata(self, tmp_path):
        client, ledger, _ = make_client(tmp_path)
        client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": [],
                  "metadata": {"user_id": "bob", "feature": "chat"}},
        )
        assert ledger.count_calls(user_id="bob", feature="chat") == 1

    def test_header_beats_body(self, tmp_path):
        client, ledger, _ = make_client(tmp_path)
        client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": [], "metadata": {"user_id": "body"}},
            headers={"x-ledger-user": "header"},
        )
        assert ledger.count_calls(user_id="header") == 1

    def test_default_user_when_nothing_given(self, tmp_path):
        client, ledger, _ = make_client(tmp_path, default_user="svc")
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert ledger.count_calls(user_id="svc") == 1

    def test_upstream_error_is_passed_through_and_recorded(self, tmp_path):
        fake = FakeUpstream(status=429, payload={"error": {"message": "rate limited"}})
        client, ledger, _ = make_client(tmp_path, fake=fake)
        r = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert r.status_code == 429
        assert ledger.count_calls() == 1
        assert ledger.spend_by("feature") is not None

    def test_missing_usage_records_unpriced_zero(self, tmp_path):
        fake = FakeUpstream(payload={"choices": []})  # 没有 usage
        client, ledger, _ = make_client(tmp_path, fake=fake)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert ledger.count_calls() == 1
        assert ledger.total_cost() == 0.0


class TestProxyAuth:
    def test_401_without_key(self, tmp_path):
        client, _, _ = make_client(tmp_path, proxy_auth_key="secret")
        r = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
        assert r.status_code == 401
        assert "鉴权" in r.json()["detail"]

    def test_200_with_key(self, tmp_path):
        client, _, _ = make_client(tmp_path, proxy_auth_key="secret")
        r = client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": []},
            headers={"authorization": "Bearer secret"},
        )
        assert r.status_code == 200

    def test_ledger_api_protected_too(self, tmp_path):
        client, _, _ = make_client(tmp_path, proxy_auth_key="secret")
        assert client.get("/v1/ledger/summary").status_code == 401


class TestBudgetFuse:
    def test_stop_blocks_before_upstream_call(self, tmp_path):
        """AC4 —— 熔断必须发生在上游调用【之前】。"""
        client, ledger, fake = make_client(
            tmp_path, rules=[{"scope": "global", "window": "total", "limit_usd": 0.0001}]
        )
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        r2 = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert r2.status_code == 402
        assert "预算" in r2.json()["error"]["message"]
        assert r2.json()["error"]["type"] == "budget_exceeded"
        # 第二次请求根本没打上游
        assert len(fake.calls) == 1

    def test_warn_passes_with_header(self, tmp_path):
        # 单次调用花 $0.00082；上限 $0.001 -> 第 2 次调用前已用 82%，越过 80% 告警线。
        # 注意：告警头反映的是【本次调用之前】的状态 —— 第 1 次当然是 ok。
        client, _, _ = make_client(
            tmp_path, rules=[{"scope": "global", "window": "total", "limit_usd": 0.001}]
        )
        first = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        second = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert first.status_code == 200
        assert first.headers.get("x-ledger-budget-tier") is None
        assert second.status_code == 200
        assert second.headers.get("x-ledger-budget-tier") == "warn"
        assert second.headers.get("x-ledger-budget-message-encoding") == "percent"
        from urllib.parse import unquote
        assert "预算告警" in unquote(second.headers.get("x-ledger-budget-message"))

    def test_below_warn_has_no_header(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, rules=[{"scope": "global", "window": "total", "limit_usd": 100}]
        )
        r = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert r.status_code == 200
        assert r.headers.get("x-ledger-budget-tier") is None

    def test_user_scoped_budget_only_hits_that_user(self, tmp_path):
        rules = [{"scope": "user", "key": "alice", "window": "total", "limit_usd": 0.0001}]
        client, _, _ = make_client(tmp_path, rules=rules)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []},
                    headers={"x-ledger-user": "alice"})
        blocked = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []},
                              headers={"x-ledger-user": "alice"})
        allowed = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []},
                              headers={"x-ledger-user": "bob"})
        assert blocked.status_code == 402
        assert allowed.status_code == 200


class TestStreamingProxy:
    SSE = (
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        'data: {"usage":{"prompt_tokens":1000,"completion_tokens":500}}\n\n'
        "data: [DONE]\n\n"
    )

    def test_stream_relayed_and_billed(self, tmp_path):
        fake = FakeUpstream(sse=self.SSE)
        client, ledger, _ = make_client(tmp_path, fake=fake)
        r = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": [], "stream": True})
        assert r.status_code == 200
        assert "你" in r.text
        assert ledger.count_calls() == 1
        assert ledger.total_cost() == pytest.approx(0.00027 + 0.00055)

    def test_stream_options_injected(self, tmp_path):
        fake = FakeUpstream(sse=self.SSE)
        client, _, _ = make_client(tmp_path, fake=fake)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": [], "stream": True})
        assert fake.calls[0].get("stream_options") == {"include_usage": True}

    def test_stream_without_usage_still_recorded(self, tmp_path):
        fake = FakeUpstream(sse='data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n')
        client, ledger, _ = make_client(tmp_path, fake=fake)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": [], "stream": True})
        assert ledger.count_calls() == 1


class TestLedgerEndpoints:
    def test_summary_dimensions(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        for user, feature in (("alice", "chat"), ("bob", "rag")):
            client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []},
                        headers={"x-ledger-user": user, "x-ledger-feature": feature})
        body = client.get("/v1/ledger/summary").json()
        assert body["calls"] == 2
        assert {r["key"] for r in body["by_user"]} == {"alice", "bob"}
        assert {r["key"] for r in body["by_feature"]} == {"chat", "rag"}

    def test_spend_by_model(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        body = client.get("/v1/ledger/spend?by=model").json()
        assert body["rows"][0]["key"] == "deepseek-chat"

    def test_ingest_endpoint_dedupes(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        records = [{"model": "deepseek-chat", "ts": "2026-09-11T00:00:00",
                    "prompt_tokens": 10, "completion_tokens": 5}]
        first = client.post("/v1/ledger/ingest", json={"records": records}).json()
        second = client.post("/v1/ledger/ingest", json={"records": records}).json()
        assert first["inserted"] == 1
        assert second["inserted"] == 0

    def test_ingest_bad_payload(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        assert client.post("/v1/ledger/ingest", json={}).status_code == 400

    def test_batches_listed(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        client.post("/v1/ledger/ingest", json={"records": [{"model": "deepseek-chat", "ts": "2026-09-11T00:00:00",
                                                             "prompt_tokens": 1, "completion_tokens": 1}]})
        assert len(client.get("/v1/ledger/batches").json()["batches"]) == 1

    def test_pricing_endpoint(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        body = client.get("/v1/ledger/pricing").json()
        assert "deepseek-chat" in body["models"]


class TestProxyNeverDropsRealCalls:
    """回归：实时流量的每一条都必须落账。

    曾经的 bug：实时调用逐条入库，每条自成一个批次 -> occurrence 恒为 0 ->
    同一秒内内容相同的 N 次真实调用被当成「重复导入」压掉，只留 1 条 = 少记账。
    上游 id 也不能当身份 —— 实测 ollama 返回可复用的 `chatcmpl-222`。
    """

    def test_three_identical_calls_all_recorded(self, tmp_path):
        client, ledger, _ = make_client(tmp_path)
        for _ in range(3):
            client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert ledger.count_calls() == 3

    def test_repeated_same_upstream_id_still_all_recorded(self, tmp_path):
        """上游复用 id 也不许压掉任何一条。"""
        fake = FakeUpstream(payload={"id": "chatcmpl-1", "choices": [],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        client, ledger, _ = make_client(tmp_path, fake=fake)
        for _ in range(5):
            client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert ledger.count_calls() == 5
        assert ledger.total_cost() > 0

    def test_cost_scales_with_call_count(self, tmp_path):
        client, ledger, _ = make_client(tmp_path)
        one = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert one.status_code == 200
        after_one = ledger.total_cost()
        for _ in range(4):
            client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert ledger.total_cost() == pytest.approx(after_one * 5)

    def test_distinct_users_same_content_both_kept(self, tmp_path):
        client, ledger, _ = make_client(tmp_path, default_user="")
        for u in ("alice", "alice", "bob"):
            client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []},
                        headers={"x-ledger-user": u})
        assert ledger.count_calls() == 3
        assert ledger.count_calls(user_id="alice") == 2, "同一用户同一秒的两次真实调用都要记账"

    def test_reimport_of_exported_proxy_records_still_dedupes(self, tmp_path):
        """反面：带着 request_id 的记录重导，仍应幂等 —— 去重能力没被削弱。"""
        client, ledger, _ = make_client(tmp_path)
        for _ in range(3):
            client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        rows = [dict(r) for r in ledger._conn().execute(
            "SELECT * FROM calls").fetchall()]  # noqa: SLF001
        exported = [
            {
                "request_id": r["call_key"].split("req:", 1)[-1],
                "provider": r["provider"], "model": r["model"], "ts": r["ts"],
                "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
                "user_id": r["user_id"], "feature": r["feature"], "status": r["status"],
            }
            for r in rows
        ]
        report = ledger.ingest(exported, source="reimport")
        assert report.inserted == 0
        assert ledger.count_calls() == 3


class TestDashboard:
    """看板：页面本身 + 背后的聚合端点。"""

    def test_dashboard_served(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        r = client.get("/")
        assert r.status_code == 200
        assert "账目控制台" in r.text
        assert r.headers["content-type"].startswith("text/html")

    def test_dashboard_has_no_external_dependencies(self, tmp_path):
        """AC3 —— 断网可用：页面不得引用任何外部主机。"""
        import re

        client, _, _ = make_client(tmp_path)
        html = client.get("/").text
        external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)
        external += re.findall(r'@import\s+url\(["\']?(https?://[^)"\']+)', html)
        assert external == [], f"页面引用了外部资源：{external}"

    def test_dashboard_renders_without_inline_cdn_fonts(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        html = client.get("/").text
        assert "fonts.googleapis" not in html and "cdn." not in html

    def test_dashboard_does_not_need_auth_to_load_page(self, tmp_path):
        """页面本身公开；数据接口才要鉴权 —— 否则用户没法输密钥。"""
        client, _, _ = make_client(tmp_path, proxy_auth_key="secret")
        assert client.get("/").status_code == 200
        assert client.get("/v1/ledger/overview").status_code == 401

    def test_overview_shape(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        body = client.get("/v1/ledger/overview?days=7").json()
        assert set(body) == {"totals", "series", "by_model", "by_user", "by_feature", "budget", "alerts"}
        assert len(body["series"]) == 7

    def test_overview_days_param_respected(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        assert len(client.get("/v1/ledger/overview?days=45").json()["series"]) == 45

    def test_overview_rejects_bad_days(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        assert client.get("/v1/ledger/overview?days=0").status_code == 422
        assert client.get("/v1/ledger/overview?days=9999").status_code == 422

    def test_overview_totals_match_calls(self, tmp_path):
        client, ledger, _ = make_client(tmp_path)
        for _ in range(3):
            client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        t = client.get("/v1/ledger/overview").json()["totals"]
        assert t["calls"] == 3
        assert t["all_time"] == pytest.approx(ledger.total_cost())

    def test_budget_endpoint(self, tmp_path):
        client, _, _ = make_client(
            tmp_path, rules=[{"scope": "global", "window": "total", "limit_usd": 10}]
        )
        rules = client.get("/v1/ledger/budget").json()["rules"]
        assert len(rules) == 1 and rules[0]["limit_usd"] == 10

    def test_alerts_endpoint(self, tmp_path):
        client, _, _ = make_client(tmp_path)
        assert "unpriced" in client.get("/v1/ledger/alerts").json()


class TestUpstreamFailures:
    def test_unreachable_upstream_returns_503_with_chinese(self, tmp_path):
        """上游连不上时不能给裸 500 —— 要说清楚是哪儿连不上。"""
        def boom(request):
            raise httpx.ConnectError("connection refused")

        settings = Settings(
            upstream_base_url="http://127.0.0.1:1",
            ledger_db=str(tmp_path / "u.db"),
        )
        ledger = Ledger(settings.ledger_db)
        app = create_app(settings, ledger, transport=httpx.MockTransport(boom))
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert r.status_code == 503
        assert r.json()["error"]["type"] == "upstream_unreachable"
        assert "上游不可达" in r.json()["error"]["message"]
        # 失败的调用也必须落账，否则账本与实际尝试不符
        assert ledger.count_calls() == 1

    def test_unreachable_upstream_recorded_with_failure_status(self, tmp_path):
        def boom(request):
            raise httpx.ConnectError("nope")

        settings = Settings(upstream_base_url="http://127.0.0.1:1", ledger_db=str(tmp_path / "u2.db"))
        ledger = Ledger(settings.ledger_db)
        app = create_app(settings, ledger, transport=httpx.MockTransport(boom))
        client = TestClient(app, raise_server_exceptions=False)
        client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": []})
        assert ledger.count_calls(status="upstream_unreachable") == 1

    def test_stream_error_yields_sse_event_not_bare_break(self, tmp_path):
        class Boom(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("stream boom")

        settings = Settings(upstream_base_url="http://127.0.0.1:1", ledger_db=str(tmp_path / "u3.db"))
        ledger = Ledger(settings.ledger_db)
        app = create_app(settings, ledger, transport=Boom())
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/v1/chat/completions",
                        json={"model": "deepseek-chat", "messages": [], "stream": True})
        assert "upstream_stream_error" in r.text or "上游流中断" in r.text


class TestMissingUpstreamConfig:
    def test_fails_loudly_when_upstream_missing(self, tmp_path):
        settings = Settings(upstream_base_url="", ledger_db=str(tmp_path / "x.db"))
        ledger = Ledger(settings.ledger_db)
        app = create_app(settings, ledger)
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
        assert r.status_code == 503
        assert "UPSTREAM_BASE_URL" in r.json()["error"]["message"]
