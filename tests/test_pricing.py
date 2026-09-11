"""计费：价格表路由、缓存口径、未计价暴露。"""

import pytest
from conftest import rec

from llm_cost_ledger import pricing


class TestModelResolution:
    def test_exact_hit(self):
        assert pricing.resolve_model_key("deepseek-chat") == "deepseek-chat"

    def test_case_insensitive(self):
        assert pricing.resolve_model_key("DeepSeek-Chat") == "deepseek-chat"

    def test_prefix_variant(self):
        assert pricing.resolve_model_key("gpt-4o-mini-2024") == "gpt-4o-mini"

    def test_longest_match_wins(self):
        # gpt-4o 和 gpt-4o-mini 都能匹配，必须取更长的那个
        assert pricing.resolve_model_key("gpt-4o-mini-2024") != "gpt-4o"

    def test_unknown_model(self):
        assert pricing.resolve_model_key("totally-unknown-model") is None

    def test_empty(self):
        assert pricing.resolve_model_key("") is None


class TestQuote:
    def test_unknown_model_is_flagged_not_zeroed_silently(self):
        q = pricing.quote(rec(model="mystery-model"))
        assert q.unpriced is True
        assert q.cost_usd == 0.0
        assert q.priced_by == ""

    def test_basic_cost(self):
        # deepseek-chat: in 0.27 / out 1.10 per 1M
        q = pricing.quote(rec(prompt_tokens=1_000_000, completion_tokens=0))
        assert q.cost_usd == pytest.approx(0.27)
        assert q.unpriced is False

    def test_output_cost(self):
        q = pricing.quote(rec(prompt_tokens=0, completion_tokens=1_000_000))
        assert q.cost_usd == pytest.approx(1.10)

    def test_cached_tokens_billed_at_cache_rate(self):
        q = pricing.quote(rec(prompt_tokens=1_000_000, cached_tokens=1_000_000, completion_tokens=0))
        assert q.cost_usd == pytest.approx(0.07)

    def test_cached_excluded_from_full_price_input(self):
        # 50万缓存 + 50万全价 = 0.5*0.07 + 0.5*0.27
        q = pricing.quote(rec(prompt_tokens=1_000_000, cached_tokens=500_000, completion_tokens=0))
        assert q.cost_usd == pytest.approx(0.5 * 0.07 + 0.5 * 0.27)

    def test_cached_cannot_exceed_prompt(self):
        q = pricing.quote(rec(prompt_tokens=100, cached_tokens=999_999, completion_tokens=0))
        assert q.cost_usd == pytest.approx(100 / 1_000_000 * 0.07)

    def test_reasoning_not_double_billed_when_included_in_completion(self):
        base = pricing.quote(rec(completion_tokens=1000))
        with_r = pricing.quote(rec(completion_tokens=1000, reasoning_tokens=800))
        assert with_r.cost_usd == pytest.approx(base.cost_usd)

    def test_standalone_reasoning_is_billed(self):
        q = pricing.quote(rec(prompt_tokens=0, completion_tokens=0, reasoning_tokens=1_000_000))
        assert q.cost_usd == pytest.approx(1.10)

    def test_breakdown_sums_to_total(self):
        q = pricing.quote(rec(prompt_tokens=1234, completion_tokens=567, cached_tokens=123))
        assert sum(q.breakdown.values()) == pytest.approx(q.cost_usd)

    def test_free_local_model(self):
        q = pricing.quote(rec(model="qwen3.5:9b", prompt_tokens=10_000))
        assert q.cost_usd == 0.0
        assert q.unpriced is False  # 免费 != 未计价

    def test_priced_by_records_price_table_version(self):
        q = pricing.quote(rec())
        assert q.priced_by.startswith(pricing.PRICING_VERSION)

    def test_zero_tokens_zero_cost(self):
        q = pricing.quote(rec(prompt_tokens=0, completion_tokens=0))
        assert q.cost_usd == 0.0
        assert q.unpriced is False

    def test_known_models_non_empty(self):
        assert "deepseek-chat" in pricing.known_models()
