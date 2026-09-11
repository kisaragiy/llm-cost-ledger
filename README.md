# llm-cost-ledger

**LLM 成本账本 · 成本归因 + 预算熔断**

市面上的 LLM 成本工具大多在解决「花了多少」。这个项目解决的是另一个问题：**账目凭什么可信**。

一个 LLM 应用的账本要能用，必须同时做到四件事 —— 记录不重复、金额可核对、失败不消失、超支能止损。缺任何一件，数字就只是看起来像那个数。

---

## 一行接入

只换 `base_url`，代码其余部分不用动：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8790/v1",   # 指向本代理
    api_key="any",                          # 代理自身鉴权见 PROXY_AUTH_KEY
)

resp = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "你好"}],
    extra_headers={
        "X-Ledger-User": "alice",       # 归因维度，可省略
        "X-Ledger-Feature": "rag",      # 归因维度，可省略
    },
)
```

请求经代理转发到真实上游，响应原样返回。记录、计费、预算判定全部在旁路完成。

---

## 四个核心能力

### 1. 幂等 —— 同一批记录导入两次，账目纹丝不动

去重键由**记录内容**决定，与文件名、行号、路径无关。日志轮转、重复导出、定时任务跑重，都不会让同一个调用被计费两次。

```
========================================================================
场景：同一时间窗口被导出两次，且第二次行号整体位移 7 行
      真实调用数 = 25 次
========================================================================

✗ 按 (来源文件, 行号) 去重
    入库行数 : 50    ← 真相是 25
    虚高比例 : 100.0%

✓ 按内容指纹去重
    第 1 次导入 [export_2026-09-10_run1.jsonl]
        看到 25 条 / 入库 25 条 / 压制 0 条
        累计花费 = $0.122661
    第 2 次导入 [export_2026-09-10_run2.jsonl]（同一窗口，行号位移）
        看到 25 条 / 入库 0 条 / 压制 25 条
        累计花费 = $0.122661

    花费变化 : 0 条新增 -> $0.000000
    账本总行数: 25（真相 25）

✅ 幂等生效：重复导入未产生任何新花费
```

### 2. 对账 —— 五道检查，不过就非零退出

可以直接挂进 CI：

| 检查 | 内容 |
|:--|:--|
| **C1** 身份唯一性 | 同一 `(指纹, 序号)` 不得出现两行 —— 结构性地证明去重生效 |
| **C2** 逐行重算 | 用每行自己的计价明细重算总额，与账面 `cost_usd` 对平 |
| **C3** 未计价暴露 | 命中不了价格表的调用显式计数，不静默按 0 元入账 |
| **C4** 压制可追溯 | 每个导入批次「压掉多少条」必须留痕，防止去重去过头没人知道 |
| **C5** 价格漂移 | 与上游返回的原始花费比对，偏差超阈值报警（价格表该更新了） |

```
== 账目对账 ==
  调用数        : 25
  总花费        : $0.122661
  未计价调用    : 0
  导入批次      : 2（压掉 25 条）

  ✅ 五道检查全部通过
```

账目被篡改时的真实反应：

```
  ❌ [C2] 5 行的明细与本行 cost_usd 对不上
        重算总额: 0.122661
        账本总额: 0.34325352
$ echo $?
1
```

### 3. 预算熔断 —— 判定发生在上游调用之前

三层预算，三种不同后果：

| 层 | 行为 |
|:--|:--|
| `WARN` | 放行，加 `X-Ledger-Budget-Tier: warn` 响应头 |
| `ASK` | 放行但标记需批准 |
| `STOP` | **拒绝请求**，返回 `402` 与可读原因 |

判定是 **fail-closed** 的：读不到账目时按 STOP 处理，宁可拒绝一次请求，也不在一个已经失控的预算上继续烧钱。

```
$ curl -X POST http://127.0.0.1:8790/v1/chat/completions ...
{"error":{"type":"budget_exceeded",
          "message":"预算已超限：global=全局 total 已花 $0.9750 / 上限 $0.50",
          "ledger":{"tier":"stop","allowed":false,...}}}
