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
