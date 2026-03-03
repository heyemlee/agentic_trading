# P0 核查结果与第 9 节接口契约（后端交接稿，第一轮）

日期：2026-09-18。状态：第二稿。第一稿经前端（ChatGPT/Codex）审阅，本稿纳入其六项答复（第 4 节）与六项修正
（账户隔离、覆盖键、兼容字段、权限语义、事实时间、待办退出），第 2、3、5 节据此改写。对应 `docs/UPGRADE_PLAN_2026-09-18.md`。

本稿只新增文档。未修改前端、`config/`、交易权限、部署或任何 `state/` 内容。文中"当前"指
`HEAD f4e25bf` 的工作树与 `config/` 的读数；"契约"指第 2 节的接口设计，后端下一轮实施，
实施前不生效。

---

## 0. 怎么读这份稿

- 第 1 节是 P0 核查：能力清单、部署版本、有效权限矩阵、重复功能、账户与评估范围。每项标
  "可调用／仅界面／不可用／未核实"，并给出代码位置。
- 第 2 节是接口契约：账户范围、有效权限、当前待办、研究任务与结果、评估。每个接口给字段
  说明和四种响应样例：正常、空数据、过期、失败。
- 第 3 节是兼容与迁移规则，第 4 节是请前端回答的问题，第 5 节是后端实施顺序。
- 所有新字段都会进 `agentic_trading/service/shapes.py`，`extra="forbid"`，FastAPI 按模型校验；
  `/openapi.json` 是最终契约，本稿与它不一致时以它为准。样例里的金额、账户尾号、时间戳都是
  示意值，不是账户读数。

约定（沿用现有 API）：

| 约定 | 内容 |
| --- | --- |
| 时间 | ISO 8601 带时区偏移，服务端 UTC。`checked_at` 是读取时刻，`as_of` / `priced_for` / `trade_date` 是数据所属的交易日 |
| 金额与比例 | `_usd` 后缀是美元，`_pct` 后缀是百分数（`3.2` 表示 3.2%）。缺失用 `null`，不用 `0` |
| 枚举 | 闭集。前端只按 `service/status.py` 的枚举分支；文本字段（`title`、`detail`、`reason`）按原文渲染 |
| 错误 | 统一 `{"detail": "..."}`：400 产品层拒绝（`ServiceError`），409 并发或状态冲突（`ServiceConflict`），401 未登录，403 CSRF 或来源不对，500 内部错误，503 未配置凭据 |
| 写请求 | 同源、`X-CT-Request: 1`、`Content-Type: application/json`、`X-CSRF-Token`。幂等写带 UUID 请求 id，重放返回原答复 |
| 新鲜度 | 由 `checked_at` 相对响应 `Date` 头的年龄判断，不由轮询是否成功判断（`docs/FRONTEND_HANDOFF.md` 第 15 节） |

---

## 1. P0 核查结果

### 1.1 部署版本核对

| 项目 | 读数 | 结论 |
| --- | --- | --- |
| 工作树 `HEAD` | `f4e25bf`（2026-09-18，"advisor: the prompt calls the paper buckets Demo"） | |
| 运行中的 release | `releases/current -> 20260918T202756Z-f4e25bf`，`RELEASE.json.sha = f4e25bf`，发布于 2026-09-18T20:27:58Z | 代码与 release 一致；本稿描述的代码就是在运行的代码 |
| 工作树差异 | 仅未跟踪文件 `docs/UPGRADE_PLAN_2026-09-18.md`（本稿写入后再多一个） | 无未提交代码 |
| `state` / `runs` / `.env` | 三个符号链接完好，指向 `~/Library/Application Support/agentic-trading/data/`；`git ls-tree HEAD -- state runs .env` 为空 | 符合 CLAUDE.md 要求 |
| launchd 四个作业 | `api`（`run_api.sh`）、`research-weekly`（`run_scheduled_research.sh` → `ct-trigger scheduled --config-dir …`，**不传 `--bucket`，默认 `core`**）、`backup-daily`、`maintenance-daily`（`ct-ops maintain`：轮转日志、清理 runs、评估快照、账本确认） | 计划第 2 节"部署脚本未传 bucket"属实（`deploy/macos/run_scheduled_research.sh`）|
| `deploy/checks.sh`（本稿写作前运行） | Ruff 通过；Python 787 项通过（77.8 秒）；前端 92 项通过；秘密扫描通过 | 计划第 10 节提到的偶发失败本次未复现；根因见 1.5 |

### 1.2 实际能力清单

标记含义：**可调用** = 代码路径存在且运行期使用；**仅界面** = 只在券商 App 里能用；**不可用** =
没有可供后台调用的接口；**未核实** = 本轮未连线或未读取生产状态，需要实测。

| 能力 | 标记 | 依据 |
| --- | --- | --- |
| Robinhood 账户、组合、持仓、报价、订单、下单、撤单、预审、可交易性 | 可调用 | `broker/robinhood.py` 连接时按 `robinhood.tool_candidates` 绑定，缺 `required_capabilities` 直接失败；`agentic_account()` 只认 `agentic_allowed: true` 且恰好一个 |
| 上述工具在当前连接的实际名称、schema、限流 | 未核实 | 运行期 `tools/list` 决定，本轮未开会话。契约不猜工具名 |
| Robinhood 自选、筛选器（`get_watchlists` / `get_watchlist_items` / `get_scans` / `run_scan`） | 可调用（有限） | `advisor.py` `READ_TOOLS`；不可用时写入 `snapshot.errors`；只读前 20 个集合、每个集合第一页、候选上限 200 |
| Gold Cortex／Digests | 不可用 | 无后台接口；计划第 3 节 |
| Robinhood 价格提醒回调 | 不可用 | 无 webhook；不作为触发源 |
| Claude Code 订阅运行（`claude -p`） | 可调用 | `llm/claude_code.py`；PF11 每次预检验证运行时无交易工具、`--strict-mcp-config`、内置工具关闭 |
| TradingAgents 研究图 | 可调用 | `graph.py`，pinned commit `9dee508c…`（`agentic_trading/__init__.py`） |
| Anthropic 金融技能（thesis-tracker 等） | 未接入 | P1 试验；本轮不设计其接口，仅在研究记录里预留 `route` 字段 |
| OpenAI 金融产品／模型 | 未接入 | 计划第 3 节 |
| yfinance、Polymarket、web fetch 白名单 | 可调用 | `investor_profile.declared_data_sources`；fetch 白名单在 `risk_profile.research_web_access.fetch` |
| FRED 宏观数据 | 可调用（需 key） | 未配置时 `/health.warnings` 提示，降置信并偏向 NO_TRADE |
| CLI 自带 WebSearch | 不可用（关闭） | `research_web_access.search.enabled: false` |
| 交易日历 | 可调用 | `exchange_calendars` XNYS；PF12 |
| 评估快照（每日） | 可调用 | `ct-ops maintain` → `Service.evaluation_snapshot()`，读 **Demo（conceptual）** 净值 |
| 账本确认（每日） | 可调用 | `ct-ops maintain` → `confirm_ledgers`，报告存 `ledger_confirm_report` |
| Web push 通知 | 可调用 | `notifications.channel: webpush`；投递去重在 `notify.py` |
| 自主减仓／开仓循环 | 可调用但关闭 | `service/approvals.py::autonomous_reductions` 每 15 秒跑一次，`autonomy.is_armed` 为假时立即返回（见 1.3） |
| 持久研究队列 | 可调用 | `research_jobs.py`，SQLite，去重键 `trade_date:ticker:bucket:instrument_type[:task_scope]` |

### 1.3 有效权限矩阵

以下是 `config/` 当前文本的读数，未改。三处"是否需要人"的判断来自三段不同配置，由三段
不同代码读取，这是契约 2.2 要统一的原因。

**谁读什么**

| 判断 | 读取的配置 | 代码 |
| --- | --- | --- |
| 预检 PF04"开仓需人工" | `micro_live_execution.execution_mode == human_approval` 且 `allow_autonomous_opening == false` | `micro_live.py::run_preflight` |
| 自主批准 | `buckets.micro_live.autonomy.open.*` 与 `.reduce.*`，加 `state/autonomy.json` 的撤回标志 | `autonomy.permits` / `is_armed` |
| 提案是否等人 | Hard Risk 结果 `requires_human_approval`（默认 `True`，只有 `engine.py:637` 一处置 `False`） | `risk/engine.py` |
| 阶段允许的模式 | `execution_policy.stage` 与 `stage_allowed_modes` | `profile.py` 装载时校验 |
| `execution_policy.autonomy.{defensive,conservative,experimental}` | **没有代码读取**（只在 `config_changes.py` 的松紧分类规则里出现） | 计划里"三种自主能力"是文档，不是运行期开关 |

**当前值（`config/risk_profile.yaml`、`config/investor_profile.yaml`）**

