"""看板聚合层：趋势补零、预算状态、告警。"""

from datetime import date, timedelta

import pytest
from conftest import rec

from llm_cost_ledger import insights
from llm_cost_ledger.budget import BudgetRule


class TestDailySeries:
    def test_length_matches_requested_days(self, ledger):
        assert len(insights.daily_series(ledger, days=7)) == 7

    def test_single_day(self, ledger):
        assert len(insights.daily_series(ledger, days=1)) == 1

    def test_missing_days_filled_with_zero(self, ledger):
        """空档必须补 0 —— 否则图表会跳过没调用的日子，把趋势形状抹平。"""
        today = date(2026, 9, 11)
        ledger.ingest([rec(ts="2026-09-09T10:00:00")])  # 只在中段有数据
        series = insights.daily_series(ledger, days=5, until=today)
        assert [d["date"] for d in series] == [
            "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
        ]
        assert [d["calls"] for d in series] == [0, 0, 1, 0, 0]

    def test_aggregates_by_day(self, ledger):
        ledger.ingest([
            rec(ts="2026-09-10T01:00:00", prompt_tokens=1000, completion_tokens=0),
            rec(ts="2026-09-10T20:00:00", prompt_tokens=1000, completion_tokens=0),
            rec(ts="2026-09-11T01:00:00", prompt_tokens=1000, completion_tokens=0),
        ])
        series = {d["date"]: d for d in insights.daily_series(ledger, days=3, until=date(2026, 9, 11))}
        assert series["2026-09-10"]["calls"] == 2
        assert series["2026-09-10"]["cost_usd"] == pytest.approx(0.00054)
        assert series["2026-09-11"]["calls"] == 1

    def test_tokens_summed(self, ledger):
        ledger.ingest([rec(ts="2026-09-11T01:00:00", prompt_tokens=100, completion_tokens=50)])
        series = insights.daily_series(ledger, days=1, until=date(2026, 9, 11))
        assert series[0]["tokens"] == 150

    def test_days_clamped_to_valid_range(self, ledger):
        assert len(insights.daily_series(ledger, days=0)) == 1
        assert len(insights.daily_series(ledger, days=99999)) == insights.MAX_DAYS

    def test_series_is_chronological(self, ledger):
        series = insights.daily_series(ledger, days=5, until=date(2026, 9, 11))
        dates = [d["date"] for d in series]
        assert dates == sorted(dates)

    def test_last_day_is_until(self, ledger):
        series = insights.daily_series(ledger, days=3, until=date(2026, 9, 11))
        assert series[-1]["date"] == "2026-09-11"

    def test_data_outside_window_excluded(self, ledger):
        ledger.ingest([rec(ts="2026-01-01T00:00:00")])
        series = insights.daily_series(ledger, days=3, until=date(2026, 9, 11))
        assert sum(d["calls"] for d in series) == 0


class TestTotals:
    def test_empty_ledger(self, ledger):
        t = insights.totals(ledger)
        assert t["all_time"] == 0.0 and t["calls"] == 0

    def test_all_time_and_calls(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000, completion_tokens=0)])
        t = insights.totals(ledger)
        assert t["all_time"] == pytest.approx(0.27)
        assert t["calls"] == 1

    def test_window_keys_present(self, ledger):
        assert set(insights.totals(ledger)) == {
            "all_time", "calls", "today", "this_month", "rolling_24h",
        }

    def test_old_record_outside_rolling_window(self, ledger):
        ledger.ingest([rec(ts="2020-01-01T00:00:00", prompt_tokens=1_000_000, completion_tokens=0)])
        t = insights.totals(ledger)
        assert t["all_time"] == pytest.approx(0.27)
        assert t["rolling_24h"] == 0.0


