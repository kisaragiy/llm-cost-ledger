"""逐条验证 README 的「可证伪声明」。

设计意图：README 里那 5 条声明不该只是文案 —— 让脚本和 CI 每次提交都跑一遍。
任何一条对不上，脚本非零退出，CI 变红。声明失真 = 构建失败。

跑法：.venv/Scripts/python.exe scripts/verify_claims.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_cost_ledger import pricing  # noqa: E402
from llm_cost_ledger.reconcile import run_reconcile  # noqa: E402
from llm_cost_ledger.store import Ledger  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

# 故意指向不可达地址 —— 用于验证「熔断发生在上游调用之前」
DEAD_UPSTREAM = "http://127.0.0.1:1"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'✅' if ok else '❌'} {name}")
    if detail:
        print(f"       {detail}")


def rmtree_with_retry(path: Path, attempts: int = 10, delay: float = 0.4) -> None:
    """Windows 上 SQLite 句柄释放有延迟 —— 直接删会 PermissionError，必须重试。"""
    import shutil

    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except (PermissionError, OSError):
            if i == attempts - 1:
                return  # 删不掉就放弃，不要让清理失败掩盖真正的验证结果
            time.sleep(delay)


def ensure_dead(proc: subprocess.Popen) -> None:
    """先把进程彻底收尸，再让调用方去删它占用的文件。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    time.sleep(0.5)  # 给 OS 一点时间回收文件句柄


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(url: str, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


# --------------------------------------------------------------------------
# 声明 1：重复导入不产生新花费
# --------------------------------------------------------------------------
def claim_reimport() -> None:
    """跑真实的 rotation_demo，读它的退出码 —— 不复制逻辑，跑同一个脚本。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "rotation_demo.py")],
        capture_output=True, text=True, cwd=str(ROOT), timeout=180,
    )
    out = proc.stdout
    ok = proc.returncode == 0 and "入库 0 条" in out and "幂等生效" in out
    detail = "第二次导入入库 0 条，花费未变" if ok else f"退出码 {proc.returncode}；输出尾部：{out[-300:]}"
    check("声明1 重复导入不产生新花费", ok, detail)
    # 清理 demo 产物
    rmtree_with_retry(ROOT / "examples" / "_demo")


# --------------------------------------------------------------------------
# 声明 2：对账能抓出账目异常（退出码 1）
# --------------------------------------------------------------------------
def claim_reconcile_detects() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="verify-reconcile-"))
    try:
        db = tmpdir / "ledger.db"
        ledger = Ledger(db)
        ledger.ingest([{"model": "deepseek-chat", "ts": "2026-09-11T00:00:00",
                        "prompt_tokens": 1_000_000, "completion_tokens": 0}])
        clean_ok = run_reconcile(ledger).ok

        # 篡改一行金额，对账必须发现
        ledger._conn().execute("UPDATE calls SET cost_usd = 999.0")  # noqa: SLF001
        ledger._conn().commit()  # noqa: SLF001
        dirty = run_reconcile(ledger)
        ledger.close()

        ok = clean_ok and (not dirty.ok) and any(f.code == "C2" for f in dirty.findings)
        check("声明2 对账能抓出账目异常", ok,
              "干净库通过、篡改后 C2 报错" if ok else f"clean_ok={clean_ok} dirty_ok={dirty.ok}")
    finally:
        rmtree_with_retry(tmpdir)


# --------------------------------------------------------------------------
# 声明 3：熔断发生在上游调用之前（毒化下游 + 对照组）
# --------------------------------------------------------------------------
def claim_fuse_before_upstream() -> None:
    port = free_port()
    tmpdir = Path(tempfile.mkdtemp(prefix="verify-claims-"))
    try:
        db = tmpdir / "ledger.db"
        # 预置超额花费，让 STOP 必然触发
        seed = Ledger(db)
        seed.ingest([{"model": "deepseek-chat", "ts": "2026-09-11T00:00:00",
                      "prompt_tokens": 1_000_000, "completion_tokens": 0}])
        seed.close()

        env = dict(os.environ)
        env.update({
            "LEDGER_DB": str(db),
            "UPSTREAM_BASE_URL": DEAD_UPSTREAM,
            "UPSTREAM_API_KEY": "dead",
            "LEDGER_HOST": "127.0.0.1",
            "LEDGER_PORT": str(port),
            "BUDGET_RULES": json.dumps([{"scope": "global", "window": "total", "limit_usd": 0.0001}]),
            "PROXY_AUTH_KEY": "",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        })
        proc = subprocess.Popen(
            [sys.executable, "-m", "llm_cost_ledger", "serve"],
            env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            if not wait_for(f"http://127.0.0.1:{port}/health"):
                check("声明3 熔断发生在上游调用之前", False, "服务未就绪")
                return
            status, body = post_chat(port)
            # 毒化下游 + 守卫开 -> 必须是守卫自己的错误(402)，不是连接错误
            ok = status == 402 and "budget_exceeded" in body
            detail = (f"上游 {DEAD_UPSTREAM} 不可达，仍得 402（短路在上游之前）"
                      if ok else f"得到 {status}：{body[:200]}")
            check("声明3 熔断发生在上游调用之前", ok, detail)
        finally:
            ensure_dead(proc)
    finally:
        rmtree_with_retry(tmpdir)


def post_chat(port: int) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# --------------------------------------------------------------------------
# 声明 4：测试通过（由 CI 单独跑 pytest，这里只做数量兜底断言）
# --------------------------------------------------------------------------
def claim_tests_exist() -> None:
    test_files = sorted((ROOT / "tests").glob("test_*.py"))
    ok = len(test_files) >= 5
    check("声明4 测试套件存在（CI 中另有 pytest 全量执行）", ok, f"{len(test_files)} 个测试文件")


# --------------------------------------------------------------------------
# 声明 5：免费模型不会被误判为未计价
# --------------------------------------------------------------------------
def claim_free_model_not_unpriced() -> None:
    quote = pricing.quote({"model": "qwen3.5:9b", "prompt_tokens": 10_000, "completion_tokens": 1_000})
    ok = quote.unpriced is False and quote.cost_usd == 0.0
    check("声明5 免费模型不被误判为未计价", ok,
          "qwen3.5:9b 单价 0 但 unpriced=False" if ok else f"unpriced={quote.unpriced} cost={quote.cost_usd}")


# --------------------------------------------------------------------------
# 声明 6：看板零外部网络依赖（断网可用）
# --------------------------------------------------------------------------
def claim_dashboard_offline() -> None:
    import re

    html_path = ROOT / "src" / "llm_cost_ledger" / "static" / "dashboard.html"
    if not html_path.is_file():
        check("声明6 看板零外部依赖", False, "找不到 dashboard.html")
        return
    html = html_path.read_text(encoding="utf-8")
    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)
    external += re.findall(r'@import\s+url\(["\']?(https?://[^)"\']+)', html)
    ok = not external
    check("声明6 看板零外部网络依赖", ok,
          "页面不引用任何外部主机，断网可打开" if ok else f"引用了外部资源：{external}")


def main() -> int:
    print("=" * 66)
    print("验证 README「可证伪声明」")
    print("=" * 66)
    claim_reimport()
    claim_reconcile_detects()
    claim_fuse_before_upstream()
    claim_tests_exist()
    claim_free_model_not_unpriced()
    claim_dashboard_offline()
    print("-" * 66)
    print(f"通过 {len(PASSED)} / 失败 {len(FAILED)}")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("✅ 全部可证伪声明成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