| 层 | 读数 | 对 BUY／ADD／REDUCE／SELL 的含义 |
| --- | --- | --- |
| `execution_policy.stage` | 3 → 允许 `recommend_only`、`human_approval`；`autonomous` 要 stage 6 | 任何桶都不能配置为 `autonomous` 模式 |
| `execution_policy.buckets.*.execution_mode` | core、sandbox = `recommend_only`；micro_live = `human_approval` | 只有 micro_live 能生成可批准提案 |
| `micro_live_execution.enabled` | **true**（真钱开关，2026-09-08 起） | 真钱订单可经人工批准下达 |
| `micro_live_execution.execution_mode` / `allow_autonomous_opening` | `human_approval` / `false` | PF04 通过 |
| `buckets.micro_live.autonomy.open` | `enabled: false`；`classes: [broad_diversified_etf, bond_or_cash_like_etf]`；`max_pct_of_account: 20`；`max_new_positions_per_day: 1`；**没有 `min_confidence`** | BUY／ADD 不自主。即便打开 `enabled`，`permits()` 也会以"declares no min_confidence"拒绝 |
| `buckets.micro_live.autonomy.reduce` | `enabled: false`；`min_confidence: 80`；`max_positions_reduced_per_day: 1` | REDUCE 不自主；打开后最多减半仓、每日一只 |
| SELL | `autonomy.permits` 对 SELL 一律拒绝；人工可经 `POST /proposals/{id}/approve` 以 `action: SELL` 批准；数量 = 账本全部持仓（HR14／HR14b，`sell_quantity` 为空时按 `position.quantity × 1.0`） | 完整清仓只能人工；没有独立的 SELL 授权对象 |
| `state/autonomy.json` 撤回标志 | 未核实（生产状态；`/config.autonomy.withdrawn` 可读） | 若为 tripped，所有自主动作停，需 `/autonomy/resume` 加密码 |
| 熔断 `state/HALT` | 未核实（`/health.paused` 可读） | 有则不下任何订单，研究继续 |
| 资金上限 | `max_capital_usd: 2000`，`capital_tolerance_usd: 50`，`refuse_if_account_equity_above_usd: 2000` | PF02 和 broker 适配器各查一次 |
| 允许的品种 | `permissions`: 只有 `equities_long`、`fractional` 为 true | PF09 |
| 置信度地板 | micro_live：半仓 ETF 70、全仓 ETF 80、行业 ETF 80、个股 85；`autonomous_any` 80；`no_trade_below` 50 | `policy.py` 决定动作，HR05 复核 |
| 仓位与集中度 | 单仓 40%（默认值已接受）、最多 3 仓；按类上限 40／40／25／10；个股合计 25%、单行业 25%、每暴露组 1 只 | HR08、HR11 系列 |
| 风险层级 | low 60%／单笔 $200；mid 25%／$80；high 15%／$50 | HR21、HR21b |
| 频率 | 每日新开 1、每周新开 2、每周订单 4、禁止当日往返（默认值已接受） | HR12 |
| 日亏损闸 | `-5%`（HR22，已执行）；`per_position_*` 两项**未被任何代码读取** | 计划第 5.3 节"止损"不能承诺 |
| 事件黑窗 | 宏观（`event_calendar.yaml`，只限自主开仓，维护到 2026-12-10）；财报（每笔买入） | HR19、HR18；退出不受黑窗 |
| 提案新鲜度 | micro_live 用 `core_conservative`：TTL 2880 分钟，960 分钟后需重验，价差 3% | `proposal.freshness` |
| 永久人工 | `strategy_policy.sandbox.strategies_requiring_human_approval_always: [momentum, short, event_driven]`；`approval.required_always_for_strategies` | PF09b |
| 放宽变更 | `GUARDED` 路径（`micro_live_execution`、`buckets`、`execution_policy`、`permissions`、…）放宽需密码并等 24 小时，可撤回 | `config_changes.py` |
| 未确认默认值 | `accept_all_recommended_defaults: true`（2026-09-15） | PF13 通过；改回 false 即阻断真钱 |

**结论**：当前没有任何动作可以自主执行；四种动作在 micro_live 都是"人工批准"。要让 BUY／ADD 自主，
需要同时改 `autonomy.open.enabled`、补 `min_confidence`，并且 PF04 不读这两项，所以会出现
"预检通过、自主开仓也通过"的组合，这正是计划第 2 节要求交叉测试的点。契约 2.2 把这三段读数
合成一个对象，并把不一致列在 `consistency` 里，让设置页、预检、风控和自动批准看同一份结果。

### 1.4 重复功能清单

| 现状 | 位置 | 处理建议 |
| --- | --- | --- |
| 首页"需要你"由前端从 `/proposals`、`/research/jobs`、`/micro-live.blocking` 自行推导；通知采集另有一套服务端规则 | `ui/assets/pages/index.js:112-202` 与 `service/notifications.py::collect_notifications` | 待办移到服务端 `GET /attention`（2.3），与通知共用同一采集函数；前端不再推导 |
| Assistant 页的"最近持仓结论"列表与 Portfolio 持仓摘要重复 | `ui/assets/pages/advisor.js:126-150` | 计划 8.4 决定删除；聊天引用原研究 |
| 个人资料在 Assistant 页内编辑器与 `profile.html` 各一份 | `advisor.html`、`profile.html` | 保留 `profile.html`，Assistant 只显示摘要 |
| Research 通用启动表单（ticker + bucket + trigger）与 Portfolio／Assistant 入口并存 | `ui/research.html` 表单，`f-bucket` 默认 **micro_live** | 计划 8.5：替代入口和旧链接迁移完成后再移除表单；后台 `POST /research` 保留 |
| `/orders.tracked_orders` 与 Robinhood 订单列表 | `service/books.py::orders` | **不是重复**：本侧记录提案来源、对账结果与分歧，保留 |
| `/evaluation.decisions` 与 `/research` 历史 | `evaluation.py::_decisions` 读 `research_history` | 同源计数，不是第二套账本；只需按范围过滤（2.5） |
| 通知投递记录与待办 | `notify.pending` vs 契约 `/attention` | 分开：投递记录回答"发过没有"，待办回答"还没解决的" |
| 成交推送、券商开放订单读取、advisor 快照里的 Agentic 账户 | 已于 2026-09-16 移除（`ac0a406..c1afa42`） | 不再引入 |

### 1.5 账户与评估范围核查

**账户标识（现状）**

| 范围 | 标识 | 来源 |
| --- | --- | --- |
| Agentic（真钱） | 桶名 `micro_live`；账户键 `micro_live`（`Portfolio.accounts`） | `books.py::ACCOUNTS`，账本 `state/micro_live_portfolio.yaml`，venue robinhood |
| Demo | 桶 `core`、`sandbox`；账户键 `conceptual` | 同上，本地账本；控制台 `SHOW_CONCEPTUAL = false` 已隐藏 |
| 个人（只读） | `sha256(account_number)[:20]`，20 位十六进制；标签 `••••` + 尾号 4 位 | `advisor.py::fetch_snapshot`，排除 `agentic_allowed` 账户 |
| 个人研究任务 | 桶名 `advisory`；`task.account_id` 可为 null（未绑定具体账户） | `service/assistant.py::start_advisor`；`research_worker` 把 `advisory` 路由到 `advisor.run_task`，永远不进提案库 |

**评估范围（现状，与计划第 2 节一致）**

- `Service.evaluation_snapshot()` 与 `ct-evaluate` 都读 `portfolio.accounts["conceptual"]`（Demo）的净值和净投入。
- `evaluation._decisions` 只排除 `bucket == "advisory"`，core、sandbox、micro_live 的研究与提案**混在一起计数**；
  `_execution` 读 `orders(limit=200).fills` **不按桶过滤**。
- 因此今天的 `GET /evaluation` = Demo 净值曲线 + 三桶混合决策计数 + 全部成交。不能标为 Agentic 成绩。
- 是否有 study 正在运行：**未核实**。本会话沙箱拒绝打开生产状态库（`chmod` 于
  `data/state/trading_state.sqlite3` 被拒），且 P0 规则本就不读生产状态。核实命令：
  `~/Library/Application\ Support/agentic-trading/releases/current/.venv/bin/ct-evaluate status --config-dir config`。
- 若没有 study，`GET /evaluation` 目前抛 `EvaluationError`（`RuntimeError` 子类），`_ok` 不认识它，
  **返回 500**，不是结构化空态。契约 2.5 改为 200 + `state: NOT_STARTED`。

**个人深链与字段口径（计划第 2、8 节，已定位）**

| 问题 | 位置 | 说明 |
| --- | --- | --- |
| Agentic 持仓字段不一致 | `ui/assets/pages/portfolio.js:231` 读 `pos.market_value`；API 是 `market_value_usd`（同文件 118 行已用对） | 桶卡片里市值显示为空 |
| 个人研究入口丢账户 | `ui/assets/pages/advisor.js:148` 把个人决策链接到 `research.html?ticker=…`；`research.js:291` 只回填 ticker，表单默认 `micro_live` | 个人语境进入 Agentic 研究表单 |
| Agentic 持仓入口只带 ticker | `portfolio.js:119` → `research.html?ticker=` | 需带 `scope=agentic` |
| 固定 50 分线 | `ui/assets/pages/research.js:17` `FLOOR = 50` | 计划 8.5：移除；阈值来自任务与授权记录 |
| Assistant 上下文未按账户过滤 | `advisor.py::run_task` 把 `advisor_decisions(limit=40)`（全部记录）和 `risk_state()`（系统风险）塞进个人聊天 | 契约 2.1 由服务端按范围筛选 |
| 会话不绑定账户 | `start_advisor` 只校验 `account_id` 正则，不校验存在；`conversation_id` 不记录范围 | 契约 2.1 |

**状态口径**

- `/health.status = READY` 表示能研究、能接受审批，**不表示**自主交易开启（自主由 1.3 决定）。
- `/system/operations.activities[reconciliation|ledger_confirm].last_success_at` 是循环跑过的心跳；
  "对账成功"要看 `/portfolio/ledgers.confirmed` 与 `.date`，"订单核对"要看 `/orders.tracked_orders[].reconciled_at`。
- 自主提案排队、休市、用户暂停不是故障：`autonomous_reductions` 在休市时 `skipped: "the market is closed"`。

**测试隔离问题（计划第 10 节保留的排查项）**

根因已定位，未改：`service/assistant.py` 以 `from .base import _last_session` 按名绑定，
`start_advisor` 每次都调用它；`_last_session()` → `_session_has_data()` → 上游 `load_ohlcv("SPY", …)`。
上游缓存目录是 `~/.tradingagents/cache`（`_TRADINGAGENTS_HOME`，项目未覆盖），**与生产共用**。
`tests/test_advisor.py::assistant_service` 夹具只桩掉了 `dispatch`，没有桩 `_last_session`；
`test_api.py` 里的桩打在 `agentic_trading.service.base._last_session`，够不到 `assistant.py` 里的名字。
于是：缓存里已有当日 SPY 日线时测试离线通过；新交易日收盘后、生产 API 还没把缓存填上时，
探测走 yfinance（curl_cffi 原生请求），`no_network_guard` 在 teardown 报错。这解释了"失败一次、
单独重跑通过"。修法是在夹具里桩 `agentic_trading.service.assistant._last_session`（或 `_session_has_data`），
不放宽 guard。修复已提交为 `6d9f39e`（分支 `claude/amazing-moser-c49a21`，`offline_market_reads` 夹具，只改 4 个测试文件），
冷缓存全套 787 项与 `deploy/checks.sh` 均通过；它是 `main` 的直接后继，可 `--ff-only` 合入。`bb51432` 是 2026-09-16 加入
网络 guard 的旧提交，与本修复无关。

