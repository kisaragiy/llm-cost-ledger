import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_cost_ledger.store import Ledger  # noqa: E402


@pytest.fixture()
def ledger(tmp_path):
    led = Ledger(tmp_path / "test.db")
    yield led
    led.close()


def rec(**overrides):
    """构造一条标准调用记录。"""
    base = {
        "provider": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "endpoint": "/v1/chat/completions",
        "ts": "2026-09-11T10:00:00",
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "user_id": "alice",
        "feature": "chat",
        "session_id": "s1",
        "agent_run": "",
        "status": "ok",
    }
    base.update(overrides)
    return base
