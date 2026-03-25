# 上游 TradingAgents 迁移（`9dee508` → v0.6.0）：设计契约（第一稿）

日期：2026-10-15。状态：**第一稿，没有写任何代码。U1–U9 已由你在 2026-10-15 定下（§11）：U1–U6、U8、U9 按本稿的建议；
U7 选的是“A 跑稳 3–5 个交易日后再发布”，不是本稿原先建议的“在 A 之前”（§7）。按本稿实施，上线时机按 U7。**
本稿不改 `config/`、`state/`、`runs/`、`.env`，不改前端，不部署。D8（2026-10-15）把上游升级移出基准建仓（A）
那一批，要求“先写它自己的契约、先在隔离环境里验证”，本稿就是这份契约。

本稿的来源：

- D8，以及基准建仓契约 §2.6（分支 `claude/amazing-raman-8f2b23`，`768f945`）。
- 上游只读核对（2026-10-15）：裸克隆在临时目录，只用 `git show`／`git diff`／`git log` 读，**没有导入、安装或运行任何上游代码**。
  版本：`9dee508`（v0.4.1 的合并，09-01；包里的版本号仍是 0.4.0）→ v0.5.0 `2d17df8`（09-18）→ v0.5.1 `35543d0`（09-24）
  → v0.5.2 `5eb5085`（09-29）→ v0.6.0 `1394a3f`（10-03）。今天上游 `main` 仍是 `1394a3f`。中间 204 个提交、194 个文件（+14405／−5589）。
- 上游 CHANGELOG 里 0.5.1、0.5.2、0.6.0 的 “Upgrading from” 三段。0.5.0 那一版没有这一段，但它本身就有不兼容的改动（§2）。
- 我们的代码：写稿时是 `main` 的 `41e0902`；本分支已变基到 `main` 的 `f41d076`（多出的 `c11c0c8` 让市场状态的读取失败后先重问两次，
  `f41d076` 只改测试），行号都按 `f41d076`；上游的行号都指 v0.6.0。
- 生产环境（只读）：当前发布 `20261015T175728Z-f41d076` 的虚拟环境里实际装的版本（和写稿时的 `20261007T230510Z-1223f50` 一样，
  `uv.lock` 没变）。
- 10-14 的两份只读分析（“研究为什么从不给出能下单的 BUY”、自动交易审查）。

---

## 0. 结论先说

1. **这是一次接口迁移，不是改几行导入。** 我们碰到上游的地方有 10 处只是换导入路径，另有十几处要设计（§3）。
2. **直接升级不会悄悄走 API，会大声失败。** 我们替换模型入口的那一行（`graph.py:144`）在上游模块里已经找不到
   `create_llm_client` 这个名字，构造研究图时就抛 `AttributeError`；行情、风控、模拟成交那六处读取也会因为导入路径全变而失败——
   有的按“读不到”处理，有的直接报错，没有一处会悄悄给出错的数。真正危险的是下面五处**不报错、悄悄变了**的地方：
   - **工具服务器**（§4.2）：v0.6.0 的数据工具从“运行状态”里取代码和分析日期；我们的工具不经过 LangGraph，照搬的话代码会变成
     模型可以随便填的参数，“不得读到分析日之后”的上限也会失效；而个人助手又恰恰需要自己填代码。
   - **账户快照自相矛盾**（§4.4）：我们不传上游新的 `portfolio` 参数，上游就会对交易员、风险辩论、组合经理说“没有提供持仓、你不知道账户”，
     同时他们又读到我们塞进去的账户快照。
   - **Form 4 和网页抓取悄悄没人拿到**（§4.8）：基本面分析师多了第 5 个工具，我们按“工具集合完全相同”认分析师，认不出来；
     守这个映射的测试只查“名字还在”，查不出“多了一个”，所以是绿的。Form 4 你 10-07 打开了。
   - **研究日期早于“本机今天”时，上游会扣下数据**（§4.6）：公司概况、财报、内部人交易、预测市场一律不给。
     周五 13:30 的定时研究不受影响；盘中或周末手动发起的、补跑的、节假日的研究会受影响。
   - **工作台不亮**（§4.9）：分析师现在在各自的子图里跑，进度事件里的节点名变成 `agent`。
3. **对之前说法的三处更正：**
   - **pandas**：生产早已是 pandas 3.0.5（`uv.lock` 至少从 09-17 的 `205025f` 起；当前发布的虚拟环境里装的也是 3.0.5）。
     上游要 ≥3.0.6，是补丁级升级。基准契约 §2.6 说“我们代码在 pandas 3 下没有验证过”不对——10-07 Vibe-Trading 冲突，
     正是因为它要 pandas<3，而我们已经是 3。
   - **模型入口**：见第 2 条，不是“悄悄不再生效”，是构造时报错。能悄悄绕过订阅的是两条新路：v0.6.0 允许用环境变量给每一层
     指定模型服务和地址（`TRADINGAGENTS_*_PROVIDER`、`*_BACKEND_URL`），以及 v0.5.1 起的 TypeSafe Jev
     （环境里有 `TYPESAFE_API_KEY`，就把社交帖子发给外部模型服务）。§4.1 把两条都堵上，并用测试证明。
   - **SEC EDGAR**：上游默认改成财报先读 SEC，但我们的配置写死了 `fundamental_data: yfinance`
     （`config/investor_profile.yaml:194`），上游的路由“配置写了哪家就只用哪家”，所以今天升级不会碰 SEC。
     风险在于这一行没人守：设置页能改它（`config_changes.py:267-280` 的受保护列表里没有它），一旦改成含 `sec_edgar`，
     上游会用占位联系人 `contact@example.com` 去请求 SEC。§4.5。
4. **并发**：上游的分析师并行没有开关可关。我们在自己的适配器里给每次研究加一个同时调用的上限，定为 1（U2），效果等于今天的串行（§4.3）。
5. **时机**：升级必然让模拟天数从 0 重数（策略版本里有上游提交号，`auto/store.py:164`）。
   单看清零，最便宜的是在 A 之前单独发布（A 那次本来就要清零，这次被它吸收）；最贵的是进实盘之后——实盘方案会被撤下，
   新版本攒满 30 个交易日之前不能开新仓。**你定的是 A 发布、跑稳 3–5 个交易日后再发布**（U7）：多清零 3–5 天，换来 A 早开始、
   A 的接口先经前端审阅、两次大改动不紧挨着发布（§7）。

---

