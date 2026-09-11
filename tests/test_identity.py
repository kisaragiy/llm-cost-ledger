"""身份键 —— 项目的立身之本，测最狠。"""

from conftest import rec

from llm_cost_ledger.identity import (
    assign_identities,
    dedupe_keys,
    fingerprint,
    normalize_ts,
)


class TestTimeNormalization:
    def test_iso_with_tz_to_utc(self):
        assert normalize_ts("2026-09-11T18:00:00+08:00") == "2026-09-11T10:00:00"

    def test_zulu_suffix(self):
        assert normalize_ts("2026-09-11T10:00:00Z") == "2026-09-11T10:00:00"

    def test_millis_format(self):
        assert normalize_ts("2026-09-11 10:00:00.123") == "2026-09-11T10:00:00"

    def test_epoch_seconds(self):
        assert normalize_ts(1789120800) == "2026-09-11T10:00:00"

    def test_epoch_millis(self):
        assert normalize_ts(1789120800000) == "2026-09-11T10:00:00"

    def test_unparseable_is_preserved_not_dropped(self):
        # 宁可留脏值让人在对账里看见，也不静默变成空串
        assert normalize_ts("not-a-date") == "not-a-date"

    def test_none_and_empty(self):
        assert normalize_ts(None) == ""
        assert normalize_ts("") == ""


class TestFingerprint:
    def test_same_content_same_fingerprint(self):
        assert fingerprint(rec()) == fingerprint(rec())

    def test_fingerprint_ignores_source_file_and_line(self):
        # 这是历史事故的核心：换文件、换行号，指纹不能变
        a = fingerprint({**rec(), "source_file": "a.log", "line_no": 10})
        b = fingerprint({**rec(), "source_file": "b.log", "line_no": 9999})
        assert a == b
        assert fingerprint(rec()) == a

    def test_model_change_changes_fingerprint(self):
        assert fingerprint(rec()) != fingerprint(rec(model="deepseek-reasoner"))

    def test_token_change_changes_fingerprint(self):
        assert fingerprint(rec()) != fingerprint(rec(prompt_tokens=1001))

    def test_cached_token_change_changes_fingerprint(self):
        assert fingerprint(rec()) != fingerprint(rec(cached_tokens=200))

    def test_ts_millisecond_variance_collapses(self):
        a = fingerprint(rec(ts="2026-09-11T10:00:00.111"))
        b = fingerprint(rec(ts="2026-09-11T10:00:00.999"))
        assert a == b

    def test_ts_different_second_differs(self):
        assert fingerprint(rec(ts="2026-09-11T10:00:00")) != fingerprint(rec(ts="2026-09-11T10:00:01"))

    def test_status_normalized(self):
        assert fingerprint(rec(status="OK")) == fingerprint(rec(status=" ok "))

    def test_missing_field_treated_as_empty(self):
        r1 = dict(rec())
        r2 = dict(rec())
        del r1["agent_run"]
        assert fingerprint(r1) == fingerprint(r2)

    def test_token_string_coerced(self):
        assert fingerprint(rec(prompt_tokens="1000")) == fingerprint(rec(prompt_tokens=1000))


class TestOccurrence:
    def test_single_record_gets_index_zero(self):
        out = assign_identities([rec()])
        assert out[0].occurrence == 0

    def test_identical_records_in_one_batch_both_kept(self):
        # 真·重复调用不能被误去重
        out = assign_identities([rec(), rec()])
        assert [r.occurrence for r in out] == [0, 1]
        assert out[0].call_key != out[1].call_key

    def test_three_identical_records(self):
        out = assign_identities([rec(), rec(), rec()])
        assert [r.occurrence for r in out] == [0, 1, 2]

    def test_interleaved_records_keep_independent_counters(self):
        a, b = rec(), rec(model="gpt-4o")
        out = assign_identities([a, b, a, b, a])
        assert [r.occurrence for r in out] == [0, 0, 1, 1, 2]

    def test_call_key_format(self):
        out = assign_identities([rec()])
        assert out[0].call_key == f"{out[0].fingerprint}:0"

    def test_request_id_takes_priority(self):
        out = assign_identities([rec(request_id="req-abc")])
        assert out[0].call_key == "req:req-abc"
        assert out[0].fingerprint == "req:req-abc"

    def test_request_id_same_for_different_payloads_dedupes(self):
        out = assign_identities([rec(request_id="r1"), rec(model="gpt-4o", request_id="r1")])
        assert out[0].call_key == out[1].call_key

    def test_request_id_and_fingerprint_do_not_collide(self):
        out = assign_identities([rec(), rec(request_id="r1")])
        assert out[0].call_key != out[1].call_key

    def test_dedupe_keys_helper(self):
        keys = dedupe_keys([rec(), rec()])
        assert len(keys) == 2 and keys[0] != keys[1]