---

## 2. 接口契约

变更总表（新增／扩展／不变）：

| 接口 | 类型 | 服务函数 | 响应模型 | 主要使用页 |
| --- | --- | --- | --- | --- |
| `GET /accounts` | 新增 | `books.account_scopes()` | `AccountScopes` | Portfolio、Assistant、Research 的账户切换与深链校验 |
| `GET /advisor?scope=&account_id=` | 扩展（加查询参数与字段，旧字段保留） | `assistant.advisor_state(scope, account_id)` | `AdvisorState` | Assistant |
| `POST /advisor/jobs` | 扩展（`scope`、`portfolio_review`） | `assistant.start_advisor` | 现有 dict | Assistant |
| `GET /advisor/conversations/{id}` | 扩展（返回绑定范围） | `assistant.advisor_conversation` | `AdvisorConversation` | Assistant |
| `GET /config/authority` | 新增 | `configuration.effective_authority()` | `Authority` | Home、More、Risk、Settings |
| `GET /config` | 扩展（新增 `authority`，`autonomy` 保留一轮） | `configuration.config_overview` | 现有 dict | Settings |
| `GET /attention` | 新增 | `notifications.attention()` | `Attention` | Home、More 徽标 |
| `POST /research` | 扩展（`scope`、`request_id`、`purpose`） | `research.start_research` | 现有 dict | Assistant 操作卡、Portfolio |
| `GET /research?scope=&account_id=&task_type=` | 扩展（过滤与字段） | `research.research_history` | `list[ResearchRecord]` | Research、Portfolio 摘要 |
| `GET /research/jobs?scope=` | 扩展 | `research.research_jobs` | `list[ResearchJob]` | Home 当前任务、Research |
| `GET /research/{run_key}` | 扩展（结果附范围与路线；只读，不写 `opened_at`） | `research.research_run` | 现有 dict | Research、Lab |
| `POST /research/{run_key}/opened` | 新增（幂等：结果已展示后调用，写 `opened_at`，重复调用原样返回；未完成的任务 409） | `research.mark_opened` | 现有 job dict | Assistant、Research |
| `GET /proposals/{id}` | 扩展（`authority_version`、`authority_changed`） | `approvals.proposal` | `ProposalDetail` | Approvals |
| `POST /proposals/{id}/approve` | 扩展（`authority_version`、`acknowledge_authority_change`） | `approvals.approve` | 现有 dict | Approvals |
| `GET /evaluation?scope=` | 扩展 | `system.evaluation_report(scope)` | `Evaluation` | Evaluation |
| `/orders`、`/activity`、`/micro-live`、`/health`、`/portfolio` | 不变（`/activity.detail` 加 `scope`、`account_id`） | | | |

### 2.0 前端答复带来的通用规则

- **枚举本地化，原文保留。** `kind`、`blocked_by[].code`、`actions[]`、`recommendation`、`account_scope_kind` 都是闭集，
  界面标签进 `ui/assets/i18n.js`；`title`、`detail`、`reason`、`rationale` 按 API 原文显示，不按错误字符串猜译。
  枚举新增值必须先改本稿与 `shapes.py`，再改 `i18n.js`。
- **站内链接用 `{page, params}`。** `page` 取自 `_PRIMARY_PAGES`（`api/app.py`），`params` 键只允许下表；
  前端据此生成 `href` 并做白名单校验，后端不再返回 `/ui/...` 路径（通知里的 `link` 一轮后同样迁移）。

| `page` | 允许的 `params` 键 | 说明 |
| --- | --- | --- |
| `index` | `scope` | |
| `portfolio` | `scope`、`account`、`ticker` | `ticker` 只定位持仓，不启动研究 |
| `advisor` | `scope`、`account`、`conversation`、`ticker` | `conversation` 必须属于该 `scope`/`account`，否则前端忽略并新建 |
| `research` | `scope`、`account`、`run`、`ticker`、`task_type` | `run` 深链不要求 `scope`；`ticker` 单独出现时只预选，不启动 |
| `lab` | `run` | |
| `approvals` | `proposal` | |
| `activity` | `subject`、`kind`、`cursor` | |
| `settings` | `change`、`section` | `change` 定位到冷静期变更并提供撤回 |
| `risk`、`evaluation` | `scope` | |
| `system`、`profile`、`notifications`、`more` | 无 | |

- **不为首页轮询新增券商调用。** `GET /attention` 与 `GET /config/authority` 只读本地状态和 kept reading，不开会话；
  Home 每 30 秒各轮询一次，与现有 `/health` 同频。完整权限对象约 3 KB，不再提供轻量视图。

### 2.1 账户范围

**词汇**

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `scope` | `agentic` \| `personal` \| `demo` | 资金、建议、对话、执行权限的归属。`demo` 只为历史记录和隐藏的 Demo 账户存在 |
| `account_scope_kind` | `single` \| `all_personal` \| `unknown` | 记录与任务上的范围种类。`single` 绑定一个账户；`all_personal` 明确表示"当时快照里的全部个人账户"；`unknown` 只用于旧记录，表示范围未记录。`agentic`／`demo` 恒为 `single` |
| `account_id` | `agentic` → `"micro_live"`；`demo` → `"conceptual"`；`personal` + `single` → 20 位十六进制；`all_personal`／`unknown` → `null` | 服务端校验：`single` 的个人 id 必须在最近一次快照的 `accounts` 里，否则 400 |
| `account_ids` | `all_personal` 时为当时快照里的成员 id 列表；其他为 `[]` | 用来判断"这条全账户复查是否覆盖了账户 A" |
| `account_label` | 显示名 | `agentic` = `"Agentic"`；`single` 个人 = `"••••1234"`；`all_personal` = `"all personal accounts"`；`unknown` = `"unspecified"`。前端可按 `account_scope_kind` 本地化后两者 |

`scope`、`account_scope_kind`、`account_id`、`account_ids` 四个字段成组出现在：`/advisor` 的 job、decision、conversation；
`/research` 的记录与 job；`/attention` 的每一项；`/activity` 的 `detail`；`decision_record.json` 顶层。

**上下文装载规则**（修正一：`account_id` 为空的记录不得自动进入单账户上下文）

| 会话范围 | 装入模型上下文 | 只在界面引用（带链接，不进上下文） | 永不装入 |
| --- | --- | --- | --- |
| `personal` + `single`（账户 A） | A 的 `single` 记录；A 会话自己的历史 | `all_personal` 且 `account_ids` 含 A 的记录，标"全部账户复查" | `unknown` 旧记录；其他账户的 `single`；`agentic`；`demo` |
| `personal` + `all_personal` | `all_personal` 记录；当前快照内各账户的 `single` 记录，逐条带账户标签 | 无 | `unknown`；`agentic`；`demo` |
| `agentic` | `micro_live` 记录；2.2 权限对象；Agentic 提案与订单 | 无 | 个人快照；个人记录；`demo` |
| `demo` | `core`／`sandbox` 记录 | 无 | 其余 |

聚合查询（`account_id` 缺省）返回 `all_personal` 与各 `single` 记录，每条自带范围字段；单账户查询（`account_id=A`）只返回 A 的
`single` 记录，`include_all_personal=true` 时追加覆盖 A 的 `all_personal` 记录。`unknown` 只在 `scope=unspecified` 的查询里出现。

**`GET /accounts`**（保留，只读，不开券商会话）

从 `Portfolio.accounts`（kept reading）和已保存的 advisor 快照合成。用途：账户切换器、深链参数校验、范围标签。

```json
{
  "checked_at": "2026-09-18T21:04:12+00:00",
  "accounts": [
    {"scope": "agentic", "account_scope_kind": "single", "account_id": "micro_live", "account_ids": [],
     "account_label": "Agentic", "is_real_money": true, "venue": "robinhood", "source": "ledger, reconciled",
     "as_of": "2026-09-17", "complete": true},
    {"scope": "personal", "account_scope_kind": "single", "account_id": "9c2e41b7a0d35f6e8b12", "account_ids": [],
     "account_label": "••••4321", "is_real_money": true, "venue": "robinhood", "source": "observed snapshot",
     "as_of": "2026-09-18T20:31:05+00:00", "complete": true},
    {"scope": "personal", "account_scope_kind": "all_personal", "account_id": null, "account_ids": ["9c2e41b7a0d35f6e8b12"],
     "account_label": "all personal accounts", "is_real_money": true, "venue": "robinhood", "source": "observed snapshot",
     "as_of": "2026-09-18T20:31:05+00:00", "complete": true}
  ],
  "personal_snapshot": {"fetched_at": "2026-09-18T20:31:05+00:00", "stale": false, "errors": []}
}
```

空数据（从未刷新过个人快照）：`accounts` 只有 Agentic 一项，`personal_snapshot.fetched_at: null`，`stale: true`。
UI 显示"尚未读取个人账户"，不显示 0 个账户。

过期（快照超过 5 分钟）：`personal_snapshot.stale: true`，个人账户条目仍列出，`complete` 按快照原值；
UI 标注快照时间，切换到个人分区时才发 `refresh`。

失败（Agentic 账本读不出）：`accounts` 里没有 `agentic` 项，加 `errors: {"agentic": "…"}`；HTTP 200。
配置读不出：HTTP 400 `{"detail": "configuration error: …"}`。

**`POST /advisor/jobs`**（扩展）

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `kind` | `refresh` \| `chat` \| `shortlist` \| `analysis` \| `portfolio_review` | `portfolio_review` 只读，不产生提案；页面加载和 `refresh` 不触发它 |
| `request_id` | UUID | 幂等；同 id 不同内容 → 409 |
| `scope` | `personal` \| `agentic`，默认 `personal` | `agentic` 下不允许 `shortlist`、`analysis`、`portfolio_review`、`refresh`（400）；Agentic 重新研究走 `POST /research` |
| `account_id` | 见词汇 | `personal` 下：给出则必须在快照里，服务端写 `account_scope_kind: single`；为 `null` 则写 `all_personal` 并把当时快照的成员写入 `account_ids` |
| `conversation_id` | 现有 | 首次使用时与 `(scope, account_scope_kind, account_id)` 绑定并持久化；后续请求范围不同 → 409 `"conversation is bound to another account scope"` |
| `ticker` | 现有 | `analysis` 必填；`portfolio_review` 忽略 |
| `language` | 现有 | |