## 1. 今天的实际行为（已核实，带位置）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 上游固定在 `9dee508`，三处各写一遍：依赖源、锁文件、常量。没有测试检查三处一致，也没有检查和实际安装的是否一致 | `pyproject.toml:53`，`uv.lock:41`，`agentic_trading/__init__.py:15` |
| 2 | 策略版本号包含这个常量（不是实际安装的提交）；常量一变，模拟天数从 0 数 | `auto/store.py:155-172`，`:175-179` |
| 3 | 每次模型调用都经 `claude -p`：构造研究图时，临时把上游 `trading_graph` 模块里的 `create_llm_client` 换成我们的工厂；深浅两层靠模型名猜 | `graph.py:63-82`，`:144`；猜层级 `:64` |
| 4 | 分析师的工具循环在 CLI 里跑：工具由我们的 MCP 工具服务器按名字取上游函数（`tool.func`）提供，代码和日期由模型填 | `llm/ta_tool_server.py:63-85` |
| 5 | 个人助手也用这个工具服务器，四个上游工具，代码由模型按对话填 | `advisor.py:280-287` |
| 6 | 委托书、“不可信内容”提示和账户快照都塞进上游的 `resolve_instrument_context`，所有角色都看得到 | `graph.py:174-187` |
| 7 | 网页抓取和 Form 4 只交给“工具集合完全相同”的那个分析师；守这个映射的测试只检查名字还在源码里 | `llm/analyst_roles.py:22-28`；`tests/test_web_access.py:266-280` |
| 8 | 工作台亮灯靠进度事件里 LangGraph 的 `langgraph_node` 认角色 | `llm/claude_code.py:598-601`，`agent_roster.py:64-72` |
| 9 | 上游的日线读取 `load_ohlcv` 被 6 处确定性代码直接用：估值价、漂移参考价、模拟成交、回测取数、交易日探测、市场状态；全部按位置传参 | `portfolio.py:218-226`，`cli.py:379`，`broker/simulated.py:223`，`auto/bars.py:50`，`service/base.py:250`，`risk/regime.py:55` |
| 10 | 研究一次一个进程、一个任务，最多 2 个进程同时跑；今天一次研究里的模型调用是一个接一个的 | `research_jobs.py:156`、`:194-196`；`config/risk_profile.yaml:578` |
| 11 | 断点续跑开着：被订阅上限打断的研究过几小时重排，从断点接着跑 | `config/investor_profile.yaml:199`；`cli.py:224-228`；`research_jobs.py:159-160` |
| 12 | 上游的决策记忆（5 天后反思）关着 | `config/investor_profile.yaml:198`；`profile.py:376-377` |
| 13 | 财报类数据只读 Yahoo | `config/investor_profile.yaml:194` |
| 14 | 定时研究在周五 13:30（本机时间，PDT＝美东 16:30，收盘后），分析日期就是当天；盘中、周末手动发起的研究和补跑，分析日期是上一个交易日 | `config/risk_profile.yaml:590-597`；`schedule.py:357-388`；`service/base.py:262-281` |
| 15 | 生产装的是 pandas 3.0.5、yfinance 1.7.0、langchain-core 1.6.1、langgraph 1.2.11 | 当前发布 `20261015T175728Z-f41d076` 的 `.venv` |
| 16 | 研究室手动研究的默认日期是美东的“今天” | `cli.py:79-80` |
| 17 | Reddit 来源默认关，经我们的包装读上游的 `dataflows.reddit` | `sources/reddit.py:77-93`、`:336-374` |

---

## 2. 上游从 `9dee508` 到 v0.6.0 变了什么（只列碰到我们的）

| 版本 | 变化 | 碰到我们哪里 |
|---|---|---|
| 0.5.0（09-18） | 带日期的工具从运行状态取分析日期，模型给的日期不能超过它 | §4.2 |
| | 研究日期早于今天时，不给只有“今天”版本的数据（先是公司概况） | §4.6 |
| | `propagate` 多一个可选的 `portfolio`，不传时告诉决策角色“没有提供持仓”；被反驳的评级不再被读成决定 | §4.4，§4.7 |
| | 日线缓存改成每个代码一个文件；SEC EDGAR 财报（可选） | §4.10，§4.5 |
| 0.5.1（09-24） | 包结构重排，**旧导入路径全部删除**；我们用到的模块全在内 | §3 |
| | 基本面分析师多了内部人交易工具 | §4.8 |
| | TypeSafe Jev：环境里有 `TYPESAFE_API_KEY` 就把社交帖子发给外部模型服务 | §4.1 |
| 0.5.2（09-29） | **分析师并行**，没有串行的开关 | §4.3 |
| | 数据工具不再接收代码参数，从运行状态取；数据函数 `curr_date=` → `as_of_date=` | §4.2，§4.10 |
| | `resolve_instrument_context` 多一个 `trade_date` 参数 | §3 |
| | `process_signal` 删除；评级只从 `Rating:` 标签读 | §4.7 |
| | 财报默认先读 SEC；研究日期早于今天时，Yahoo 财报、内部人交易也不给 | §4.5，§4.6 |
| | 要求 Python≥3.11、pandas≥3.0.6 和若干依赖的最低版本；旧断点不再接续 | §4.12，§4.11 |
| 0.6.0（10-03） | `create_llm_client` → `create_tier_client`，每一层可以有自己的模型服务（也能用环境变量指定） | §4.1 |
| | 保存报告默认多写一个 HTML 页 | §4.11 |
| | 研究进行时同时结算**所有**代码到期的历史决策（记忆开着才有） | §4.3 |
| | **个股新闻为空时改读 Yahoo 搜索**（`4429078`）——这次升级要的东西 | §5 |
| | 引用别人的评级（“Consensus rating: Buy”）不再当作组合经理的决定 | §5 |

---

## 3. 接触点清单

`grep -rn "tradingagents" agentic_trading` 共 56 处。去掉只是名字的（路由名 `"tradingagents"`、配置段名、说明文字），
真正碰到上游代码的是下面这些。

**A 组：只换导入路径（10 处）**

| 位置 | 现在 | v0.6.0 |
|---|---|---|
| `graph.py:92`，`portfolio.py:230` | `agents.utils.agent_utils.resolve_instrument_identity` | `agents.context.resolve_instrument_identity`（同名同签名；失败不再被缓存，§5 #12） |
| `portfolio.py:237` | `dataflows.symbol_utils.normalize_symbol` | `dataflows.symbols.normalize_symbol` |
| `portfolio.py:220`，`cli.py:379`，`broker/simulated.py:223`，`auto/bars.py:50`，`service/base.py:250`，`risk/regime.py:55` | `dataflows.stockstats_utils.load_ohlcv` | `dataflows.vendors.yahoo.ohlcv.load_ohlcv`（参数名改了，我们按位置传，不受影响；行为变化见 §4.10） |
| `sources/reddit.py:78`（及 `:65` 的说明） | `dataflows.reddit` | `dataflows.vendors.reddit` |

**B 组：要设计的**

| 位置 | 问题 | 见 |
|---|---|---|
| `graph.py:63-82`、`:144` | 模型入口换名、换签名 | §4.1 |
| `graph.py:147-168` `propagate` | 要放运行范围、传 `portfolio` | §4.2，§4.4 |
| `graph.py:174-177` `resolve_instrument_context` | 上游用三个位置参数调它（`trading_graph.py:304`），我们的签名只有两个 → `TypeError`；加上 `trade_date` 原样转交 | — |
| `llm/ta_tool_server.py:63-85` | 工具参数改由运行状态注入 | §4.2 |
| `llm/claude_code.py:421-446` `_mcp_args`，`:578-680` `_generate` | 运行范围、并发上限、进度节点名 | §4.2，§4.3，§4.9 |
| `llm/analyst_roles.py:22-28` | 基本面分析师 5 个工具 | §4.8 |
| `advisor.py:280-287`（助手工具），`:371-380`（只读研究） | 助手模式；报告 | §4.2，§4.11 |
| `cli.py:237`，`advisor.py:380` `save_reports` | HTML 页 | §4.11 |
| `cli.py:79-80` 默认日期 | 上游拒绝“晚于本机今天”的日期（`trading_graph.py:30-41`）；本机 21:00–24:00 不写日期手动发起的研究会被拒。改成在我们这里先拒并说明（CLAUDE.md：远端会拒的值先在这里拒），API 传来的日期同样处理 | §6 T21 |
| `service/base.py:232-259` `_session_has_data` | 交易日探测会把“缺当天这根”当成“有数据” | §4.10 |
| `profile.py:301-309`、`:359-381` | 配置护栏、来源链护栏 | §4.1，§4.5 |
| `config_changes.py:267-280` | 来源配置不受保护 | §4.5 |
| `__init__.py:15`，`pyproject.toml:53`，`uv.lock` | 固定版本 | §4.13 |

