"""对账五道检查：必须能抓出问题，也必须不误报。"""

from conftest import rec

from llm_cost_ledger.reconcile import run_reconcile
from llm_cost_ledger.store import Ledger


class TestCleanLedger:
    def test_clean_ledger_passes(self, ledger):
        ledger.ingest([rec(), rec(model="gpt-4o", user_id="bob")])
        report = run_reconcile(ledger)
        assert report.ok is True
        assert report.findings == []

    def test_empty_ledger_passes(self, ledger):
        report = run_reconcile(ledger)
        assert report.ok is True
        assert report.totals["calls"] == 0

    def test_totals_reported(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000, completion_tokens=0)])
        r = run_reconcile(ledger)
        assert r.totals["cost_usd"] == 0.27
        assert r.totals["calls"] == 1

    def test_reimport_keeps_totals_identical(self, ledger):
        batch = [rec(prompt_tokens=1_000_000) for _ in range(4)]
        ledger.ingest(batch)
        first = run_reconcile(ledger).totals
        ledger.ingest(batch)
        second = run_reconcile(ledger).totals
        assert first["cost_usd"] == second["cost_usd"]
        assert first["calls"] == second["calls"]
        assert second["suppressed"] > 0


class TestC1IdentityUniqueness:
    def test_duplicate_identity_is_an_error(self, ledger):
        ledger.ingest([rec()])
        # 绕过主键约束，模拟「去重被破坏」的账本
        ledger._conn().execute(
            "INSERT INTO calls (call_key, fingerprint, occurrence, ts, cost_usd, ingested_at)"
            " VALUES ('dup', 'FP', 0, '2026-09-11T00:00:00', 1.0, '2026-09-11T00:00:00')"
        )
        ledger._conn().execute(
            "INSERT INTO calls (call_key, fingerprint, occurrence, ts, cost_usd, ingested_at)"
            " VALUES ('dup2', 'FP', 0, '2026-09-11T00:00:00', 1.0, '2026-09-11T00:00:00')"
        )
        ledger._conn().commit()
        report = run_reconcile(ledger)
        assert report.ok is False
        assert any(f.code == "C1" for f in report.findings)


class TestC2Recompute:
    def test_tampered_cost_is_caught(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000)])
        ledger._conn().execute("UPDATE calls SET cost_usd = 999.0")
        ledger._conn().commit()
        report = run_reconcile(ledger)
        assert report.ok is False
        assert any(f.code == "C2" for f in report.findings)

    def test_untampered_passes(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000, completion_tokens=500)])
        assert not any(f.code == "C2" for f in run_reconcile(ledger).findings)


class TestC3Unpriced:
    def test_unpriced_model_flagged(self, ledger):
        ledger.ingest([rec(model="mystery-model")])
        report = run_reconcile(ledger)
        assert any(f.code == "C3" for f in report.findings)
        assert report.totals["unpriced_calls"] == 1

    def test_no_unpriced_no_finding(self, ledger):
        ledger.ingest([rec()])
        assert not any(f.code == "C3" for f in run_reconcile(ledger).findings)


class TestC4BatchTraceability:
    def test_calls_without_batch_flagged(self, ledger):
        ledger._conn().execute(
            "INSERT INTO calls (call_key, fingerprint, occurrence, ts, cost_usd, ingested_at)"
            " VALUES ('x', 'FP2', 0, '2026-09-11T00:00:00', 1.0, '2026-09-11T00:00:00')"
        )
        ledger._conn().commit()
        report = run_reconcile(ledger)
        assert any(f.code == "C4" for f in report.findings)

    def test_high_suppression_ratio_flagged(self, ledger):
        ledger.ingest([rec()])
        for _ in range(5):
            ledger.ingest([rec()])  # 5 次全部压制
        report = run_reconcile(ledger)
        assert any(f.code == "C4" for f in report.findings)


class TestC5PriceDrift:
    def test_drift_detected(self, ledger):
        # 上游实际收了 10 倍，我们的价格表明显过期
        ledger.ingest([rec(prompt_tokens=1_000_000, raw_cost_usd=2.70)])
        report = run_reconcile(ledger)
        assert any(f.code == "C5" for f in report.findings)

    def test_small_drift_tolerated(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000, raw_cost_usd=0.28)])  # ~4% 偏差
        assert not any(f.code == "C5" for f in run_reconcile(ledger).findings)

    def test_no_raw_cost_no_drift_check(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000)])
        assert not any(f.code == "C5" for f in run_reconcile(ledger).findings)


class TestReportRendering:
    def test_render_contains_totals(self, ledger):
        ledger.ingest([rec()])
        text = run_reconcile(ledger).render()
        assert "调用数" in text and "总花费" in text

    def test_render_pass_line(self, ledger):
        ledger.ingest([rec()])
        assert "全部通过" in run_reconcile(ledger).render()

    def test_as_dict_shape(self, ledger):
        d = run_reconcile(ledger).as_dict()
        assert set(d) == {"ok", "totals", "findings"}