响应：现有 job dict 加 `scope`、`account_scope_kind`、`account_id`、`account_ids`、`account_label`、`task_type`；`deduplicated` 不变。

**`GET /advisor?scope=personal&account_id=`**（扩展，旧字段保留；修正三）

- `decisions`、`jobs` 按上表装载规则过滤；`scope=agentic` 时 `snapshot` 为空对象，`decisions` 只含 `bucket == "micro_live"`。
- 新增 `scope`、`account_scope_kind`、`account_id`、`account_ids`、`account_label`、`latest_review`（见 2.4；没有则 `null`）。
- 新增 `coverage_info: {"scope": "personal", "instruments": "stocks_and_etfs", "note": "…"}`；**`coverage` 字符串保留不变**。
- 新增 `conversation_summaries: [{"conversation_id", "scope", "account_scope_kind", "account_id", "last_at"}]`；
  **`conversations` 字符串列表保留不变**。
- 旧字段在 `/openapi.json` 标 `deprecated: true`，前端迁移完成后再移除。

**`GET /advisor/conversations/{id}`**：响应新增 `scope`、`account_scope_kind`、`account_id`；请求方传的范围与绑定不一致时返回绑定值，不改绑定。

### 2.2 有效权限 `GET /config/authority`

一个确定性对象，由 `autonomy.effective_authority(profiles, …)` 计算，不开券商会话；`autonomous_reductions`、预检和设置页读同一函数。
`GET /config` 新增同一对象 `authority`，`autonomy` 子对象保留一轮。授权状态不代替下单前的实时检查：执行时仍重跑预检、重验、Hard Risk。

| 字段 | 说明 |
| --- | --- |
| `checked_at` | 计算时刻 |
| `version` | 输入的哈希（两份配置的 revision、`autonomy.json`、HALT 文件、pending 变更集合、stage）。见"授权版本流程" |
| `history` | 最近 10 次版本变化 `[{version, at, changed_inputs, paths, classification}]`；`classification` ∈ `tightened` \| `loosened` \| `mixed` \| `unrelated` \| `unknown` |
| `config_revisions` | `{"risk_profile.yaml": "…", "investor_profile.yaml": "…"}`，与 `/config.files[].revision` 相同 |
| `account` | `{scope:"agentic", account_id:"micro_live", venue:"robinhood", is_real_money:true}` |
| `stage`、`stage_allows` | 当前阶段与允许的模式 |
| `real_money_enabled`、`execution_mode` | `micro_live_execution.enabled` / `.execution_mode` |
| `kill_switch` | `{engaged, since, reason}` |
| `withdrawn` | `{tripped, reason, at}`（`autonomy.json`；只影响自主，不影响人工批准） |
| `actions` | `BUY`、`ADD`、`REDUCE`、`SELL` 四个键，每个：`mode`、`autonomous`、`human_approval`、`limits`（见下） |
| `limits` | 账户级限额，字段同第一稿（`capital`、`tiers`、`position_limits`、`class_caps_pct`、`concentration`、`activity`、`daily_loss_gate_pct`、`confidence_floors`、`freshness_class`、`permissions`、`event_blackouts`、`order_rules`） |
| `pending_changes` | 冷静期内的放宽变更（`config_changes.public` 形状） |
| `preflight` | `{ready, blocking:[…], checked_at, stale}`，取自 kept reading；`stale` 为 true 表示 venue 读数超过服务窗口，不能当现状。从未读过 venue 时 `ready: null`、`checked_at: null`、`stale: true`，配置门仍按当前配置评估 |
| `consistency` | 交叉检查发现的不一致 `[{code, detail, effect}]` |

**动作对象**（修正四：区分"阻止自主执行"与"阻止所有执行"）

```json
{"mode": "human_approval",
 "autonomous":     {"permitted": false, "blocked_by": [{"code": "not_enabled", "detail": "…"}]},
 "human_approval": {"permitted": true,  "blocked_by": []},
 "limits": {}}
```

- `human_approval.blocked_by` 代码（阻止**所有**执行，含人工）：`kill_switch`、`real_money_disabled`、`mode_recommend_only`、
  `stage`、`preflight`（`detail` 列出失败的 PF id 与读数时间）、`ladder_halt`、`config_error`。
- `autonomous.blocked_by` 代码（只阻止自主）：`not_enabled`、`no_min_confidence`、`no_classes`、`withdrawn`、`never_autonomous`、
  `stage`（阶段不允许 `autonomous` 模式；SELL 不列）、`pending_change`（信息，不阻止）、`authority_changed`（提案级，见下）。
  人工被阻止的原因自动继承到自主一侧。
- `mode` 派生：`autonomous.permitted` → `autonomous`；否则 `human_approval.permitted` → `human_approval`；否则 `blocked`。
- 计划 5.3 的"SELL 独立授权"在本轮表现为：`SELL.autonomous.permitted` 恒为 false、`code: never_autonomous`；
  `SELL.limits.quantity_basis` 说明数量来自账本全仓并经 HR14／HR14b 校验。单独的 SELL 自主授权对象不在本轮。

**授权版本流程**（修正四）

| 记录点 | 字段 | 写入者 |
| --- | --- | --- |
| 研究提交 | job `authority_version` | `start_research` |
| 提案创建 | proposal `authority_version` = **研究所依据的版本**（job 的版本；手工启动的运行取研究开始时的版本）。若写提案时的版本已不同：`authority_changed_during_research: true`，并强制 `requires_human_approval: true`（转人工），历史加一条 `authority_changed_during_research`；决策记录带 `authority: {research, proposal, changed_during_research}` | `research_worker` → `cli.run_one` → `new_proposal` |
| 版本变化 | state meta `authority_versions`（有界 50 条）：`{version, at, changed_inputs: [risk_profile \| investor_profile \| autonomy_json \| halt_file \| pending_changes \| stage], paths, classification}` | `effective_authority()` 发现版本与上次不同时追加；`paths`／`classification` 取自匹配的 `config_changes` 记录，控台之外的编辑记 `paths: ["(edited outside the console)"]`、`classification: unknown` |
| 审批请求 | `ApproveRequest.authority_version`（可选，前端上线后改必填）、`acknowledge_authority_change`（bool，默认 false）。**带 `acknowledge_authority_change: true` 时必须同时带 `authority_version`**，缺失 400；确认因此绑定到被确认的那个版本，确认期间再变则 409 | 控台 |

| 情形 | 处理 |
| --- | --- |
| 请求带的 `authority_version` ≠ 当前 | 409 `"authority changed since the screen loaded; reload and review"`，与提案 `version` 不一致同样处理 |
| 提案记录的 `authority_version` ≠ 当前，变化为 `tightened` 或 `unrelated` | 不拒绝。响应与 `GET /proposals/{id}` 带 `authority_changed: {from, to, at, paths, classification}`；执行按当前配置重验 |
| 变化为 `loosened`、`mixed` 或 `unknown` | 需要 `acknowledge_authority_change: true`，否则 409 `"authority loosened since this proposal was made; review and acknowledge"`。前端在批准前显示变化摘要与确认框 |
| 提案无版本（旧提案，`authority_version: null`） | 视为 `unknown`：同上要求确认；`authority_changed.from: null`，`detail: "authority version not recorded at proposal time"` |
| 自主批准 | `autonomous_reductions` 要求 `proposal.authority_version == current.version`，否则 `declined.reasons: ["authority changed since research"]`，留给人。研究结束后收紧的授权由此立即生效 |
| 研究进行中授权变化 | 提案照常创建但**转人工**：`requires_human_approval: true`，自主循环永不取它；`authority_version` 保留研究所依据的版本，所以人工批准时的 `authority_changed` 从研究版本算到当前，放宽或未知仍需确认；`authority_changed_during_research: true` 进入 `ProposalDetail` 与 `/attention` 的 `proposal_waiting.detail` |

正常（当前配置，无 pending，未撤回，预检就绪）：