**C 组：不用改，但要知道**

- `graph.py:27-28` 的 `TradingAgentsGraph`、`BaseLLMClient`：路径和接口没变（`llm_clients/base_client.py` 未改）。
- `agent_roster.py` 的节点名没变；多了一个不调模型的 `Memory Log` 节点。
- `sources/reddit.py:86`、`:345`：情绪分析师仍按名字调用 `fetch_reddit_posts`，多传一个 `screen=`（Jev 不开时是 `None`），我们的包装原样转交。
- `policy.py:157-174`、`records.py:87-113` 读的状态键都还在；被删的 `judge_decision` 我们没用。
- 报告目录和文件名没变（`1_analysts/market.md` …）；研究页只读各阶段目录里的 `*.md`（`service/research.py:279-283`）。
- `records.py:93`、`service/research.py:586`、`sources/comparison.py:84` 记录上游提交号；比较工具按提交号分组，升级前后的研究不会混算。
- 我们没导入上游不再依赖的 backtrader、redis、parsel、tqdm、setuptools、langchain-experimental。

**测试（9 个文件）**：`test_graph_offline.py:5-6`、`test_claude_code_adapter.py:73`、`:82`、`test_web_access.py:270`、
`test_session_anchor.py:43`、`test_advisor.py:258`、`:285`、`test_sources_reddit.py:292-332`、`test_sources_form4.py:655-670`、
`test_cli_offline.py:34-37`、`:90-96`、`conftest.py:359`（`dataflows.config._config` 还在）。另外上游的默认配置是**导入时按环境变量构造的**
（`default_config.py:10-34`），`conftest.py` 要在所有测试前清掉 `TRADINGAGENTS_*` 和 `TYPESAFE_*`。

---

## 4. 设计（每项：问题 → 做法 → 测试）

### 4.1 模型调用：换注入点，并证明没有旁路

**事实。** v0.6.0 用 `create_tier_client(config, "deep"|"quick", **extra)` 建两层模型（`trading_graph.py:79-80`），
它再调 `factory.create_llm_client`（`factory.py:138-152`）。除了 `llm_clients` 包自己（它只能经 `factory` 进入），整个上游包里
能“造模型或直连模型服务”的只有三处：这两处，加上 `agents/post_screen.py:87`（TypeSafe，环境里有密钥才跑，`:139`）。
另外，上游一导入就会从**当前工作目录往上找** `.env` 读进环境（`tradingagents/__init__.py:12-13`，两个版本都这样）；
研究进程的工作目录就是数据目录。

**做法：**

1. 注入点换成 `trading_graph.create_tier_client`，仍是“作用域内替换一个名字、调用返回就放回”，不改上游文件。
   我们的函数收到 `tier` 就知道是深层还是浅层，不再按模型名猜（`graph.py:64` 在两层用同一个模型时会把深层认成浅层，今天就有，顺手修）。
2. 同一作用域里，把 `factory.create_llm_client` 换成“一调用就抛错、并说明原因”的绊线。作用域覆盖构造（`__init__`）和每次运行（`propagate`）：
   上游以后在运行中途另造模型，会当场失败，而不是去找 API 密钥。
3. 配置：`build_tradingagents_config`（`profile.py:359`）把 `deep_think_provider`、`quick_think_provider`、`deep_think_backend_url`、
   `quick_think_backend_url`、`backend_url` 一律置空，`llm_provider` 固定为 `claude_code`；`investor_profile.yaml` 的 `tradingagents`
   段里出现前五个键，加载配置就报错（和现在的计费护栏 `profile.py:301-309` 同一处、同一说法）。
4. 环境：下列变量任一存在，就拒绝构造研究图并说出变量名——`TYPESAFE_API_KEY`、`TYPESAFE_BASE_URL`、`TRADINGAGENTS_LLM_PROVIDER`、
   `TRADINGAGENTS_DEEP_THINK_PROVIDER`、`TRADINGAGENTS_QUICK_THINK_PROVIDER`、`TRADINGAGENTS_LLM_BACKEND_URL`、
   `TRADINGAGENTS_DEEP_THINK_BACKEND_URL`、`TRADINGAGENTS_QUICK_THINK_BACKEND_URL`。检查放在上游导入之后（它会读 `.env`）。
   `ANTHROPIC_API_KEY` 照旧在每次调用前从 CLI 的环境里删掉（`llm/claude_code.py:70-80`），不变。
5. 工具服务器是另一个进程，也走第 3、4 条（它同样调用 `build_tradingagents_config`）。

**测试（证明没有一次模型调用绕过 CLI 适配器）：**

- **T1 构造**：两层都是 `ClaudeCodeChatModel`，`role` 分别是 deep／quick，effort 分别来自 `llm.deep_effort`／`llm.quick_effort`。
- **T2 端到端绊线**：用现有的假 CLI（`tests/fake_claude.py`）各跑一次 ETF（2 个分析师）、个股（4 个）、加密（3 个）。整个运行期间，
  上游的 `AnthropicClient`、`OpenAIClient`、`GoogleClient`、`AzureOpenAIClient`、`BedrockClient`，LangChain 的 `ChatAnthropic`、
  `ChatOpenAI`、`ChatGoogleGenerativeAI` 的构造，`factory.create_llm_client`、`post_screen.system_one`、`sec_edgar._fetch_json`
  都换成“一碰就记一笔并失败”。断言：运行完成；绊线一次没碰；假 CLI 被调用的次数 = `USAGE_LOG` 条数 = 本次 `llm_calls.jsonl` 新增行数
  = `progress.jsonl` 里 `call_finished` 的个数。再跑一次“结构化输出失败、回落到自由文本”（`structured.py:59-81`），同样的断言成立。
- **T3 静态扫描**（同 `tests/test_money_paths.py` 扫全包的做法）：扫描已安装上游包里 `llm_clients` 之外的源码，找出
  `create_llm_client(`、`create_tier_client(`、`Chat[A-Z]\w*(`、`init_chat_model(`、`requests.post(` 的所有位置，和测试里写死的清单逐条比：
  v0.6.0 上恰好是 `trading_graph.py` 两处、`factory.py` 一处、`post_screen.py` 一处。下次升级多出任何一处，测试就红，必须先在契约里写明再放行。
- **T4 护栏**：第 3、4 条里每个键、每个变量各一条用例，各自被拒、消息里有名字；工具服务器进程同样被拒。

### 4.2 工具服务器：运行范围注入，研究和助手两种模式

