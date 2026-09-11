"""账本：幂等去重是核心，这里测「导入两次数字不变」。"""

import pytest
from conftest import rec

from llm_cost_ledger.store import Ledger


class TestIngestIdempotency:
    def test_first_import_inserts_all(self, ledger):
        r = ledger.ingest([rec(), rec(model="gpt-4o")])
        assert (r.seen, r.inserted, r.suppressed) == (2, 2, 0)

    def test_same_batch_twice_inserts_nothing(self, ledger):
        batch = [rec(), rec(model="gpt-4o"), rec(user_id="bob")]
        first = ledger.ingest(batch)
        second = ledger.ingest(batch)
        assert first.inserted == 3
        assert second.inserted == 0
        assert second.suppressed == 3
        assert ledger.count_calls() == 3

    def test_cost_unchanged_after_reimport(self, ledger):
        """AC2 —— 这条是整个项目的存在理由。"""
        batch = [rec(prompt_tokens=10_000, completion_tokens=2_000) for _ in range(3)]
        ledger.ingest(batch)
        before = ledger.total_cost()
        ledger.ingest(batch)
        after = ledger.total_cost()
        assert before == after
        assert before > 0

    def test_reimport_from_different_source_name_still_dedupes(self, ledger):
        """换文件名不算新数据 —— 对应历史事故里的日志轮转场景。"""
        ledger.ingest([rec()], source="file:app-2026-09-10.log")
        r2 = ledger.ingest([rec()], source="file:app-2026-09-11.log")
        assert r2.inserted == 0
        assert ledger.count_calls() == 1

    def test_partial_overlap_only_inserts_new(self, ledger):
        ledger.ingest([rec(), rec(model="gpt-4o")])
        r = ledger.ingest([rec(), rec(model="gpt-4o"), rec(model="deepseek-reasoner")])
        assert r.inserted == 1
        assert r.suppressed == 2
        assert ledger.count_calls() == 3

    def test_three_identical_calls_all_kept(self, ledger):
        r = ledger.ingest([rec(), rec(), rec()])
        assert r.inserted == 3
        assert ledger.count_calls() == 3

    def test_identical_calls_not_collapsed_on_reimport(self, ledger):
        ledger.ingest([rec(), rec()])
        r = ledger.ingest([rec(), rec()])
        assert r.inserted == 0
        assert ledger.count_calls() == 2

    def test_request_id_dedupes_across_different_content(self, ledger):
        ledger.ingest([{**rec(), "request_id": "r1"}])
        r = ledger.ingest([{**rec(model="gpt-4o"), "request_id": "r1"}])
        assert r.inserted == 0

    def test_empty_batch(self, ledger):
        r = ledger.ingest([])
        assert (r.seen, r.inserted) == (0, 0)

    def test_unpriced_counted(self, ledger):
        r = ledger.ingest([rec(model="mystery-model"), rec()])
        assert r.unpriced == 1


class TestBatchTraceability:
    def test_every_ingest_leaves_a_batch(self, ledger):
        ledger.ingest([rec()])
        ledger.ingest([rec()])
        assert len(ledger.batches()) == 2

    def test_batch_records_suppressed(self, ledger):
        ledger.ingest([rec()])
        ledger.ingest([rec()])
        batches = ledger.batches(limit=1)
        assert batches[0]["suppressed"] == 1
        assert batches[0]["seen"] == 1

    def test_batch_records_cost(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000, completion_tokens=0)])
        assert ledger.batches(limit=1)[0]["cost_usd"] == pytest.approx(0.27)

    def test_batch_finished_at_set(self, ledger):
        ledger.ingest([rec()])
        assert ledger.batches(limit=1)[0]["finished_at"]


class TestQueries:
    def test_total_cost(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000, completion_tokens=0)])
        assert ledger.total_cost() == pytest.approx(0.27)

    def test_count(self, ledger):
        ledger.ingest([rec(), rec(user_id="bob")])
        assert ledger.count_calls() == 2

    def test_spend_by_user(self, ledger):
        ledger.ingest([
            rec(user_id="alice", prompt_tokens=1_000_000, completion_tokens=0),
            rec(user_id="bob", prompt_tokens=500_000, completion_tokens=0),
        ])
        rows = {r["key"]: r for r in ledger.spend_by("user_id")}
        assert rows["alice"]["cost_usd"] == pytest.approx(0.27)
        assert rows["bob"]["cost_usd"] == pytest.approx(0.135)
        assert rows["alice"]["calls"] == 1

    def test_spend_by_model_grouping(self, ledger):
        ledger.ingest([rec(), rec(model="gpt-4o")])
        keys = {r["key"] for r in ledger.spend_by("model")}
        assert keys == {"deepseek-chat", "gpt-4o"}

    def test_spend_by_ts_buckets_by_day(self, ledger):
        ledger.ingest([rec(ts="2026-09-10T01:00:00"), rec(ts="2026-09-11T01:00:00")])
        assert {r["key"] for r in ledger.spend_by("ts")} == {"2026-09-10", "2026-09-11"}

    def test_filter_by_user(self, ledger):
        ledger.ingest([rec(user_id="alice"), rec(user_id="bob")])
        assert ledger.count_calls(user_id="alice") == 1

    def test_filter_by_feature(self, ledger):
        ledger.ingest([rec(feature="chat"), rec(feature="rag")])
        assert ledger.count_calls(feature="rag") == 1

    def test_filter_by_since(self, ledger):
        ledger.ingest([rec(ts="2026-09-10T00:00:00"), rec(ts="2026-09-11T00:00:00")])
        assert ledger.count_calls(since="2026-09-11T00:00:00") == 1

    def test_filter_by_until(self, ledger):
        ledger.ingest([rec(ts="2026-09-10T00:00:00"), rec(ts="2026-09-11T00:00:00")])
        assert ledger.count_calls(until="2026-09-10T23:59:59") == 1

    def test_tokens_summed(self, ledger):
        ledger.ingest([rec(prompt_tokens=100, completion_tokens=50)])
        assert ledger.spend_by("feature")[0]["tokens"] == 150

    def test_bad_dimension_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.spend_by("drop_table")

    def test_bad_filter_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.total_cost(nonsense="x")


class TestConcurrency:
    def test_schema_created_on_construction(self, tmp_path):
        led = Ledger(tmp_path / "x.db")
        assert led.count_calls() == 0
        led.close()

    def test_same_ledger_object_reused_across_threads(self, tmp_path):
        import threading

        led = Ledger(tmp_path / "t.db")
        errors = []

        def work(n):
            try:
                led.ingest([rec(user_id=f"u{n}", ts=f"2026-09-11T10:0{n}:00")])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert led.count_calls() == 5
        led.close()