```json
{
  "checked_at": "2026-09-18T21:04:12+00:00",
  "version": "b31d7e0a9f4c2d11",
  "history": [{"version": "b31d7e0a9f4c2d11", "at": "2026-09-18T20:27:58+00:00", "changed_inputs": ["risk_profile"],
               "paths": ["research_schedule.tickers"], "classification": "unrelated"}],
  "config_revisions": {"risk_profile.yaml": "5d2c…", "investor_profile.yaml": "a90f…"},
  "account": {"scope": "agentic", "account_id": "micro_live", "venue": "robinhood", "is_real_money": true},
  "stage": 3, "stage_allows": ["recommend_only", "human_approval"],
  "real_money_enabled": true, "execution_mode": "human_approval",
  "kill_switch": {"engaged": false, "since": null, "reason": null},
  "withdrawn": {"tripped": false, "reason": null, "at": null},
  "actions": {
    "BUY": {"mode": "human_approval",
            "autonomous": {"permitted": false, "blocked_by": [
              {"code": "not_enabled", "detail": "buckets.micro_live.autonomy.open.enabled is not true"},
              {"code": "no_min_confidence", "detail": "buckets.micro_live.autonomy.open declares no min_confidence"}]},
            "human_approval": {"permitted": true, "blocked_by": []},
            "limits": {"classes": ["broad_diversified_etf", "bond_or_cash_like_etf"], "max_pct_of_account": 20,
                       "max_new_positions_per_day": 1, "min_confidence": null, "used_today": 0}},
    "ADD": {"mode": "human_approval",
            "autonomous": {"permitted": false, "blocked_by": [
              {"code": "not_enabled", "detail": "buckets.micro_live.autonomy.open.enabled is not true"},
              {"code": "no_min_confidence", "detail": "buckets.micro_live.autonomy.open declares no min_confidence"}]},
            "human_approval": {"permitted": true, "blocked_by": []},
            "limits": {"classes": ["broad_diversified_etf", "bond_or_cash_like_etf"], "max_pct_of_account": 20,
                       "max_new_positions_per_day": 1, "min_confidence": null, "used_today": 0}},
    "REDUCE": {"mode": "human_approval",
               "autonomous": {"permitted": false, "blocked_by": [
                 {"code": "not_enabled", "detail": "buckets.micro_live.autonomy.reduce.enabled is not true"}]},
               "human_approval": {"permitted": true, "blocked_by": []},
               "limits": {"min_confidence": 80, "max_positions_reduced_per_day": 1, "max_fraction_of_holding": 0.5, "used_today": 0}},
    "SELL": {"mode": "human_approval",
             "autonomous": {"permitted": false, "blocked_by": [
               {"code": "never_autonomous", "detail": "SELL is never autonomous; closing a position outright belongs to a person"}]},
             "human_approval": {"permitted": true, "blocked_by": []},
             "limits": {"quantity_basis": "full ledger holding (HR14, HR14b)", "sell_quantity_validated_against": "ledger"}}
  },
  "limits": {
    "capital": {"max_capital_usd": 2000, "tolerance_usd": 50, "sizing_basis": "live_equity"},
    "tiers": {"low": {"classes": ["broad_diversified_etf", "bond_or_cash_like_etf"], "max_pct_of_account": 60, "max_order_usd": 200},
              "mid": {"classes": ["sector_factor_thematic_etf"], "max_pct_of_account": 25, "max_order_usd": 80},
              "high": {"classes": ["individual_stock"], "max_pct_of_account": 15, "max_order_usd": 50}},
    "position_limits": {"max_single_position_pct": 40, "max_open_positions": 3},
    "class_caps_pct": {"broad_diversified_etf": 40, "bond_or_cash_like_etf": 40, "sector_factor_thematic_etf": 25, "individual_stock": 10},
    "concentration": {"all_individual_stocks_pct": 25, "direct_sector_pct": 25, "max_etfs_per_exposure_group": 1},
    "activity": {"max_new_positions_per_day": 1, "max_new_positions_per_week": 2, "max_orders_per_week": 4, "no_same_day_round_trips": true},
    "daily_loss_gate_pct": -5,
    "confidence_floors": {"broad_etf_half_size": 70, "broad_etf_full_size": 80, "sector_factor_etf": 80, "individual_stock": 85, "autonomous_any": 80, "no_trade_below": 50},
    "freshness_class": {"name": "core_conservative", "ttl_minutes": 2880, "stale_after_minutes": 960, "max_price_drift_pct": 3.0},
    "permissions": {"equities_long": true, "fractional": true, "options": false, "crypto": false, "short_selling": false, "margin": false, "extended_hours": false},
    "event_blackouts": {"macro": {"applies_to": "autonomous_opens_only", "maintained_through": "2026-12-10"},
                        "earnings": {"applies_to": "every_buy", "days_before": 5, "days_after_no_add": 2}},
    "order_rules": {"order_type": "market", "prefer_limit_in_whole_shares": true, "market_hours": "regular_hours", "time_in_force": "gfd"}
  },
  "pending_changes": [],
  "preflight": {"ready": true, "blocking": [], "checked_at": "2026-09-18T21:03:40+00:00", "stale": false},
  "consistency": [
    {"code": "open_without_min_confidence",
     "detail": "buckets.micro_live.autonomy.open has no min_confidence; PF04 does not read autonomy.open.enabled",
     "effect": "enabling autonomy.open would pass preflight and still be refused by permits()"}
  ]
}
```

空数据（配置读不出，`/health.status = CONFIG_ERROR`）：只有三个字段，其余缺省而非置零。

```json
{"checked_at": "2026-09-18T21:04:12+00:00", "status": "CONFIG_ERROR",
 "detail": "configuration error: risk_profile.yaml: buckets.micro_live.tiers.low.max_order_usd must be a number"}
```

过期（一条放宽变更冷静期已到但循环还没应用；venue 读数超过服务窗口且上次读到 PF01／PF02 失败）：

```json
{
  "checked_at": "2026-09-18T21:04:12+00:00", "version": "0e7a…",
  "history": [{"version": "0e7a…", "at": "2026-09-17T20:58:00+00:00", "changed_inputs": ["pending_changes"],
               "paths": ["buckets.micro_live.autonomy.reduce.enabled"], "classification": "unrelated"}],
  "pending_changes": [{"id": "chg_2f0b…", "file": "risk_profile.yaml",
                       "paths": ["buckets.micro_live.autonomy.reduce.enabled"],
                       "classification": "cooling", "status": "pending",
                       "apply_after": "2026-09-18T20:58:00+00:00", "requested_by": "owner",
                       "reasons": ["buckets.micro_live.autonomy.reduce.enabled: false -> true loosens"]}],
  "actions": {"REDUCE": {"mode": "blocked",
                         "autonomous": {"permitted": false, "blocked_by": [
                           {"code": "not_enabled", "detail": "buckets.micro_live.autonomy.reduce.enabled is not true"},
                           {"code": "pending_change", "detail": "chg_2f0b… applies at 2026-09-18T20:58:00+00:00; withdrawable until applied"},
                           {"code": "preflight", "detail": "PF01, PF02 failing as of 2026-09-18T20:41:02+00:00; reading is stale"}]},
                         "human_approval": {"permitted": false, "blocked_by": [
                           {"code": "preflight", "detail": "PF01, PF02 failing as of 2026-09-18T20:41:02+00:00; reading is stale"}]}}},
  "preflight": {"ready": false, "blocking": ["PF01", "PF02"], "checked_at": "2026-09-18T20:41:02+00:00", "stale": true}
}
```

（其余字段同正常样例，此处省略。）UI 规则：`pending_changes` 非空显示"待生效 + 撤回入口"，不显示为等待批准的订单；
`preflight.stale` 为 true 时把 `blocked` 显示为"待确认"，不把 `ready` 或 `blocked` 当现状。

失败：`{"detail": "internal error; inspect the local server log"}`，HTTP 500。UI 显示"权限状态无法确认"，
禁用一切依赖权限判断的按钮。

### 2.3 当前待办 `GET /attention`

服务端从未解决的状态计算，复用 `collect_notifications` 的采集规则（重构为 `collect_conditions()`，通知与待办各取所需）。
不读推送投递记录，不读历史事件是否存在。

| 字段 | 说明 |
| --- | --- |
| `checked_at` | |
| `readable` | 所有采集器都读到了状态。false 时 `unreadable` 列出读不到的来源，`items` 可能不全，UI 必须显示"无法确认" |
| `unreadable` | `["ledger_report: …", …]` |
| `top` | 优先级最高的一项（与 `items[0]` 相同），无事时 `null` |
| `items` | 按优先级排序 |
| `summary` | `{total, requires_person, by_severity: {critical, high, normal, info}}` |
| `context` | 不是待办但影响文案的状态：`{market_open, paused, autonomy_armed, autonomous_queue, next_scheduled_at}` |

每项：

| 字段 | 说明 |
| --- | --- |
| `id` | 稳定键，与通知 `key` 同规则（例 `reconciliation_error:<proposal_id>`）；退出条件满足后消失，不因订单、研究、推送各有记录而重复 |
| `kind` | 闭集，见下表 |
| `severity` | `critical` \| `high` \| `normal` \| `info` |
| `priority` | 1 订单状态不明／对账与账本差异／授权连接失效；2 风控停机或需手动恢复；3 需人工判断的提案；4 需干预的研究或后台任务；5 信息 |
| `requires_person` | 只有人能解决 |
| `scope` / `account_scope_kind` / `account_id` / `account_ids` / `account_label` | `scope` ∈ `agentic` \| `personal` \| `system` |
| `subject` | 提案 id、run_key、账本名或变更 id |
| `title` / `detail` | 英文原文；前端按 `kind` 本地化标题，原文作回退与详情 |
| `since` | 该状态开始的时间 |
| `link` | `{page, params}`，键见 2.0 白名单 |
| `actions` | 闭集：`approve`、`reject`、`reanalyze`、`confirm_ledger`、`record_cashflow`、`withdraw_change`、`reset_autonomy`、`resume`、`retry`、`cancel`、`open` |

**出现与退出规则**（修正六）

| `kind` | 出现 | 退出 | 严重性／优先级 |
| --- | --- | --- | --- |
| `order_unknown` | 跟踪订单状态 `UNKNOWN`／`SUBMITTING` | 对账把订单推到终态 | critical／1 |
| `reconciliation_error` | 提案 `reconciliation_error` 非空 | 一次成功对账清空它 | critical／1 |
| `ledger_unconfirmed` | 最近一份账本报告有拒绝或错误 | 更新的报告 `confirmed: true`，或差额已用 `record_cashflow` 记录后复查通过 | critical／1 |
| `credential_invalid` | 真钱开启且 `connect_error` 非空 | 后续探测连上 | critical／1 |
| `risk_halt` | 任一梯子 `manual_reset_required` 或 `/health = HALTED` | 手动重置 | critical／2 |
| `autonomy_withdrawn` | `autonomy.json.tripped` | `/autonomy/resume` | high／2 |
| `proposal_waiting` | `WAITING_FOR_APPROVAL` 且新鲜度 `VALID`／`STALE` | 提案离开 `PROPOSED`，或新鲜度到 `EXPIRED`（转为 `proposal_stale`） | high／3 |
| `proposal_stale` | `PROPOSED` 且 `EXPIRED` | 被拒绝、重研，或存储层标记过期 | normal／3 |
| `research_failed` | `agentic`／`demo` 任务 `FAILED`／`TIMED_OUT`，72 小时内，且没有更新的同 `(scope, ticker, task_type)` 任务处于 `QUEUED`／`RUNNING`／`COMPLETE` | 出现更新的任务；用户取消（`CANCELLED` 不列）；满 72 小时后只留在 Activity | normal／4 |
| `research_limit_exhausted` | `CLAUDE_LIMIT_REACHED` 且 `limit_recoveries >= max_limit_recoveries` | 重新排队、取消，或更新任务完成 | normal／4 |
| `backup_overdue` | 后台活动 `overdue`（backup、maintenance、ledger_confirm） | 活动成功 | normal／4 |
| `config_change_pending` | 冷静期记录 `pending` | 应用、撤回或失败 | info／5，`requires_person: false` |
| `config_change_failed` | 冷静期记录 `failed` | 同路径提交新变更，或满 72 小时 | high／4 |
| `personal_task_done` | 用户显式请求的 `analysis`／`portfolio_review` 完成，24 小时内，`opened_at` 为空 | 前端在结果实际展示后调用 `POST /research/{run_key}/opened`（幂等写，走现有写请求保护）写入 `opened_at`；**任何 GET 都不写**，轮询详情不会消掉提醒；或满 24 小时 | info／5，`requires_person: false` |
| `personal_task_failed` | 同上两类任务 `FAILED`／`TIMED_OUT`，72 小时内 | 结果被打开；同 `(account_scope_kind, account_id, ticker, task_type)` 的更新任务完成；或满 72 小时 | normal／4 |
| `personal_change_found`（预留，P1 后） | 新 `analysis` 的 `changes_since_previous` 非空，24 小时内 | 结果被打开，或满 24 小时 | info／5 |