**问题。** v0.6.0 的数据工具把代码参数和分析日期参数标成 LangGraph 的 `InjectedState`（`agents/tools.py:20` 等），只有在 LangGraph 的
工具节点里执行时才由运行状态填。我们的工具不在 LangGraph 里执行：CLI 经 MCP 调我们的工具服务器，服务器直接调 `tool.func`
（`ta_tool_server.py:78-84`）。照搬的结果：

- 代码参数出现在给模型看的参数表里，由模型填——上游 0.5.2 专门修掉的“模型换个代码就能读别的股票”，在我们这里又回来了；
- 分析日期取默认值 `""`，`as_of()`（`date_window.py:78-89`）看到空日期就放行，“不得读到分析日之后”的上限失效；
- 个人助手（`advisor.py:286`）本来就要按对话里的代码查，不能从“运行状态”取。

**做法：**

- **运行范围**：`AgenticTradingGraph.propagate` 把本次的代码和分析日期放进一个只在本次运行内有效的上下文变量（`contextvars`，和上游
  `run_config` 同一机制；LangGraph 给每个并行分支复制上下文，四个分析师线程都看得到）。`_mcp_args`（`claude_code.py:421`）读它，
  作为两个环境变量交给工具服务器，和现在交 `SOURCES_AS_OF_ENV` 的方式相同（`:427-436`）。运行结束就清掉。
- **研究模式**（有运行范围）：给模型看的参数表取自 `tool.tool_call_schema`（LangChain 已经去掉被注入参数的那一份）；调用时按上游的参数名
  （`symbol`／`ticker`、`trade_date`）把代码和分析日期补上。模型看不到、也填不了代码。
- **助手模式**（没有运行范围）：代码参数照旧由模型填，分析日期留空（＝今天，不扣数据），和今天的助手一样。
- 我们自己的两个工具（`fetch_url`、`get_sec_form4`）不变；未知工具名照旧拒绝启动（`:80-81`）。

**测试：**

- **T5** 研究模式：每个上游工具的参数表里没有 `symbol`、`ticker`、`trade_date`；服务到的是运行的代码；模型给一个晚于分析日的日期，返回的数据不晚于分析日。
- **T6** 助手模式：参数表里有代码；助手的四个工具各调一次。
- **T7** 同一进程先跑研究、再跑助手：研究结束后范围已清，助手不会被锁在研究的代码上。

### 4.3 并发：给每次研究一个同时调用的上限

**问题。** 上游把每个分析师都直接接在起点上（`setup.py:157-166`），没有串行的开关。今天一次个股研究的 4 个分析师一个接一个调 `claude -p`；
升级后会同时调 4 个（ETF 2 个），每个带工具的分析师还各起一个工具服务器进程（每个都导入 pandas、yfinance）。两个研究进程同时跑时，
最多 8 个 `claude -p` 同时用你的订阅。订阅和生产研究、你自己的使用共用，以前大批并发碰到过会话上限；碰到上限，整次研究停下、
几小时后重排（`cli.py:224-228`，`research_jobs.py:159-160`）。

LangGraph 有一个 `max_concurrency` 设置，但同步执行时它怎么算（当前线程算不算一个）不是写明的约定，不靠它。

**做法**：上限放在我们自己的适配器里——

- 每张研究图一个信号量，深浅两层共用；`claude -p` 子进程拿到许可才启动；
- 上限从配置 `llm.max_parallel_calls_per_run` 读，缺省 1；代码里另有硬上限 4（和 `max_turns_ceiling` 同一做法：配置能写，代码能压）。
  缺省就是 1，所以迁移本身不需要你改配置；
- 等许可的时间不算进单次调用的超时（超时只管子进程），单独记成 `queued_seconds`；`wall_seconds` 仍只算调用本身，和以前的记录可比；
- 进度事件 `call_started` 拿到许可之后才写，工作台不会出现“四张桌子都在忙、其实三张在排队”；
- 只管研究图里的调用；个人助手不受影响。

上限 1 时，一次研究的模型调用和今天一样一个接一个，耗时相近；上限 2 时分析师阶段大约快一半，峰值翻倍。→ U2：定为 1（已定）。

**记忆步骤**（`trading_graph.py:308-335`）和分析师同时跑，会为每一条到期的历史决策调一次模型写反思。我们的记忆是关的
（`memory_log_path: null`），这一步读不到任何条目、不调模型（`memory/log.py:20-25`，`memory/settlement.py:158-168`）。

**测试：**

- **T8** 我们的配置下，记忆步骤不产生任何模型调用、不读行情。
- **T9** 假 CLI 记下每次调用的开始和结束时间：个股研究在上限 1 时任何时刻最多 1 个调用在跑，上限 2 时最多 2 个；配置写 10 也只到 4；
  `call_started` 个数 = `call_finished` 个数。

### 4.4 账户快照：填上游的 `portfolio` 入口

**问题。** v0.5.0 起 `propagate` 多了可选的 `portfolio`；不传时，交易员、三个风险辩论者和组合经理的提示里会多一段，大意是
“没有提供持仓，你不知道调用方的持仓和现金，不要假设是空仓”（`context.py:204-216`）。我们不传——我们的账户快照放在 instrument context 里
（`graph.py:174-187`）。升级后这几个角色会同时读到“这是你的账户”和“你不知道账户”，而组合经理的输出正是我们政策步骤的主要输入。

上游对 `portfolio` 只用两个方法：`render(ticker)` 和 `fingerprint()`（`trading_graph.py:158`、`:305`）。我们传一个自己的小对象就行，
不用改上游，也不用套上游的 `PortfolioContext` 结构。

**两种填法（U3：甲，已定）：**

- **甲（采用）**：`render` 只返回一句“调用方的持仓、现金、所属账户和执行阶段见上文 PORTFOLIO CONTEXT 一段”。这三个角色的提示里，
  上游都把这一句排在 instrument context 后面（例如 `aggressive_debator.py:38-39`、`trader.py:68-70`），所以“上文”成立。
  快照仍在原处，所有角色看到的和今天一样，矛盾的那段不再出现。
- **乙**：快照移进 `render`，只给交易员、风险辩论者、组合经理看；分析师和多空研究员不再看到持仓（上游的设计：研究团队不看调用方的持仓）。
  这会改变研究行为（10-14 的分析发现市场分析师的报告 19 次里有 18 次写了 HOLD/NO TRADE），应该作为一次单独评估的研究改动，不夹在迁移里。

两种都让 `fingerprint()` = 快照文字的摘要：上游把它写进断点签名（`trading_graph.py:158`），账户变了之后的重跑不会接上旧断点。

**测试**：**T10** 三个决策角色收到的提示里没有 `Portfolio context: not provided`（从假 CLI 收到的提示里查）；快照一变，`fingerprint` 跟着变。

### 4.5 SEC EDGAR 与来源规则

**事实：**

- 0.5.2 起上游的 `fundamental_data` 默认是 `sec_edgar,yfinance`（`default_config.py:163`）。SEC 请求用 `SEC_EDGAR_USER_AGENT`
  说明身份，没有就用占位的 `TradingAgents/<版本> (contact@example.com)`（`vendors/sec_edgar.py:92-102`）。
