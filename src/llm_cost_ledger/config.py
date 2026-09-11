"""配置。环境变量优先，其次 config.json，最后默认值。

不给上游 key 设魔法默认值 —— 缺了就在启动时报错，而不是运行时静默转发失败。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_DB = "ledger.db"
DEFAULT_PORT = 8790
DEFAULT_HOST = "127.0.0.1"


@dataclass
class Settings:
    upstream_base_url: str = ""
    upstream_api_key: str = ""
    upstream_timeout_s: float = 120.0
    ledger_db: str = DEFAULT_DB
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    proxy_auth_key: str = ""
    budget_rules: list[dict[str, Any]] = field(default_factory=list)
    default_user: str = "anonymous"
    default_feature: str = ""

    def require_upstream(self) -> None:
        if not self.upstream_base_url:
            raise RuntimeError(
                "缺少上游地址：请设置 UPSTREAM_BASE_URL（或 config.json 的 upstream_base_url）。\n"
                "例：export UPSTREAM_BASE_URL=https://api.deepseek.com"
            )

    def public(self) -> dict[str, Any]:
        """可安全外露的一份配置（不含任何密钥）。"""
        return {
            "upstream_base_url": self.upstream_base_url,
            "upstream_key_set": bool(self.upstream_api_key),
            "ledger_db": self.ledger_db,
            "proxy_auth": bool(self.proxy_auth_key),
            "budget_rules": self.budget_rules,
            "default_user": self.default_user,
        }


def _from_env() -> dict[str, Any]:
    out: dict[str, Any] = {}
    mapping = {
        "UPSTREAM_BASE_URL": "upstream_base_url",
        "UPSTREAM_API_KEY": "upstream_api_key",
        "UPSTREAM_TIMEOUT_S": "upstream_timeout_s",
        "LEDGER_DB": "ledger_db",
        "LEDGER_HOST": "host",
        "LEDGER_PORT": "port",
        "PROXY_AUTH_KEY": "proxy_auth_key",
        "LEDGER_DEFAULT_USER": "default_user",
        "LEDGER_DEFAULT_FEATURE": "default_feature",
    }
    for env_key, field_name in mapping.items():
        val = os.environ.get(env_key)
        if val not in (None, ""):
            out[field_name] = val
    raw_rules = os.environ.get("BUDGET_RULES")
    if raw_rules:
        out["budget_rules"] = json.loads(raw_rules)
    return out


def _coerce(data: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for key, caster in (("port", int), ("upstream_timeout_s", float)):
        if key in out:
            out[key] = caster(out[key])
    return out


def _load_dotenv() -> None:
    """读取项目根目录的 .env。缺失 dotenv 或文件时静默跳过 —— 不影响纯环境变量用法。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path.cwd() / ".env", override=False)


def load_settings(config_path: str | Path | None = None) -> Settings:
    _load_dotenv()
    data: dict[str, Any] = {}
    path = Path(config_path) if config_path else Path("config.json")
    if path.is_file():
        data.update(json.loads(path.read_text(encoding="utf-8")))
    data.update(_from_env())
    return Settings(**_coerce(data))