不列为待办：正常自主提案排队、休市、用户主动暂停、`CLAUDE_LIMIT_REACHED` 且还有恢复次数、个人快照自然变旧、
历史成功记录。`CANCELLED` 任务永不列出。

正常：

```json
{
  "checked_at": "2026-09-18T21:04:12+00:00", "readable": true, "unreadable": [],
  "top": {
    "id": "reconciliation_error:p_8c1d2e", "kind": "reconciliation_error", "severity": "critical", "priority": 1,
    "requires_person": true, "scope": "agentic", "account_scope_kind": "single", "account_id": "micro_live",
    "account_ids": [], "account_label": "Agentic",
    "subject": "p_8c1d2e", "title": "BND order could not be reconciled",
    "detail": "order synchronization unavailable: venue returned no status for ct-p_8c1d2e-1; lookup required",
    "since": "2026-09-18T15:32:10+00:00",
    "link": {"page": "activity", "params": {"subject": "p_8c1d2e"}}, "actions": ["open"]
  },
  "items": [
    {"id": "reconciliation_error:p_8c1d2e", "kind": "reconciliation_error", "severity": "critical", "priority": 1,
     "requires_person": true, "scope": "agentic", "account_scope_kind": "single", "account_id": "micro_live",
     "account_ids": [], "account_label": "Agentic", "subject": "p_8c1d2e",
     "title": "BND order could not be reconciled",
     "detail": "order synchronization unavailable: venue returned no status for ct-p_8c1d2e-1; lookup required",
     "since": "2026-09-18T15:32:10+00:00", "link": {"page": "activity", "params": {"subject": "p_8c1d2e"}}, "actions": ["open"]},
    {"id": "proposal_waiting:p_41aa07:e3b0…", "kind": "proposal_waiting", "severity": "high", "priority": 3,
     "requires_person": true, "scope": "agentic", "account_scope_kind": "single", "account_id": "micro_live",
     "account_ids": [], "account_label": "Agentic", "subject": "p_41aa07",
     "title": "REDUCE VTI needs your approval", "detail": "$21.50 in micro_live (real money)",
     "since": "2026-09-18T14:05:44+00:00", "link": {"page": "approvals", "params": {"proposal": "p_41aa07"}},
     "actions": ["approve", "reject", "reanalyze"]},
    {"id": "personal_task_done:advisor_5b7c…", "kind": "personal_task_done", "severity": "info", "priority": 5,
     "requires_person": false, "scope": "personal", "account_scope_kind": "all_personal", "account_id": null,
     "account_ids": ["9c2e41b7a0d35f6e8b12"], "account_label": "all personal accounts", "subject": "advisor_5b7c…",
     "title": "portfolio review finished", "detail": "3 priorities; 1 account had incomplete totals",
     "since": "2026-09-18T20:36:48+00:00", "link": {"page": "advisor", "params": {"scope": "personal"}}, "actions": ["open"]},
    {"id": "config_change_pending:chg_2f0b…", "kind": "config_change_pending", "severity": "info", "priority": 5,
     "requires_person": false, "scope": "system", "account_scope_kind": "single", "account_id": null,
     "account_ids": [], "account_label": null, "subject": "chg_2f0b…",
     "title": "a loosening change to risk_profile.yaml applies at 2026-09-19 20:58 UTC",
     "detail": "buckets.micro_live.autonomy.reduce.enabled — requested by owner; withdraw it from Settings",
     "since": "2026-09-18T20:58:00+00:00", "link": {"page": "settings", "params": {"change": "chg_2f0b…"}},
     "actions": ["withdraw_change"]}
  ],
  "summary": {"total": 4, "requires_person": 2, "by_severity": {"critical": 1, "high": 1, "normal": 0, "info": 2}},
  "context": {"market_open": false, "paused": false, "autonomy_armed": false, "autonomous_queue": 0,
              "next_scheduled_at": "2026-09-25T13:30:00-07:00"}
}
```

空数据：

```json
{"checked_at": "2026-09-18T21:04:12+00:00", "readable": true, "unreadable": [], "top": null, "items": [],
 "summary": {"total": 0, "requires_person": 0, "by_severity": {"critical": 0, "high": 0, "normal": 0, "info": 0}},
 "context": {"market_open": true, "paused": false, "autonomy_armed": false, "autonomous_queue": 0,
             "next_scheduled_at": "2026-09-25T13:30:00-07:00"}}
```

UI 显示"无需处理"，**只在 `readable: true` 时**。

过期（提案超过 TTL，动作从批准变为重研）：

```json
{"checked_at": "2026-09-18T21:04:12+00:00", "readable": true, "unreadable": [],
 "top": {"id": "proposal_stale:p_41aa07:e3b0…", "kind": "proposal_stale", "severity": "normal", "priority": 3,
         "requires_person": true, "scope": "agentic", "account_scope_kind": "single", "account_id": "micro_live",
         "account_ids": [], "account_label": "Agentic", "subject": "p_41aa07",
         "title": "REDUCE VTI expired before a decision",
         "detail": "age 2905 minutes against a 2880 minute window; approval would be refused, request fresh research",
         "since": "2026-09-16T14:05:44+00:00", "link": {"page": "approvals", "params": {"proposal": "p_41aa07"}},
         "actions": ["reanalyze", "reject"]},
 "items": ["…同 top…"],
 "summary": {"total": 1, "requires_person": 1, "by_severity": {"critical": 0, "high": 0, "normal": 1, "info": 0}},
 "context": {"market_open": false, "paused": false, "autonomy_armed": false, "autonomous_queue": 0, "next_scheduled_at": null}}
```

失败（部分来源读不到，HTTP 200）：

```json
{"checked_at": "2026-09-18T21:04:12+00:00", "readable": false, "unreadable": ["ledger_report: database is locked"],
 "top": null, "items": [],
 "summary": {"total": 0, "requires_person": 0, "by_severity": {"critical": 0, "high": 0, "normal": 0, "info": 0}},
 "context": {"market_open": false, "paused": false, "autonomy_armed": false, "autonomous_queue": 0, "next_scheduled_at": null}}
```

整个请求失败：HTTP 500 `{"detail": …}`。两种情况 UI 都显示"无法确认"，不显示"无需处理"。

### 2.4 研究任务与结果

**任务类型**

| `task_type` | 入口 | 范围 | 产物 | 能否产生提案 |
| --- | --- | --- | --- | --- |
| `full_research` | `POST /research` | `agentic` \| `demo` | `decision_record.json` + 报告树 | 能（只有 `agentic`／`demo` 桶，经 Hard Risk） |
| `analysis`（只读个股复查） | `POST /advisor/jobs kind=analysis` | `personal` | `decision_record.json`，`advisory: true` | 否 |
| `portfolio_review` | `POST /advisor/jobs kind=portfolio_review` | `personal` | `assistant_result.review` | 否 |
| `chat` / `shortlist` / `refresh` | `POST /advisor/jobs` | `personal` | `assistant_result` | 否；不是研究记录，不参与覆盖 |
| `thesis_check`（P1 后） | 预留 | `agentic` | 复查结论，`refers_to_run_id` 指向被复查的 `full_research` | 否（只产生"需重研"信号） |

**`POST /research`**（扩展）

| 字段 | 规则 |
| --- | --- |
| `ticker`、`instrument_type`、`trade_date`、`trigger` | 现有 |
| `bucket` | 现有；`scope` 给出时必须一致（`agentic` ↔ `micro_live`；`demo` ↔ `core`/`sandbox`），否则 400 |
| `scope` | `agentic` \| `demo`；缺省按 `bucket` 推断 |
| `request_id` | UUID，可选。同 id 重放返回原 job 且 `deduplicated: true`；同 id 不同内容 409。没有 id 时沿用现有去重键 |
| `purpose` | `review_holding` \| `candidate` \| `exit_review`，可选。`review_holding`／`exit_review` 要求账本持有该标的（否则 400）；`candidate` 不要求 |

响应：现有 job dict 加 `scope`、`account_scope_kind`、`account_id`、`account_ids`、`account_label`、`task_type: "full_research"`、
`purpose`、`request_id`、`authority_version`。

**Job 形状**（`GET /research/jobs?scope=&account_id=&limit=`、`GET /research/{run_key}`）

现有字段不变。新增：

| 字段 | 说明 |
| --- | --- |
| `scope`、`account_scope_kind`、`account_id`、`account_ids`、`account_label` | 服务端写入；`advisory` 桶映射为 `personal`；旧任务无范围信息时 `account_scope_kind: unknown` |
| `task_type`、`purpose`、`request_id` | 见上；旧任务按 `task.kind` 或桶推断，`request_id: null` |
| `route` | `{"name": "tradingagents" \| "assistant" \| "skill:<id>", "package_version": "0.1.0", "upstream_commit": "9dee508c…", "skill_version": null, "models": {"deep": "…", "quick": "…"}}` |
| `authority_version` | 提交时的授权版本 |
| `opened_at` | 前端确认结果已展示后经 `POST /research/{run_key}/opened` 写入的时间（幂等：已写则原样返回）；GET 不写；用于 2.3 的退出规则 |
| `cancellable` | `state in (QUEUED, RUNNING)` |

`state` 枚举不变：`QUEUED`、`RUNNING`、`COMPLETE`、`FAILED`、`TIMED_OUT`、`CLAUDE_LIMIT_REACHED`、`CANCELLED`。

**记录形状**（`GET /research?scope=&account_id=&task_type=&include_all_personal=&limit=`）

现有字段不变。新增：