- 上游的路由只用配置里写了的那几家，不会悄悄换（`router.py:211-225`）。我们写的是 `yfinance`，SEC 只会在配置改了以后才被读到。
- SEC 只服务三张财报（`router.py:113-127`）；公司概况、内部人交易仍然只有 Yahoo。
- 我们的 Form 4 来源有自己的联系人（`CT_SEC_CONTACT`，在 `.env`）、登记（`declared_data_sources`）和逐次读取记录（P2.6 的规则：
  每次读取以一个有名字的结果结束并记下来）。上游的 SEC 读取这三样都没有。

**做法（U4：甲，已定）：**

- **甲（采用）**：财报继续只读 Yahoo。`build_tradingagents_config` 发现任何来源链（`data_vendors`、`tool_vendors`）里有 `sec_edgar`，
  就拒绝启动研究，并说明这是 U4 的决定；`config_changes.py:267-280` 的受保护列表加上 `tradingagents.data_vendors`、
  `tradingagents.tool_vendors` 和 §4.1 的五个模型服务键，设置页对它们的改动按“放宽”处理（等一天）。
- **乙（以后单独做）**：开 SEC 财报。要做三件事：只在工具服务器进程里把 `CT_SEC_CONTACT`（按 Form 4 的同一条格式校验）映射成
  `SEC_EDGAR_USER_AGENT`；在 `declared_data_sources` 里登记；把上游的 SEC 请求像 Reddit 那样包一层，按 P2.6 记录每次读取。
  它的好处在 §4.6：研究日期早于今天时，只有它还能给出个股财报。

**测试**：**T11** 含 `sec_edgar` 的配置被拒；离线端到端运行里 `sec_edgar._fetch_json` 一次都没被调（T2 的绊线）。

### 4.6 研究日期早于“本机今天”时，上游会扣下数据

**事实。** 0.5.0／0.5.2 起，分析日期早于本机的今天（`date.today()`，`date_window.py:34-41`）就算“历史研究”，下面这些一律不给，
只给一段说明（`date_window.py:108-170`）：

- 公司概况（`get_fundamentals`）；
- Yahoo 的三张财报；
- Yahoo 的内部人交易；
- Polymarket 预测市场（`vendors/polymarket.py:86`）——**ETF 研究也受影响**；
- 所有角色读到的身份信息只剩“当前名称”，不给行业、交易所（`context.py:138-142`）。

**我们什么时候会碰到：**

- 周五 13:30 的定时研究：不会（收盘后，分析日期＝当天）；
- 盘中或周末在研究室手动发起的、补跑的、节假日（`previous_session`）的研究：会，分析日期是上一个交易日；
- 被订阅上限打断、隔天才重跑的研究：会，而且同一个任务前后两次读到的数据不同。

**对 HR05d 的影响**：这些说明会让政策步骤列出更多“缺失信息”，而 HR05d 只要缺失信息不为空就拒绝开仓——这些情况下的个股研究实际上
给不出能下单的 BUY。

**做法（U5：甲，已定）：**

- **甲（采用）**：接受上游的规则，不改定时；研究记录写明这次是不是“历史研究”、哪些类别被扣下（按同一条日期规则和本次跑了哪些分析师算，
  不去解析模型看到的文字），研究页照原样显示：

  ```json
  "point_in_time": {"historical": true, "machine_date": "2026-10-17", "trade_date": "2026-10-16",
                    "withheld": ["company_profile", "statements", "insider_transactions", "prediction_markets"],
                    "text": {"zh": "分析日期早于今天，上游没有给：公司概况、财报、内部人交易、预测市场。",
                             "en": "Dated before today: upstream withheld the company profile, statements, insider trades and prediction markets."}}
  ```

  不是历史研究时为 `null`。只写进 `decision_record.json`（读的时候都用 `.get`），回滚安全（§8）。
- **乙**：同时做 U4 乙，个股财报在历史研究里也有（概况、内部人交易、预测市场仍然没有）。

**测试**：**T12** 分析日期早于今天的离线个股研究，记录里 `withheld` 恰好是这四类；ETF 研究只有 `prediction_markets`；当天的研究是 `null`。

### 4.7 评级与 REVIEW

**事实。** 现在的评级是组合经理结构化输出里的 `rating`（`portfolio_manager.py:80-86`）；结构化失败时从自由文本里读，只认 `Rating:` 标签，
第一行的标签优先，几处标签互相矛盾就算读不出（`rating.py:55-93`）；读不出是 `REVIEW`（`:96-103`）。`9dee508` 读不到标签时会拿全文里
**第一个**评级词——“这不是 Sell”会被读成 Sell。

**我们**：`pm_signal` 直接用 `propagate` 的第二个返回值（`cli.py:222`，`advisor.py:379`）；`REVIEW` 不在 `RATING_TO_ACTION` 里，
政策步骤记 `rating_not_tradeable`、NO_TRADE（`policy.py:112-114`）。不用改。

**测试**：**T13** 结构化输出给 Hold、正文里引用 “Consensus rating: Buy” 时，评级是 Hold；结构化失败、自由文本没有标签时是 `REVIEW`，政策是 NO_TRADE。

### 4.8 分析师签名、Form 4 与网页抓取

**问题。** 0.5.1 起基本面分析师的工具从 4 个变成 5 个（多了 `get_insider_transactions`，`fundamentals_analyst.py:14-20`）。
我们的签名表（`analyst_roles.py:26-27`）是 4 个、要求完全相等 → 升级后基本面分析师认不出来，`fetch_url` 和 Form 4 不交给任何人；
守它的测试（`test_web_access.py:266-280`）只查那 4 个名字还在源码里，查不出多了一个，所以是绿的。

**做法**：签名表改成 5 个；测试改成直接和上游每个分析师模块的 `TOOLS` 元组（v0.6.0 起每个分析师模块都有，如 `market_analyst.py:8-12`）
比较集合是否相等。新增的 Yahoo 内部人交易和我们的 Form 4 是同一类信息的两个来源（前者按交易日期、后者按申报日期）；
→ U9：保留，你在 `declared_data_sources` 里登记（已定）。

**测试**：**T14** 三个分析师的签名和上游 `TOOLS` 完全相等；个股研究里 Form 4 只出现在基本面分析师的调用里，并在来源记录里留下这次读取。

### 4.9 进度事件：分析师在子图里

**问题。** v0.6.0 每个分析师是一张自己的小图，里面的节点叫 `agent`、`tools`、`wrap_up`（`setup.py:53-90`）。模型调用时 LangGraph 给的
`langgraph_node` 是 `agent`，不再是 `Market Analyst`；`role_for_node` 认不出 → 工作台上分析师的桌子不亮。现有测试
（`test_graph_offline.py:56`）会红，这是好事。

**做法**：节点名是 `agent`／`wrap_up` 时，取 LangGraph 元数据里命名空间（checkpoint namespace）的第一段，也就是外层节点名。

**测试**：**T15** ETF、个股、加密三种研究里，每个进度事件都有角色。

### 4.10 `load_ohlcv` 的六处调用

除了路径，v0.6.0 的 `load_ohlcv`（`vendors/yahoo/ohlcv.py:161-260`）和 `9dee508` 有四处不同：