HTTP_CODE=402
```

规则按维度组合，任一维度触顶即触顶：

```json
[{"scope": "global",  "window": "day", "limit_usd": 50},
 {"scope": "user",    "key": "alice", "window": "day", "limit_usd": 2},
 {"scope": "feature", "key": "rag",   "window": "day", "limit_usd": 5}]
```

`scope` 支持 `global` / `user` / `feature`，`window` 支持 `day` / `month` / `rolling_24h` / `total`。

### 4. 失败也记账

上游返回 4xx、连接失败、流式中断 —— 全部落账并带状态标记。失败的调用不会在账本里凭空消失。

```
{"error":{"type":"upstream_unreachable",
          "message":"上游不可达（http://127.0.0.1:1）：All connection attempts failed"}}
HTTP_CODE=503
```

---

## 部署

```bash
# 1. 环境
git clone https://github.com/kisaragiy/llm-cost-ledger.git
cd llm-cost-ledger
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"   # 只跑服务可去掉 [dev]

# 2. 配置
cp .env.example .env
#    至少填 UPSTREAM_BASE_URL 与 UPSTREAM_API_KEY

# 3. 起服务（代理 + 账本 API）
.venv/Scripts/python.exe -m llm_cost_ledger serve
#    -> http://127.0.0.1:8790
```

### 命令行

```bash
ledger serve                                # 启动代理 + API
ledger reconcile                            # 对账，不一致时退出码 1
ledger reconcile --json                     # 机读输出
ledger ingest calls.jsonl                   # 批量导入历史记录
ledger summary --since 2026-09-01           # 汇总
ledger spend --by feature                   # 按维度看花费
ledger pricing                              # 查看价格表
```

### 账本 API

| 端点 | 用途 |
|:--|:--|
| `POST /v1/chat/completions` | OpenAI 兼容代理（透明转发 + 记录 + 熔断） |
| `GET /health` | 健康检查（含累计调用数与花费） |
| `GET /v1/ledger/summary` | 汇总 + 按 model/user/feature 分组 |
| `GET /v1/ledger/spend?by=feature` | 单维度花费排行 |
| `POST /v1/ledger/ingest` | 批量导入调用记录 |
| `GET /v1/ledger/batches` | 导入批次审计 |
| `GET /v1/ledger/pricing` | 当前价格表 |

---

## 测试

```bash
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"   # pytest 在 dev extra 里
.venv/Scripts/python.exe -m pytest tests/ -q
# 171 passed in 4.52s
```

覆盖：身份键（含行号位移不变性）、计费口径（缓存/reasoning 不重复计价）、
幂等（重复导入 0 新增）、三层预算边界、fail-closed、用量提取（OpenAI / DeepSeek / Ollama 三种口径）、
SSE 流式捞 usage、代理端到端（转发/归因/熔断短路/上游故障）、对账 C1–C5。

---

## 明确不做

- 通用 LLM 网关（路由 / 负载均衡 / 多租户计费）
- 编辑器插件的本地用量看板
- prompt 管理与评估平台
- 深度 tracing（OpenTelemetry 生态已覆盖）

---

## 可证伪声明

以下每条都可以在本仓库里当场验证，对不上就是我在吹：

| 声明 | 验证方式 |
|:--|:--|
| 重复导入不产生新花费 | `python examples/rotation_demo.py`，观察第 2 次导入 `入库 0 条` |
| 对账能抓出账目异常 | 篡改任意一行 `cost_usd` 后跑 `ledger reconcile`，退出码应为 1 |
| 熔断发生在上游调用之前 | 把 `UPSTREAM_BASE_URL` 指向不可达地址并设超限预算，应得 `402` 而非连接错误 |
| 171 个测试通过 | `pytest tests/ -q` |
| 免费模型不会被误判为未计价 | `ledger pricing` 中 `qwen3.5:9b` 单价为 0，但 `unpriced` 计数不增加 |

---

MIT License
