"""三层预算 + 熔断：尤其测 fail-closed。"""

import pytest

from llm_cost_ledger.budget import (
    TIER_ASK,
    TIER_OK,
    TIER_STOP,
    TIER_WARN,
    BudgetRule,
    decide,
    evaluate_rule,
    load_rules,
)


class TestRuleValidation:
    def test_bad_scope_rejected(self):
        with pytest.raises(ValueError):
            BudgetRule(scope="galaxy", window="day", limit_usd=1)

    def test_bad_window_rejected(self):
        with pytest.raises(ValueError):
            BudgetRule(scope="global", window="fortnight", limit_usd=1)

    def test_non_positive_limit_rejected(self):
        with pytest.raises(ValueError):
            BudgetRule(scope="global", window="day", limit_usd=0)

    def test_user_scope_requires_key(self):
        with pytest.raises(ValueError):
            BudgetRule(scope="user", window="day", limit_usd=1, key="")

    def test_ratios_must_be_ordered(self):
        with pytest.raises(ValueError):
            BudgetRule(scope="global", window="day", limit_usd=1, warn_ratio=0.9, ask_ratio=0.5)

    def test_valid_rule_ok(self):
        assert BudgetRule(scope="global", window="day", limit_usd=1).limit_usd == 1

    def test_load_rules_from_dicts(self):
        rules = load_rules([{"scope": "global", "window": "day", "limit_usd": 5}])
        assert len(rules) == 1 and rules[0].limit_usd == 5

    def test_load_rules_bad_config_raises(self):
        with pytest.raises(KeyError):
            load_rules([{"scope": "global"}])

    def test_load_rules_empty(self):
        assert load_rules(None) == []


class TestTierBoundaries:
    def test_zero_spend_is_ok(self):
        assert evaluate_rule(BudgetRule(scope="global", window="day", limit_usd=10), 0)[0] == TIER_OK

    def test_below_warn_ratio(self):
        assert evaluate_rule(BudgetRule(scope="global", window="day", limit_usd=10), 7.9)[0] == TIER_OK

    def test_at_warn_ratio(self):
        assert evaluate_rule(BudgetRule(scope="global", window="day", limit_usd=10), 8.0)[0] == TIER_WARN

    def test_between_warn_and_ask(self):
        assert evaluate_rule(BudgetRule(scope="global", window="day", limit_usd=10), 9.0)[0] == TIER_WARN

    def test_at_ask_ratio(self):
        assert evaluate_rule(BudgetRule(scope="global", window="day", limit_usd=10), 9.5)[0] == TIER_ASK

    def test_at_limit_is_stop(self):
        assert evaluate_rule(BudgetRule(scope="global", window="day", limit_usd=10), 10.0)[0] == TIER_STOP

    def test_over_limit_is_stop(self):
        assert evaluate_rule(BudgetRule(scope="global", window="day", limit_usd=10), 999)[0] == TIER_STOP

    def test_negative_spend_clamped(self):
        assert evaluate_rule(BudgetRule(scope="global", window="day", limit_usd=10), -5)[1] == 0.0


class TestDecide:
    def _lookup(self, value):
        return lambda rule, ctx: value

    def test_no_rules_allows(self):
        d = decide([], {}, self._lookup(100))
        assert d.allowed and d.tier == TIER_OK

    def test_global_rule_applies(self):
        rules = [BudgetRule(scope="global", window="day", limit_usd=10)]
        assert decide(rules, {"user_id": "anyone"}, self._lookup(1)).tier == TIER_OK

    def test_user_rule_only_matches_that_user(self):
        rules = [BudgetRule(scope="user", window="day", limit_usd=10, key="alice")]
        assert decide(rules, {"user_id": "alice"}, self._lookup(10)).tier == TIER_STOP
        assert decide(rules, {"user_id": "bob"}, self._lookup(10)).tier == TIER_OK

    def test_feature_rule_matching(self):
        rules = [BudgetRule(scope="feature", window="day", limit_usd=10, key="rag")]
        assert decide(rules, {"feature": "rag"}, self._lookup(10)).tier == TIER_STOP
        assert decide(rules, {"feature": "chat"}, self._lookup(10)).tier == TIER_OK

    def test_strictest_rule_wins(self):
        rules = [
            BudgetRule(scope="global", window="day", limit_usd=100),
            BudgetRule(scope="user", window="day", limit_usd=10, key="alice"),
        ]
        d = decide(rules, {"user_id": "alice"}, self._lookup(10))
        assert d.tier == TIER_STOP
        assert d.rule.scope == "user"

    def test_stop_blocks(self):
        rules = [BudgetRule(scope="global", window="day", limit_usd=1)]
        assert decide(rules, {}, self._lookup(1)).allowed is False

    def test_warn_allows_with_message(self):
        rules = [BudgetRule(scope="global", window="day", limit_usd=10)]
        d = decide(rules, {}, self._lookup(8.5))
        assert d.allowed is True
        assert "告警" in d.message

    def test_ask_allows_but_flags(self):
        rules = [BudgetRule(scope="global", window="day", limit_usd=10)]
        d = decide(rules, {}, self._lookup(9.7))
        assert d.allowed is True
        assert "需批准" in d.message

    def test_fail_closed_on_lookup_error(self):
        """读不到账目时必须拒绝 —— 这是本模块最重要的行为。"""
        def boom(rule, ctx):
            raise RuntimeError("database is locked")

        rules = [BudgetRule(scope="global", window="day", limit_usd=10)]
        d = decide(rules, {}, boom)
        assert d.allowed is False
        assert d.tier == TIER_STOP
        assert "database is locked" in d.message

    def test_message_is_chinese_and_actionable(self):
        rules = [BudgetRule(scope="user", window="day", limit_usd=1, key="alice")]
        d = decide(rules, {"user_id": "alice"}, self._lookup(2))
        assert "alice" in d.message and "超限" in d.message

    def test_decision_serialisable(self):
        rules = [BudgetRule(scope="global", window="day", limit_usd=1)]
        d = decide(rules, {}, self._lookup(1))
        assert d.as_dict()["tier"] == TIER_STOP
        assert d.as_dict()["rule"]["scope"] == "global"