| 不同 | `9dee508` | v0.6.0 | 对我们 |
|---|---|---|---|
| 最新一根没有收盘价 | 抛错 | 记一条警告，退到上一根有收盘价的（`:232-245`） | **交易日探测**（`service/base.py:232-259`）以前抛错就算“这天没数据”，现在会拿到一张只到前一天的表、算作“有数据”。改成比较最后一行的日期是不是要探测的那一天（这也顺带补上一个老缺口：Yahoo 缺当天整行时，探测今天也照样说“有数据”）。估值和市场状态本来就读最后一行的日期并检查新旧（`portfolio.py:408-412`，`risk/regime.py:94-105`），回测取数按每一行的日期取（`auto/bars.py:72-80`），不受影响。模拟成交（`broker/simulated.py:223-228`）盘前本来就拿前一天收盘价，只有“Yahoo 已放出当天一行但还没有收盘价”的那几分钟从“报不出价”变成“前一天收盘价”，可以接受、不改 |
| 下载方式 | `yf.download`（出错吞掉、返回空表） | `Ticker.history`＋重试（`:212`；限流先重试，最后抛 `VendorUnavailableError`） | 我们六处都按“任何异常都是读不到”处理，行为不变；市场状态的读取在 `c11c0c8` 之后会先对抛错的读取重问两次（`risk/regime.py:66-77`），v0.6.0 失败时抛错而不是返回空表，正好落进这条重问 |
| 缓存文件名 | 每个代码每天一个 | 每个代码一个，当天有效（`:146-158`、`:193`） | 旧文件留在 `~/.tradingagents/cache`，没有害处；第一次运行多下载一次 |
| 参数 | `curr_date` | `as_of_date`，多一个 `fill_gaps` | 我们按位置传参，不受影响 |

**测试**：**T16** 最新一根没有收盘价、或缺当天整行时，交易日探测都返回“没有数据”；其余五处用同一张样例表各走一遍。

### 4.11 断点续跑与 HTML 报告

**断点：**

- 上游的断点签名现在包含“分析师并行”“记忆并行”和全部设置的摘要（`trading_graph.py:140-165`）。`9dee508` 写的断点永远对不上，
  升级后一律从头跑；回滚方向同理。不会接错，也不用清理（旧记录留在 `~/.tradingagents/cache/checkpoints/<代码>.db`）。
- 代价：发布前被上限打断、发布后才重排的研究会从头再跑一次（多花一次研究的订阅额度）。上线前看一眼研究队列（§7）。
- 以后在同一版本内：设置改了（比如你在设置页改了一项），重排的研究也会从头跑——上游有意如此，不拿别的设置产出的报告接着跑。
- 同一步里一个分析师失败时，按 LangGraph 的设计，同一步其他已完成分析师的结果会被保存，续跑只重跑没完成的。这一点不靠假设，T17 验证。

**测试**：**T17** 旧格式的断点在场时新运行从头开始；假 CLI 在第 3 个分析师处报“订阅上限”，续跑只重跑没完成的那个。

**HTML（U6：不写，已定）**：`save_reports` 默认多写一个 `complete_report.html`（`reporting.py:92-120`；内容已转义，带 CSP，不加载任何东西）。
研究页只读各阶段目录里的 `*.md`，用不到它。建议我们两处调用（`cli.py:237`，`advisor.py:380`）传 `html=False`，`runs/` 和今天一样。
`complete_report.md` 的开头多了几行（评级、上游版本、模型、分析师、数据来源），研究页不读开头。**T18** 报告目录里没有 HTML，文件名与今天相同。

### 4.12 pandas 3 审计与依赖

**pandas：**

- 生产已是 pandas 3.0.5（§1 #15）。上游要 ≥3.0.6，补丁级。
- 我们直接用 pandas 的只有 7 个模块：`portfolio.py`、`risk/regime.py`、`risk/engine.py`、`service/base.py`、`service/system.py`、
  `broker/robinhood.py`、`micro_live.py`，外加 `auto/bars.py` 对上游返回的表做 `to_dict("records")`。用到的只有
  `Timestamp`／`Timedelta`／`to_datetime`／`bdate_range`、`iloc[-1]`、`stockstats.wrap`。没有链式赋值、`inplace`、旧的频率别名、
  按位置取 `Series[int]`、`object` 类型判断——pandas 3 的写时复制和默认字符串类型都碰不到，而且这些代码今天就在 3.0.5 上跑。
- 真正要验证的是上游**返回的表**：`Ticker.history` 给的日期带时区，上游在 `_normalize_dates` 里转成本地零点；我们都用
  `pd.to_datetime(...).date()` 或 `.date()` 取日期。**T19** 用一张带时区日期的样例表走一遍六处调用。

**依赖（锁文件会变）：**

| 包 | 现在 | v0.6.0 要求 |
|---|---|---|
| pandas | 3.0.5 | ≥3.0.6 |
| langchain-core | 1.6.1 | ≥1.6.5 |
| langchain-openai | 1.6.0 | ≥1.6.6 |
| langchain-anthropic | 1.7.1 | ≥1.7.4 |
| langgraph | 1.2.11 | ≥1.2.12 |
| langgraph-checkpoint-sqlite | 3.1.1 | ≥3.1.1 |
| yfinance | 1.7.0 | ≥1.7.0 |
| markdown-it-py | 4.2.0（间接） | ≥4.0（直接） |
| Python | 3.12 | ≥3.11 |

建议同时把我们自己的确定性代码直接用到的 pandas、yfinance、stockstats、langchain-core 写进我们自己的 `pyproject.toml`：今天它们都是
经上游间接装进来的，上游哪天不要了，风控代码会跟着坏。

### 4.13 固定版本：一处改，四处对

`pyproject.toml:53` 的 `rev`、`uv.lock`、`__init__.py:15` 的常量一起改成 `1394a3f` 的完整 40 位。**T20**：三处一致，并且和实际安装的包一致
（读安装包 `direct_url.json` 里的 `commit_id`）。这一条防的是“依赖升了、常量没改”——那样策略版本号不动、模拟天数不清零，
名下的记录却是另一套上游跑出来的。

### 4.14 Reddit 包装（来源关着）

只改路径和 `leans_on()`（`sources/reddit.py:77-93`）。上游 429 之后的等待从 5 秒改成 60 秒（`vendors/reddit.py:142`），我们的 `_Clock`
照旧替它等。Reddit 的 RSS 11-13 停，这个来源本来就建议关着，迁移不动开关。

---

## 5. 会改变研究结论的上游变化（给你看）