class TestBudgetStatus:
    def test_no_rules(self, ledger):
        assert insights.budget_status(ledger, []) == []

    def test_rule_fields(self, ledger):
        rules = [BudgetRule(scope="global", window="total", limit_usd=10)]
        out = insights.budget_status(ledger, rules)
        assert set(out[0]) == {
            "scope", "key", "window", "spent_usd", "limit_usd", "ratio", "tier", "error",
        }

    def test_tier_ok_when_no_spend(self, ledger):
        out = insights.budget_status(ledger, [BudgetRule(scope="global", window="total", limit_usd=10)])
        assert out[0]["tier"] == "ok" and out[0]["ratio"] == 0.0

    def test_tier_stop_when_over(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000, completion_tokens=0)])
        out = insights.budget_status(ledger, [BudgetRule(scope="global", window="total", limit_usd=0.1)])
        assert out[0]["tier"] == "stop"
        assert out[0]["spent_usd"] == pytest.approx(0.27)

    def test_ratio_capped_at_one(self, ledger):
        ledger.ingest([rec(prompt_tokens=10_000_000, completion_tokens=0)])
        out = insights.budget_status(ledger, [BudgetRule(scope="global", window="total", limit_usd=0.1)])
        assert out[0]["ratio"] == 1.0

    def test_user_scoped_rule_reads_that_user(self, ledger):
        ledger.ingest([rec(user_id="alice", prompt_tokens=1_000_000, completion_tokens=0)])
        rules = [BudgetRule(scope="user", key="alice", window="total", limit_usd=10)]
        assert insights.budget_status(ledger, rules)[0]["spent_usd"] == pytest.approx(0.27)

    def test_multiple_rules_all_reported(self, ledger):
        rules = [
            BudgetRule(scope="global", window="day", limit_usd=5),
            BudgetRule(scope="user", key="bob", window="day", limit_usd=1),
        ]
        assert len(insights.budget_status(ledger, rules)) == 2


class TestAlerts:
    def test_clean_ledger_has_no_alerts(self, ledger):
        ledger.ingest([rec()])
        a = insights.alerts(ledger)
        assert a["unpriced"] == [] and a["drift"] == [] and a["failed_calls"] == []

    def test_unpriced_listed(self, ledger):
        ledger.ingest([rec(model="mystery-1"), rec(model="mystery-1"), rec(model="mystery-2")])
        a = insights.alerts(ledger)
        assert a["unpriced_total"] == 3
        assert a["unpriced"][0]["calls"] == 2  # 按出现次数降序

    def test_drift_grouped_by_model_not_repeated(self, ledger):
        """同一模型漂移一片时只报最差一条，不刷屏。"""
        ledger.ingest([
            rec(prompt_tokens=1_000_000, completion_tokens=0, raw_cost_usd=10.0),
            rec(ts="2026-09-11T11:00:00", prompt_tokens=1_000_000, completion_tokens=0, raw_cost_usd=5.0),
            rec(ts="2026-09-11T12:00:00", prompt_tokens=1_000_000, completion_tokens=0, raw_cost_usd=1.0),
        ])
        drift = insights.alerts(ledger)["drift"]
        assert len(drift) == 1
        assert drift[0]["model"] == "deepseek-chat"
        assert drift[0]["occurrences"] == 3

    def test_drift_sorted_worst_first(self, ledger):
        ledger.ingest([
            rec(prompt_tokens=1_000_000, completion_tokens=0, raw_cost_usd=0.5),
            rec(model="deepseek-reasoner", prompt_tokens=1_000_000, completion_tokens=0, raw_cost_usd=50.0),
        ])
        drift = insights.alerts(ledger)["drift"]
        assert [d["model"] for d in drift] == ["deepseek-reasoner", "deepseek-chat"]

    def test_small_drift_ignored(self, ledger):
        ledger.ingest([rec(prompt_tokens=1_000_000, completion_tokens=0, raw_cost_usd=0.28)])
        assert insights.alerts(ledger)["drift"] == []

    def test_failed_calls_grouped(self, ledger):
        ledger.ingest([rec(status="upstream_unreachable"), rec(status="upstream_unreachable"),
                       rec(status="http_429")])
        failed = {f["status"]: f["calls"] for f in insights.alerts(ledger)["failed_calls"]}
        assert failed == {"upstream_unreachable": 2, "http_429": 1}

    def test_recent_batches_included(self, ledger):
        ledger.ingest([rec()])
        ledger.ingest([rec()])
        assert len(insights.alerts(ledger)["recent_batches"]) == 2

    def test_batches_newest_first(self, ledger):
        ledger.ingest([rec()])
        ledger.ingest([rec(user_id="bob")])
        b = insights.alerts(ledger)["recent_batches"]
        assert b[0]["inserted"] == 1 and b[0]["source"] != b[1]["source"] or b[0]["batch_id"] != b[1]["batch_id"]


class TestBuildOverview:
    def test_shape(self, ledger):
        ledger.ingest([rec()])
        o = insights.build_overview(ledger, [BudgetRule(scope="global", window="day", limit_usd=1)])
        assert set(o) == {"totals", "series", "by_model", "by_user", "by_feature", "budget", "alerts"}

    def test_series_respects_days(self, ledger):
        o = insights.build_overview(ledger, [], days=5)
        assert len(o["series"]) == 5

    def test_json_serialisable(self, ledger):
        import json

        ledger.ingest([rec()])
        o = insights.build_overview(ledger, [BudgetRule(scope="global", window="day", limit_usd=1)])
        json.dumps(o)  # 不抛异常即可
