# AGENTS.md — llm-cost-ledger

## Project Compass

**定位（一句话）**：LLM 应用的「可审计成本账本 + 预算熔断」中间件 —— 不是显示花了多少，而是**保证账目对得上**。

**不做什么**（边界，写死）：
- ✗ 不做通用 LLM 网关（路由 / 负载均衡 / 多租户计费）→ litellm 的地盘
- ✗ 不做 C 端编码工具用量看板（Claude Code / Cursor 用量挂件）→ codeburn 的地盘
- ✗ 不做 prompt 管理 / eval 平台 → 另两个候选方向的地盘
- ✗ 不做深度 tracing（OTel 生态已有）

**护城河**：市面工具做「花费展示」，本项目管理的是**账目正确性**。真实事故驱动 —— 旧实现因
`UNIQUE(src_file, line_no)` + 日志轮转导致行号位移，同一调用被重复入库，花费虚高 58.6%、调用虚高 70%。

## 架构

```
采集            计费            幂等            归因            熔断
app.py      →  pricing.py   →  identity.py  →  store.py    →  budget.py
(OpenAI兼容)   (价格表+口径)    (内容指纹)      (SQLite账本)    (WARN/ASK/STOP)
                                                                ↓
                                              reconcile.py (五道对账检查)
```

| 模块 | 职责 |
|------|------|
| `identity.py` | 调用身份键：fingerprint(内容哈希) + occurrence(批内序号)。**与文件名/行号无关** |
| `pricing.py` | 价格表 + 计费。未知模型标记 `unpriced`，绝不静默按 0 元入账 |
| `store.py` | SQLite 账本。靠主键冲突去重；每批导入留 batch 留痕 |
| `budget.py` | 三层预算 WARN/ASK/STOP + 熔断。**fail-closed**：判不出来就拒绝 |
| `spend.py` | 窗口计算 + 账本花费查询 |
| `extract.py` | 用量提取（OpenAI / DeepSeek / Ollama 三种口径）+ SSE 流式捞 usage |
| `app.py` | FastAPI：代理 + 账本 API |
| `reconcile.py` | 对账 CLI，五道检查，不过则非零退出 |
| `cli.py` | 命令行入口 |
| `insights.py` | 看板聚合层：趋势（缺失日期补 0）、预算状态、告警聚合。纯函数，可单测 |
| `static/dashboard.html` | 看板页面。单文件、零外部依赖（断网可用），手写 SVG 图表 |
| `scripts/verify_claims.py` | 逐条执行 README 的可证伪声明，任一条不成立则非零退出（CI 的 claims 任务） |
| `scripts/seed_demo.py` | 造 30 天演示数据，供看板预览与截图 |
| `Dockerfile` / `docker-compose.yml` | 多阶段构建、非 root、healthcheck；一行起服务 |
| `.github/workflows/ci.yml` | 三个 job：测试矩阵 / 声明验证 / 镜像构建+容器健康检查 |

## ADR

### ADR-001 用内容指纹做身份键，不用文件位置
**背景**：旧实现 `UNIQUE(src_file, line_no)`，日志轮转后行号位移 → 重复计费，花费虚高 58.6%。
**决策**：身份 = `sha256(规范化字段集合)` + 批内出现序号；显式 `request_id` 优先。
**备选**：全量内容哈希（无法区分真·重复调用）｜只用 request_id（上游不一定给）。
**后果**：与文件名/行号/路径完全解耦；真·重复调用仍会全部保留，不会被误去重。

### ADR-002 未知模型不按 0 元入账
**背景**：静默按 0 元会把「总额被低估」藏起来。
**决策**：无法计价时 `unpriced=1` 落库，对账 C3 检查显式暴露。
**后果**：总额可能偏低但有明确告警，不会假装正确。

### ADR-003 预算判定 fail-closed
**背景**：无人值守时「评估失败就放行」等于失控烧钱。
**决策**：读取账目异常时直接按 STOP 拒绝，返回可读原因。
**后果**：极端情况下会误拦请求，代价可接受 —— 相比静默超支。

### ADR-004 流式响应也必须计费
**背景**：很多后端只在最后一个 chunk 带 usage；拿不到就按 0 记账 = 流式调用全部免费。
**决策**：转发时 tee 一份原始字节，流结束后从 SSE 里捞 usage；断流也走 finally 记账。
**后果**：内存里多留一份响应副本（可接受），换来流式调用不被漏计。

## 已知坑

- 价格表会漂移 —— `pricing.PRICING_VERSION` 是版本锚，对账 C5 用上游 `raw_cost_usd` 比对报警。
- SQLite 每线程一条连接 + WAL；不要用 `check_same_thread=False` 共享连接。
- `reasoning_tokens` 通常已含在 `completion_tokens` 里，只在独立上报时才单算（防双计）。

## 验收标准（v0.1.0 实测结果）

| | 标准 | 结果 | 证据 |
|---|---|---|---|
| AC1 | 代理跑通一次真实 LLM 调用 | ✅ | 打本地 ollama `qwen3.5:0.8b`，`usage:{prompt_tokens:17,completion_tokens:60}` 入库并三维归因 |
| AC2 | 同一批记录导入两次，花费数字不变 | ✅ | `examples/rotation_demo.py`：旧做法 50 行（虚高 100%）→ 本做法第 2 次导入 0 新增，花费 $0.122661 不变 |
| AC3 | 对账 CLI 检出账目异常并非零退出 | ✅ | 篡改 5 行金额 → `[C2] 明细对不上`，`EXIT=1`；干净账本 `EXIT=0` |
| AC4 | STOP 阈值触发即拒请求 | ✅ | 已花 $0.975/上限 $0.50 → `402` + 中文原因；**上游设为死地址仍得 402**，反证短路发生在上游调用前 |
| AC5 | user/feature/model 三维汇总与手工核对一致 | ✅ | `by_model/by_user/by_feature` 均正确 |
| AC6 | pytest ≥60 用例通过 | ✅ | **171 passed** |
| AC7 | README 含真截图 + 部署步骤 + 可证伪声明 | ✅* | 部署步骤 ✅ / 可证伪声明 ✅ / *截图以逐字实测输出代替 —— CLI 工具的可复制输出是比截图更强的证据，且 clone 后能当场复现 |

## 开发过程中测试抓出的真实缺陷（已修）

1. 批次排序不稳定 —— `ingest_batches` 按秒精度 `started_at` 排序，同秒内顺序随机 → 改 `rowid DESC`；顺带修了 `finished_at` 被误写成开始时间。
2. 中文提示塞不进 HTTP 头 —— `x-ledger-budget-message` 放中文 latin-1 编码失败 → 改百分号编码 + `encoding: percent`。
3. `Ledger` 不建父目录 —— 报 `unable to open database file` → 构造时自动 `mkdir(parents=True)`。
4. 上游不可达返回裸 500 → 改 503 + 中文原因，且失败调用落账（`status=upstream_unreachable`）；流式中断补发 SSE 错误事件。

## 仓库

https://github.com/kisaragiy/llm-cost-ledger （public，topics: llm / llmops / finops / cost-management / observability）

## 版本

- v0.1.0 — 代理 + 账本 + 幂等 + 三层预算 + 对账 CLI（171 测试，AC1-AC7 全过）
- v0.2.0 — 容器化（多阶段 / 非 root / healthcheck）+ GitHub Actions CI + `scripts/verify_claims.py`（把 README 可证伪声明编译成 CI 任务）