| # | 变化 | 上游位置 | 对我们意味着什么 |
|---|---|---|---|
| 1 | **“什么时候给 Hold”反过来了。** 以前：证据平衡、矛盾、含糊或不足就选 Hold。现在：有冲突本身不是 Hold 的理由，要判断哪边更强、按赢的程度定方向；只有权衡之后仍然平衡、或证据太少才 Hold。研究经理、交易员、组合经理的提示和结构化输出的字段说明都这样改了 | `research_manager.py`，`trader.py`，`portfolio_manager.py:66`，`schemas.py` | 预计 Hold 变少，买和卖两个方向都变多。研究模板下：BUY／Overweight 有更多机会过置信度下限；持仓上 REDUCE／SELL 提案会变多（`policy.py:29-35`）。基准模板（A）下：减持／卖出变多 → 否决（D2）和研究退出变多。**这是这次升级最大的研究行为变化** |
| 2 | 分析师不再被要求在报告前写 “FINAL TRANSACTION PROPOSAL: BUY/HOLD/SELL”，改成“只报告工具支持的内容，交易由别的角色决定” | `market_analyst.py`，`news_analyst.py`，`fundamentals_analyst.py` | 10-14 的分析发现市场分析师的报告 19 次里有 18 次写了 HOLD/NO TRADE，上游这条往反方向推。我们的委托书仍在所有角色的上下文里（`graph.py:176`），效果要在真实研究里看 |
| 3 | 评级只认 `Rating:` 标签；组合经理的结构化评级就是本次评级；引用别人的评级不算 | `rating.py:55-112` | §4.7 |
| 4 | 交易员：明说 Overweight＝Buy、Underweight＝Sell；入场价和止损只能写价格，不能写百分比 | `trader.py`，`schemas.py` | 我们不读交易员的价位，影响小 |
| 5 | **缺的字段明写出来**：组合经理多出 “Price Target: not provided”“Time Horizon: not provided”，交易员的入场价／止损／仓位缺了也写 “not provided” | `schemas.py` 的 `render_*` | 政策步骤读到这些行，**可能**多列“缺失信息”→ HR05d 更容易拒。真实研究要专门比较缺失信息的条数 |
| 6 | 没跑的分析师，在研究员和风险辩论的提示里写成“这份报告不可用，不是空结论” | `context.py:190-201` | ETF 研究每次都会出现两条；我们的政策步骤本来就写 “(not produced in this run)”（`policy.py:174`），一致 |
| 7 | 研究日期早于今天时扣数据 | §4.6 | §4.6 |
| 8 | 不传 `portfolio` 时提示“没有提供持仓” | `context.py:204-216` | §4.4，我们会传 |
| 9 | **新闻**：个股新闻为空时改读 Yahoo 搜索；两边都没有时说“不可用”而不是“没有新闻”；`get_news` 不再收代码 | `vendors/yahoo/news.py:87-130` | 升级要的就是这个：10-02 起 VTI、VXUS、BND、AMZN 的新闻全空。ETF 在 Yahoo 搜索里有没有足够的文章，要真实研究才知道 |
| 10 | 个股的基本面分析师多了 Yahoo 内部人交易 | `fundamentals_analyst.py:14-20` | §4.8，U9 |
| 11 | 情绪分析师按窗口截取 StockTwits／Reddit；有 TypeSafe 密钥时先筛帖子 | `sentiment_analyst.py:57-125` | 密钥会被拒（§4.1）；Reddit 关着 |
| 12 | 公司身份查询：失败不再被缓存，下次重查 | `context.py:61-104` | 以前一次查询失败，空身份会在整个进程里被记住（`classify_instrument` 据它分 ETF 和个股，`graph.py:87-102`），现在下次会重查。是好事 |

1、2、5 三条方向相反（1 推向有方向的结论，5 可能让 HR05d 更常拒），净效果只能量出来：§6.3 的真实研究和发布后头几次定时研究都要把它们列出来。

---

## 6. 测试计划

### 6.1 离线（`deploy/checks.sh` 的一部分；不联网、不用订阅）

- 新增 T1–T21（上面各节；T21 是 §3 的未来日期先拒）。
- 改写：§3 列的 9 个测试文件；`conftest.py` 清掉 `TRADINGAGENTS_*`、`TYPESAFE_*`；`test_web_access.py:266-280` 改成和上游 `TOOLS` 比较集合。
- 不动但必须绿：`tests/test_money_paths.py`（本次不碰任何下单路径）、退出契约和自动交易的全部用例。
- **T22 回滚读取**：用当前发布（`9dee508` 那一版）的代码，读本次新代码写出的研究记录（带 `point_in_time`）和断点库，不报错
  （“回滚前一版要先读得懂”这条规则，10-07 定的）。

### 6.2 隔离环境里的检查（我来做，不碰生产）

- 在单独的工作树和临时数据目录里装候选版本（`uv sync --frozen`），跑 `deploy/checks.sh`；
- 按沙盒 API 的做法（临时 `CT_DATA_DIR`、临时工作目录、端口 8790）起 API，用假 CLI 跑研究，看工作台、研究页、记录；
- 只读真实行情、不调模型：用候选环境对 SPY、^VIX、VTI、AMZN 各走一遍六处 `load_ohlcv` 的入口，和同一天生产读到的价格和日期比较。

### 6.3 一次真实研究（你来启动，U8）

- **为什么要你启动**：用订阅额度，结论要你看。
- **怎么跑**：候选版本的虚拟环境；临时数据目录，状态用 `ct-state backup` 的副本恢复进去（研究会写回撤阶梯状态，不能指向生产 `state/`）；
  `TRADINGAGENTS_CACHE_DIR` 指到临时目录，上游的断点和行情缓存不和生产共用。收盘半小时以后（本机 13:30 以后，和定时研究同一时间，
  当天的日线已经发布）VTI 和 AMZN 各跑一次，大约 25 次 `claude -p`。
- **验收**（全部从记录里读）：
  1. 两次都完成，评级不是 `REVIEW`；
  2. `llm_calls.jsonl` 每一行都是 CLI 调用；任何时刻最多 1 个调用在跑（U2＝1）；行数 = `call_finished` 个数；
  3. 新闻：`get_news` 的结果里有文章，或者明说“不可用”——不再是 “No news found”；
  4. 工具调用的参数里没有 `symbol`／`ticker`；
  5. AMZN：Form 4 出现在基本面分析师的调用里，来源记录里有这次读取；
  6. 缺失信息的条数、置信度、评级、耗时，和升级前最近一次同代码的研究并排列出。不设“必须更好”的门槛，只要求写下来给你看；
  7. 研究页和工作台显示正常，`point_in_time` 是 `null`（收盘后跑的）。
- 可选：盘中再跑一次 AMZN，看 `point_in_time` 是否如实列出被扣下的四类。

---

## 7. 上线时机（U7）与发布步骤

**事实：**

- 策略版本号 = 方案参数＋7 个逻辑文件＋委托书＋两个模型＋上游提交号（`auto/store.py:155-179`）。本次改的文件都不在那 7 个里，
  只动上游提交号一项，但这一项就够清零。
- 模拟账户：自动换绑新版本、从 0 重数（`auto/runner.py:153`）。实盘账户：如果有方案在生效，方案会被撤下，新版本攒满 30 个交易日之前
  不能开新仓，已买的照原条件退出（`auto/runner.py:158-166`）。

**代价随时间增加：**

| 什么时候发布 | 多清零多少 | 回滚 |
|---|---|---|
| 在 A 之前单独发布（本稿原建议 a，未采用） | 0：A 那次本来就要清零，现在的天数（基准契约记的是 1/30）本来就会作废 | 在 A 发布前：切回上一个发布即可。A 发布之后才发现问题：要另发一个撤回本次的版本，会再清零一次——所以至少等一次周五定时研究跑完、按 §6.3 的清单看过，再发 A |
| 和 A 同一次发布 | 0 | D8 已否：两边一起出问题就一起回滚 |
| **A 发布、跑稳 3–5 个交易日后（b，已定）** | 3–5 天 | 切回 A 那一版即可 |
| A 攒到二十几天时 | 二十几天 | 同上 |
| A 进实盘以后 | 实盘方案撤下，至少 30 个交易日不能开新仓 | 同上 |

