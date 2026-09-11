"""用量提取：三种厂商口径 + 流式。"""

import json

from llm_cost_ledger.extract import Usage, parse_usage, request_meta, usage_from_sse


class TestOpenAIFormat:
    def test_standard_usage(self):
        u = parse_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 20}})
        assert (u.prompt_tokens, u.completion_tokens) == (10, 20)

    def test_cached_tokens_from_details(self):
        u = parse_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 5,
                                   "prompt_tokens_details": {"cached_tokens": 64}}})
        assert u.cached_tokens == 64

    def test_reasoning_tokens_from_details(self):
        u = parse_usage({"usage": {"prompt_tokens": 5, "completion_tokens": 50,
                                   "completion_tokens_details": {"reasoning_tokens": 30}}})
        assert u.reasoning_tokens == 30

    def test_total_tokens_property(self):
        u = parse_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 20}})
        assert u.total_tokens == 30

    def test_is_empty_helper(self):
        assert Usage().is_empty() is True
        assert Usage(prompt_tokens=1).is_empty() is False


class TestDeepSeekFormat:
    def test_cache_hit_tokens(self):
        u = parse_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                   "prompt_cache_hit_tokens": 80}})
        assert u.cached_tokens == 80

    def test_no_cache_field(self):
        u = parse_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 10}})
        assert u.cached_tokens == 0


class TestOllamaFormat:
    def test_top_level_counters(self):
        u = parse_usage({"prompt_eval_count": 30, "eval_count": 12})
        assert (u.prompt_tokens, u.completion_tokens) == (30, 12)

    def test_zero_ollama_usage_returns_none(self):
        assert parse_usage({"prompt_eval_count": 0, "eval_count": 0}) is None


class TestMissingUsage:
    def test_no_usage_key(self):
        assert parse_usage({"choices": []}) is None

    def test_none(self):
        assert parse_usage(None) is None

    def test_non_mapping(self):
        assert parse_usage("nope") is None

    def test_usage_not_a_dict(self):
        assert parse_usage({"usage": 5}) is None

    def test_zero_tokens_returns_none_not_zero(self):
        """提取不到用量必须返回 None —— 不能伪装成「免费调用」。"""
        assert parse_usage({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}) is None


class TestStreaming:
    def _sse(self, *chunks):
        lines = []
        for c in chunks:
            lines.append("data: " + json.dumps(c))
            lines.append("")
        lines.append("data: [DONE]")
        return "\n".join(lines)

    def test_picks_up_final_usage_chunk(self):
        body = self._sse(
            {"choices": [{"delta": {"content": "hi"}}]},
            {"usage": {"prompt_tokens": 7, "completion_tokens": 9}},
        )
        u = usage_from_sse(body)
        assert (u.prompt_tokens, u.completion_tokens) == (7, 9)

    def test_no_usage_in_stream(self):
        body = self._sse({"choices": [{"delta": {"content": "hi"}}]})
        assert usage_from_sse(body) is None

    def test_ignores_malformed_lines(self):
        body = "data: {not json}\n\ndata: " + json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 2}})
        assert usage_from_sse(body).prompt_tokens == 1

    def test_empty_body(self):
        assert usage_from_sse("") is None

    def test_last_usage_wins(self):
        body = self._sse(
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            {"usage": {"prompt_tokens": 99, "completion_tokens": 99}},
        )
        assert usage_from_sse(body).prompt_tokens == 99


class TestRequestMeta:
    def test_meta(self):
        m = request_meta({"model": "deepseek-chat", "stream": True})
        assert m == {"model": "deepseek-chat", "stream": True}

    def test_meta_defaults(self):
        m = request_meta({})
        assert m == {"model": "", "stream": False}