| 字段 | 说明 |
| --- | --- |
| `scope`、`account_scope_kind`、`account_id`、`account_ids`、`account_label`、`task_type`、`purpose`、`route`、`authority_version` | 同 job；旧记录 `scope: "unspecified"`、`account_scope_kind: "unknown"`、`route.name: "unknown"`，不猜归属 |
| `snapshot_as_of` | 研究所用账户或账本快照时间（`portfolio.as_of` 或个人 `fetched_at`） |
| `prices_as_of` | 价格锚定的交易日（`trade_date`） |
| `sources_retrieved_at_max` | 本侧读取来源的最晚时间（`evidence.web_sources[].retrieved_at`）；没有为 `null` |
| `facts_as_of`、`facts_as_of_basis` | 事实所属时间。**不从读取时间推导。** 只有每条实质来源都记录了事件时间或报告期（P1 证据升级后写入）才给出，`basis` ∈ `event_time` \| `reported_period`；否则 `facts_as_of: null`、`basis: "unknown"` |
| `evidence` | `{"sources": n, "web_fetches": n, "cli_web_search_used": false}` |
| `limitations` | `{"data_quality": "good\|partial\|poor\|null", "missing_information": [...], "unresolved_profile_fields": n}` |
| `counter_evidence`、`counter_evidence_truncated` | 熊方摘要前 600 字与是否截断；完整文本经 `GET /research/{run_id}/trace` 的 `bear` 步骤读取；没有为 `null`、`false` |
| `changes_since_previous` | 同覆盖键上一条记录以来的变化摘要；P1 前为 `null` |
| `refers_to_run_id` | 轻量任务引用的完整研究；`full_research` 为 `null` |
| `superseded_by` | 同覆盖键更新记录的 `run_id`（见下表）；摘要按覆盖键取最新未被覆盖者，不按 ticker 或文件时间覆盖 |
| `applicable_floor` | 该任务在其范围下的开仓置信度地板（来自 2.2）；个人任务为 `null` |

**覆盖键**（修正二）

| `task_type` | 覆盖键 | 可覆盖的类型 |
| --- | --- | --- |
| `full_research` | `(scope, bucket, ticker, purpose)` | 仅 `full_research` |
| `analysis` | `(scope, account_scope_kind, account_id, ticker)` | 仅 `analysis` |
| `portfolio_review` | `(scope, account_scope_kind, account_id)` | 仅 `portfolio_review` |
| `thesis_check` | 不覆盖，`refers_to_run_id` 指向被复查记录 | 无 |
| `chat` / `shortlist` / `refresh` | 不是记录，不参与 | 无 |

`all_personal` 的记录不覆盖任何 `single` 记录，反之亦然；`unknown` 记录不参与覆盖。

**`portfolio_review` 结果**（`GET /research/{run_key}.assistant_result.review`，`GET /advisor.latest_review`）

```json
{
  "scope": "personal", "account_scope_kind": "all_personal", "account_id": null, "account_ids": ["9c2e41b7a0d35f6e8b12"],
  "account_label": "all personal accounts",
  "snapshot_at": "2026-09-18T20:31:05+00:00", "analysed_at": "2026-09-18T20:36:48+00:00",
  "route": {"name": "assistant", "package_version": "0.1.0", "models": {"quick": "claude-sonnet-5"}},
  "priorities": [
    {"rank": 1, "symbol": "NVDA", "recommendation": "needs_full_research",
     "rationale": "23% of equity in one name against a stated preference for low concentration; last full review is 41 days old.",
     "citations": [{"kind": "run", "run_id": "advisor_5b7c…", "as_of": "2026-08-08"}, {"kind": "snapshot", "as_of": "2026-09-18T20:31:05+00:00"}],
     "missing": ["cost basis for the lot bought 2026-07"]},
    {"rank": 2, "symbol": "VXUS", "recommendation": "hold",
     "rationale": "Within the international sleeve the profile describes; no new facts since the 2026-09-05 review.",
     "citations": [{"kind": "run", "run_id": "advisor_90aa…", "as_of": "2026-09-05"}], "missing": []},
    {"rank": 3, "symbol": "CASH", "recommendation": "wait_for_evidence",
     "rationale": "18% cash; the profile's liquidity need is unconfirmed, so no deployment suggestion is made.",
     "citations": [], "missing": ["confirmed cash need over the next 12 months"]}
  ],
  "limitations": ["options and crypto positions are not read", "1 of 2 accounts had incomplete totals"],
  "stale": false
}
```

`recommendation` 闭集：`hold`、`consider_change`、`wait_for_evidence`、`needs_full_research`。`consider_change` 的增减方向
在 `rationale` 里说明；四个值只表达建议，不对应任何执行动作，也不会变成提案。最多三项。
`stale` 为 true 表示 `snapshot_at` 早于当前快照的 `fetched_at`；旧结果仍显示，标注两个时间，刷新持仓不会使它变新。

样例：

- 正常：`GET /research/jobs?scope=agentic` 返回一个 `COMPLETE` job，`proposal_created: true`，`route.name: "tradingagents"`，
  `purpose: "review_holding"`；`GET /research?scope=agentic` 对应记录 `final_action: "REDUCE"`，`superseded_by: null`，
  `facts_as_of: null`、`facts_as_of_basis: "unknown"`，`prices_as_of: "2026-09-17"`。
- 空数据：`GET /research?scope=personal&account_id=9c2e…` → `[]`（HTTP 200）；`GET /advisor.latest_review: null`。
  UI 显示"尚未分析"，不补成 HOLD。
- 过期：job `state: "TIMED_OUT"`，`error: "research exceeded 5400 seconds"`，`cancellable: false`；或记录 `superseded_by` 非空、
  `portfolio_review.stale: true`。
- 失败：`POST /research` 带 `purpose: review_holding` 而账本未持有 → 400 `{"detail": "AAPL is not held in micro_live; use purpose=candidate"}`；
  `POST /advisor/jobs` 的 `account_id` 不在快照 → 400 `{"detail": "unknown personal account; refresh the snapshot"}`；
  同会话仍在回答 → 409 `{"detail": "The assistant is still answering this conversation."}`；
  会话范围不符 → 409 `{"detail": "conversation is bound to another account scope"}`；job `state: "FAILED"`，`error` 原文。

### 2.5 评估 `GET /evaluation?scope=`

| 参数 | 说明 |
| --- | --- |
| `scope` | `demo`（现有 study，默认，保持兼容）\| `agentic`（新序列，从声明的起点开始）\| `route_quality`（研究路线指标，不含收益） |

响应（`demo` / `agentic`）：现有字段不变（`study_id`、`state`、`started_on`、`declared`、`days_recorded`、`config_drifted`、
`frozen_fingerprint`、`current_fingerprint`、`strategy`、`benchmark`、`strategy_minus_benchmark_pct_points`、`decisions`、
`execution`、`stop_rules_triggered`、`readable`、`conclusive`、`caveat`）。新增与改动：

| 字段 | 说明 |
| --- | --- |
| `scope`、`account` | `account = {scope, account_id, account_label, is_real_money}` |
| `buckets` | 该范围计入的桶：`demo` → `["core","sandbox"]`，`agentic` → `["micro_live"]` |
| `decisions`、`execution` | **只计该范围的桶**（今天是混合计数，见 1.5） |
| `sample` | `{"started_on", "last_snapshot_on", "days_recorded", "gaps": [{"from","to","reason"}], "stale": bool}`；`stale` 为最近快照落后最近交易日超过 2 个交易日 |
| `state` | `RUNNING` \| `STOPPED` \| `NOT_STARTED`；`NOT_STARTED` 时 HTTP 200，`readable: false`，收益字段为 `null` |

`route_quality` 响应：

```json
{
  "scope": "route_quality", "checked_at": "2026-09-18T21:04:12+00:00", "since": "2026-08-19",
  "routes": [
    {"route": "tradingagents", "runs": 14, "complete": 12, "failed": 1, "timed_out": 1, "limit_hits": 2,
     "median_elapsed_seconds": 1840, "llm_calls_total": 233, "format_pass_rate_pct": null,
     "evidence_check_pass_rate_pct": 91.7, "fact_accuracy_pct": null, "human_edit_rate_pct": null,
     "evaluated": {"timing": true, "calls": true, "failures": true, "format": false, "evidence": true, "facts": false, "edits": false}},
    {"route": "assistant", "runs": 31, "complete": 30, "failed": 1, "timed_out": 0, "limit_hits": 0,
     "median_elapsed_seconds": 62, "llm_calls_total": 31, "format_pass_rate_pct": 96.8,
     "evidence_check_pass_rate_pct": null, "fact_accuracy_pct": null, "human_edit_rate_pct": null,
     "evaluated": {"timing": true, "calls": true, "failures": true, "format": true, "evidence": false, "facts": false, "edits": false}}
  ],
  "note": "fact accuracy and human edits come from the fixed-case review (plan section 4); null means not evaluated, not zero"
}
```

样例：

- 正常（`demo`，study 运行中）：现有形状 + `scope: "demo"`、`buckets: ["core","sandbox"]`、`sample.stale: false`。
- 空数据（`agentic` 未声明）：

```json
{"scope": "agentic", "account": {"scope": "agentic", "account_id": "micro_live", "account_label": "Agentic", "is_real_money": true},
 "buckets": ["micro_live"], "state": "NOT_STARTED", "readable": false, "conclusive": false,
 "strategy": {"return_pct": null, "max_drawdown_pct": null, "equity_usd": null, "net_deposits_usd": null},
 "benchmark": {"weights_pct": {"VTI": 60, "BND": 40}, "return_pct": null, "max_drawdown_pct": null, "equity_usd": null},
 "decisions": {"research_runs": 3, "run_outcomes": {"NO_TRADE": 2, "REDUCE": 1}, "job_states": {"COMPLETE": 3}, "proposal_statuses": {"EXECUTED": 1}},
 "sample": {"started_on": null, "last_snapshot_on": null, "days_recorded": 0, "gaps": [], "stale": false},
 "caveat": "no Agentic study has been declared; decisions are counted from the record, performance is not"}
```

- 过期：`sample.stale: true`，`sample.last_snapshot_on: "2026-09-12"`，`config_drifted: true`，`caveat` 说明配置变了、样本不同质。
- 失败：`GET /evaluation?scope=live` → 400 `{"detail": "unknown evaluation scope 'live'"}`；内部错误 500。
  今天 `demo` 无 study 时返回 500，契约改为上面的 `NOT_STARTED` 形状。

---