本稿原先建议 a，理由留在这里备查：单看清零，a 不多花一天；另外，每拖一周，研究就多一周没有个股新闻；§5 #1 会改变 A 的否决频率，
A 的 30 天记录越多来自升级后的研究，越能代表它真正上实盘时的样子。

**决定（U7，2026-10-15）：b——A 发布、跑稳 3–5 个交易日后再发布本次迁移，不在 A 之前。** 这是上级会话的建议，你采纳了它而不是本稿的 a。
理由：A 可以早点开始；A 的接口前端还没审，不该被迁移挤到前面；两次大改动不紧挨着发布，出了问题分得清是哪一次。代价：多清零 3–5 个交易日；
A 那 30 天里最初几天的记录来自升级前的研究。那段时间里若本来就有别的会清零的改动（模型换代、委托书修改、7 个逻辑文件的修复），一起发布。

**发布步骤：**

1. `deploy/checks.sh` 全绿；§6.3 验收通过；
2. 研究队列里没有“被上限打断、等着重排”的任务（§4.11）；有就等它跑完；
3. `release.sh publish`（自带 `--fail-on-unsettled` 检查）；
4. 发布后：按“从沙盒验证发布”的做法核对（`lsof` 看端口、进程路径、`ct-ops` 心跳；不用 selfcheck 的回环请求）；模拟账户的日志里
   有一条 `auto_strategy_adjusted`（原因 `code_changed`）；第一次周五 13:30 定时研究按 §6.3 的清单再看一遍。

---

## 8. 回滚

- **代码**：`deploy/macos/release.sh rollback`。每个发布有自己的虚拟环境（`uv sync --frozen`，`release.sh:172`），上一个发布的环境里就是
  `9dee508`，不需要重装。回滚前同样先过 `--fail-on-unsettled`。
- **策略版本**：常量回到 `9dee508`，模拟账户换回旧版本号；旧版本自己的记录还在，升级期间攒的天数记在新版本名下，不算给旧版本
  （`auto/store.py:401-415` 的现有规则；实施时用一条测试确认）。
- **记录**：升级期间写的研究记录照常可读；新增的 `point_in_time` 只在 `decision_record.json` 里，旧版本不读它。本次不往任务行、提案这些
  严格校验的存储里加字段（10-07 的“回滚前一版要先读得懂”）。T22 证明。
- **断点**：两个方向签名都对不上，一律从头跑，不会接错。
- **行情缓存**：两种文件名共存，不用清。
- **配置**：迁移不需要你改配置。如果你按 U2 加了 `llm.max_parallel_calls_per_run`，旧版本会忽略它（`LLMSettings` 允许多余的键，
  `profile.py:48-50`）；按 U9 改了 `declared_data_sources`，那只是声明。
- **回不来的**：已经花掉的订阅额度和模拟天数。

---

## 9. 本轮不做

- 主动调整研究行为（U3 乙、把委托书从分析师那里拿掉等）：迁移只求“和今天一样，再加上新闻”；研究行为的改动另做、另评估。
- SEC EDGAR 财报（U4 乙）。
- 上游的回测、记忆和反思、TypeSafe Jev、HTML 报告、每层不同的模型服务：都不用。
- 改定时、改研究队列的并发数（`research_jobs.max_concurrent` 仍是 2）。
- 跟进 v0.6.0 之后的上游提交：以后的升级各自写契约。本稿的 T3（静态扫描）、T14（签名）、T20（版本一致）会在下次升级时先变红。

---

## 10. 实施顺序（你定完之后）

1. 单独的工作树和分支。和 A 批次的文件基本不重叠（A 改 `policy.py`、`risk/engine.py`、`autonomy.py`、`auto/*`、`service/approvals.py`
   等；本次改 `graph.py`、`llm/*`、`profile.py`、`config_changes.py`、`portfolio.py`、`service/base.py`、`cli.py`、`advisor.py`、
   `sources/reddit.py` 和测试），可以和 A 并行开发，谁后合入谁变基。
2. 版本、依赖、导入路径（§4.12、§4.13、§3 A 组），T20。
3. 模型注入与护栏（§4.1），T1–T4。
4. 工具服务器两种模式（§4.2），T5–T7。
5. 并发上限、进度（§4.3、§4.9），T8、T9、T15。
6. `portfolio` 入口（§4.4），T10。
7. SEC、历史研究记录、评级、签名、`load_ohlcv`、断点、HTML（§4.5–§4.11），T11–T14、T16–T18；pandas 样例 T19；未来日期 T21；回滚读取 T22。
8. 门禁全绿 → §6.2 → 你启动 §6.3 → 按 U7 发布。

---

## 11. 你的决定（2026-10-15：U1–U9 全部已定）

| 编号 | 问题 | 决定 |
|---|---|---|
| U1 | 目标版本 | v0.6.0（`1394a3f`，也就是今天上游的 `main`）（已定，按建议）。不选 `4429078`：它只比 v0.6.0 早 7 个提交，已经有 `create_tier_client`，却缺“引用别人的评级不算”（#1466）和“日期写错发回给模型”（#1476）两处修复 |
| U2 | 一次研究里同时最多几个模型调用 | 1，和今天一样（已定，按建议） |
| U3 | 账户快照放哪里 | 甲：留在原处，上游的持仓入口只放一句“见上文”（已定，按建议）。乙是研究行为改动，以后单独做 |
| U4 | SEC EDGAR 财报 | 甲：不开；配置里出现 `sec_edgar` 就拒绝；设置页的受保护列表加上来源配置和模型服务键（已定，按建议）。乙以后单独做 |
| U5 | 研究日期早于今天时上游扣数据 | 甲：接受，研究记录和研究页写明扣了什么（已定，按建议） |
| U6 | HTML 报告 | 不写（`html=False`）（已定，按建议） |
| U7 | 什么时候上线 | **b：A 发布、跑稳 3–5 个交易日后再发布，不在 A 之前**（已定，采纳上级会话的建议，不是本稿原先建议的 a；理由和 a 的理由都在 §7） |
| U8 | 真实研究 | 你来启动：VTI、AMZN 各一次，收盘后，临时数据目录；按 §6.3 验收（已定，按建议） |
| U9 | 个股基本面分析师新增的 Yahoo 内部人交易工具 | 保留（上游自带、只对个股）；你在 `declared_data_sources` 的 yfinance 那一条的 `used_for` 里加 `insider_transactions`——这是你改的配置（已定，按建议） |

你的原话（2026-10-15）：“按照你的建议都做完就好了。”这句话回的是上级会话列出的一份清单：U1–U6、U8、U9 推荐本稿的建议，U7 推荐 b。
所以 U7 记为 b。实施按 §10，上线时机按 U7；下面两个问题仍等前端回答。

**给前端的问题：**

- §4.6 的 `point_in_time` 块够不够在研究页上说清“这次研究少了哪些数据”？要不要在研究室里盘中发起个股研究前先提示一句？
- 工作台按 §4.9 的办法照常亮灯；§4.3 新增的“排队”（`queued_seconds`）要不要显示？