## 3. 兼容与迁移

- 历史个人会话保持 `personal` 用途；缺账户信息的历史记录 `scope: "unspecified"`、`account_scope_kind: "unknown"`，
  不进入任何模型上下文，不参与覆盖，不获得执行资格。`unknown`（范围未记录）与 `all_personal`（明确的全部个人账户）是两个值，
  界面分别显示"旧记录／范围未指定"与"全部个人账户"。
- 进入模型上下文的记录按 2.1 装载规则表选取；Agentic 聊天只装 `micro_live` 记录与 2.2 权限对象，不装个人快照。
- 同一标的多账户多建议：摘要按 2.4 覆盖键取最新未被 `superseded_by` 覆盖的记录；聊天与轻量复查不覆盖完整研究。
- 去重：`request_id`（UUID）优先；缺省时沿用 `dedupe_key`。`full_research`、`analysis`、`portfolio_review`、Agentic 重新研究各自去重，互不合并。
- 兼容字段：`/advisor.coverage`（字符串）与 `conversations`（字符串列表）保持原类型；新信息走 `coverage_info`、`conversation_summaries`。
  `/config.autonomy` 保留，新信息走 `authority`。三者在 `/openapi.json` 标 `deprecated: true`，前端迁移完成后再移除。
- 导航白名单按 2.0 表新增 `scope`、`account`、`conversation`、`change`、`task_type` 参数；`ticker` 单独出现时不启动研究，先要求选择范围。
  旧 `research.html?run=` 深链继续可用。
- 默认值只用于普通入口：Portfolio 默认 `agentic`，Assistant 默认 `personal`，Research 从 More 进入默认 `agentic`；已打开的报告、持仓深链、
  现有会话的范围不被默认值覆盖；晚到的异步响应按其 `run_key` 所属范围落位。
- 审批请求的 `authority_version` 在前端上线前可选，上线后必填；旧提案无版本按 2.2 表处理，不因缺版本拒绝，只要求确认，且确认必须带版本。
- **同步交付**：`main` 上的后端不等于线上（release 不可变，当前线上仍是 `20260918T202756Z-f4e25bf`）。含授权检查的后端与前端最小审批适配
  （每次审批带 `/config/authority.version`；收到 409 的确认要求时展示 `authority_changed` 并以 `acknowledge_authority_change: true` + 同一
  `authority_version` 重发）在同一个 release 发布；单独发布后端会让旧提案在旧控台上无法批准。
- 数据结构扩展经 `state_db` 迁移与 `ct-state` 完成，不手改生产账本；`decision_record.json` 新字段只写新记录，旧记录读取时推断并标记。

---

## 4. 前端答复（已定）

| 问题 | 决定 | 落到契约 |
| --- | --- | --- |
| 1 `GET /accounts` 是否需要 | 保留 | 2.1，服务端统一账户标识、范围和可读状态 |
| 2 `link` 形态 | `{page, params}` | 2.0 白名单表；前端生成站内链接 |
| 3 文案本地化 | 枚举标签本地化，API 原文保留 | 2.0 通用规则；`kind`、`blocked_by.code`、`actions`、`recommendation`、`account_scope_kind` 进 `i18n.js` |
| 4 轻量权限接口 | 不需要 | 2.0：完整对象约 3 KB，两个首页轮询接口不开券商会话 |
| 5 反证摘要 | 保留 600 字，加截断标记 | 2.4 `counter_evidence_truncated`；完整内容经 trace 读取 |
| 6 `recommendation` 四值 | 足够 | 2.4：方向写在 `rationale`，值只表达建议 |

---

## 5. 后端实施顺序（下一轮，P2a 的接口部分）

1. `service/shapes.py`：`AccountScopes`、`Authority`、`Attention`、`ResearchJob`、`ResearchRecord`、`Evaluation` 模型；
   `tests/test_frontend.py` 的夹具同步到 `ui/assets/data.js`。
   **已完成（后端部分）**：`5c41382`（直接提交在 `main`）。七个模型全部 `extra="forbid"`，先建退化形状；`/evaluation` 返回
   `Evaluation | RouteQuality`；`GET /research/{run_key}` 仍为 dict（带 `log_tail`／`progress`／`assistant_result`）。
   `tests/test_api.py` 在沙箱里把每个新读接口都走一遍模型校验。**未做**：`DEMO_SHAPES` 里没有登记新模型，因为 `ui/assets/data.js`
   还没有这些接口的样例；前端补样例后，把 `attention`／`authority`／`accounts`／`researchJobs`／`evaluation` 加进
   `tests/test_frontend.py::DEMO_SHAPES`（模型可从 `agentic_trading.service.shapes` 导入）。隔离工作树全套 851 项通过。
2. `autonomy.effective_authority()` + `configuration.effective_authority()`，含 `actions[X].autonomous` / `.human_approval` 两侧阻断、
   `history` 与 `authority_versions` 记录；`autonomous_reductions` 改读它并加版本相等要求；`ApproveRequest` 加 `authority_version`、
   `acknowledge_authority_change`；`new_proposal` 记版本。交叉测试：PF04 × `autonomy.open` × `permits` 的八种组合，加"版本变化四种分类 × 有无确认"。
   **已完成**：`78fec51`（已合入 `main`）。`GET /config/authority` 与 `GET /config.authority` 已可用，返回 dict（严格模型留到第 1 步）；
   `authority_version` 在审批请求里目前可选；研究任务与提案已记版本；`deploy/checks.sh` 813 项通过，其中 26 项为新测试
   （`tests/test_authority.py`）。前端审阅后的修正 `d9655fc`（分支 `claude/amazing-moser-c49a21`，待快进合入）：提案记研究所依据的版本，
   研究期间变化转人工并记两版本；确认必须带 `authority_version`。已读写接口 `POST /research/{run_key}/opened` 在第 3 步实现。
3. `notifications.collect_conditions()` 拆出，`attention()` 与 `collect_notifications()` 各取；每种 `kind` 的出现／退出规则各一条测试；
   已读经 `POST /research/{run_key}/opened` 写入。
   **已完成**：`017fe0c`（分支 `claude/amazing-moser-c49a21`，待快进合入）。`GET /attention` 与 `POST /research/{run_key}/opened` 已可用；
   通知的 key／kind／title／link 全部不变，投递去重不受影响。实现与 2.3 表的差别：`scope` 多一个 `demo`（Demo 桶的提案与账本）；
   `risk_level_changed`、`autonomy_acted`、`config_change_applied` 只通知不列待办；`personal_change_found` 未实现（P1 后）。
   `deploy/checks.sh` 828 项通过，其中 14 项为新测试（`tests/test_attention.py`）。
4. 账户范围：`start_advisor` 校验、`account_scope_kind`／`account_ids` 写入、会话绑定；`advisor.run_task` 按装载规则表装上下文；
   `start_research` 的 `scope`／`request_id`／`purpose`；记录写入范围、`route`、`authority_version`。隔离测试：单账户上下文不含
   `all_personal` 与 `unknown` 记录。
   **已完成**：`7464953`（直接提交在 `main`）。与 2.1 装载规则表的一处差别：单账户会话的模型上下文**包含**覆盖它的 `all_personal`
   记录（带账户标签，作为该账户曾被复查的事实），`unknown` 记录仍不装入；Agentic 会话上下文含 `authority`（版本、动作、待生效变更、
   一致性）与最近 20 条提案，不含任何个人快照。`portfolio_review` 结果形状与 2.4 一致，另带 `run_key`；`GET /accounts` 不列 Demo。
   会话绑定存 `advisor.conversations`；旧会话视为 personal／unknown，下一条个人消息确定其种类。隔离工作树全套 850 项通过
   （10 项新测试，`tests/test_account_scope.py`）。
5. `research_history` / `research_jobs` 过滤与新字段；覆盖键计算；`prices_as_of`／`sources_retrieved_at_max` 写入，`facts_as_of` 恒为
   `null` 直到证据升级；`counter_evidence_truncated`。
   **已完成**：`cd95a34`（直接提交在 `main`）。记录与任务带 2.4 表全部字段；`route` 多一个 `inferred`（旧记录未写路线时按代码史推断：
   交易桶与个人 `analysis` 走 TradingAgents 图，其余个人任务走助手单次调用）；`POST /research` 的 `scope`／`request_id`／`purpose`
   全部生效，持有校验读账本不读 venue；`applicable_floor` 为该范围开小仓的置信度地板，个人为 `null`，地板不可达（>100）也为 `null`。
   `portfolio_review` 的记录形状留待第 4 步产生第一条记录时验证。隔离工作树全套 840 项通过（7 项新测试，`tests/test_research_records.py`）。
6. `evaluation_report(scope)`：按桶过滤决策与成交；`NOT_STARTED` 形状；`route_quality` 汇总；`ct-evaluate` 加 `--scope`。
   **已完成**：`4dd65e3`（直接提交在 `main`）。`GET /evaluation?scope=demo|agentic|route_quality`；`demo` 沿用原有 study 的存储键，
   `agentic` 独立 study；无 bucket 的旧行归 `demo`，`advisory` 行任何范围都不计；`sample` 按交易所日历算缺口与落后会话数；
   `route_quality` 的路线在记录带 `route` 字段前按 `advisory`／task 归为 `assistant`，其余归 `tradingagents`；维护任务对每个运行中的
   study 各打一次快照。与 2.5 的差别：`sample` 多 `basis`、`sessions_behind`；`route_quality` 里 `format_pass_rate_pct` 目前恒为 `null`
   （助手结构化输出的解析失败没有单独记录）。隔离工作树全套 833 项通过（含 5 项新测试）。
7. 把 `6d9f39e`（离线套件修复，分支 `claude/amazing-moser-c49a21`）快进合入 `main` 后再跑全套；不放宽 guard。
8. `deploy/checks.sh` 通过后发布；不改 `config/`，不开任何自主开关。

以上均不改前端；前端可按第 2 节的形状先做只读展示，依赖后端的部分随对应接口验收后上线。

**验收边界**：接口部分完成不等于 P2a 完成。按账户调度（`ct-trigger --bucket micro_live` 与 `schedule` 的按账户去重）、
完整 SELL 语义（可卖数量、部分成交、撤单与重启恢复）、退出优先的研究预算，各自单独验收；本稿不为它们定义接口。
