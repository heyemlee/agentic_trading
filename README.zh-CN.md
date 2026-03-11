<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

<h1 align="center">Agentic Trading</h1>

<p align="center">
  <strong>从多智能体研究，到可审计的交易执行。</strong><br />
  理解持仓的 AI 研究、明确的风控边界，以及人工审批控制台。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/API-FastAPI-009688?style=flat" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/Frontend-Vanilla_JS-F7DF1E?style=flat" alt="原生 JavaScript 前端" />
</p>

<p align="center">
  <a href="#项目概览">项目概览</a> ·
  <a href="#系统架构">系统架构</a> ·
  <a href="#技术栈">技术栈</a> ·
  <a href="#关键工程设计">工程设计</a> ·
  <a href="#快速开始">快速开始</a>
</p>

<!-- 主界面截图准备好后，将下方图片路径替换为 docs/screenshots/dashboard.png。 -->
![项目主界面截图预留位置。](docs/screenshots/dashboard-placeholder.svg)

<p align="center"><sub>此处预留项目主界面截图。</sub></p>

## 项目概览

输入一个标的和持仓上下文，分析师收集市场证据，多空研究员展开辩论，管理者汇总成结构化决策。随后由确定性策略与风控代码判断，这份决策能否成为可执行的提案。

- **研究分析：** 按标的类型选择 Agent，注入持仓约束，保留分析报告与工具调用记录。
- **审批执行：** 查看提案、确认具体交易条款，提交订单前重新核验当前状态。
- **个人助手：** 按账户分析持仓、筛选股票与 ETF，并保留连续对话；个人账户独立于交易权限。
- **追踪评估：** 查看任务、订单、成交与审计事件，将前瞻结果与预先声明的被动基准比较。

当前实盘执行模式要求人工审批。研究 Agent 使用数据工具，执行权限由 Python 代码掌握。

## 系统架构

![系统架构：控制台和服务发起研究；Agent 使用持仓上下文、Claude Code 和 MCP 数据工具；提案经策略风控、人工审批、重新校验后提交券商。](docs/architecture/system.svg)

<sub>使用 <a href="https://github.com/tt-a1i/archify">Archify</a> 绘制 · <a href="docs/architecture/system.html">交互版 HTML：下载后打开</a> · <a href="docs/architecture/system.architecture.json">可编辑源文件</a> · <a href="docs/architecture/README.md">重新生成与校验</a></sub>

**研究 → 提案 → 审批 → 重新校验 → 提交订单 → 对账。** 模型建议是这条链路的输入。`NO_TRADE`、证据过期、提案拒绝、部分成交和提交结果不确定，都有明确的处理逻辑。

## 技术栈

| 层次 | 技术 | 在项目中的作用 |
| --- | --- | --- |
| 运行环境 | **Python 3.12 · uv** | 带类型约束的应用代码、锁定依赖，以及 `ct-*` 命令行工具。 |
| Agent 编排 | **TradingAgents · LangGraph · LangChain** | 复用固定版本的上游研究图，扩展持仓上下文、分析师选择，以及模型与推理强度路由。 |
| 模型与工具 | **Claude Code CLI · MCP Python SDK** | 兼容 LangChain 的订阅模型适配器；通过白名单开放行情、新闻、基本面和宏观数据工具。 |
| API 与数据契约 | **FastAPI · Uvicorn · Pydantic v2** | 在共享 Python 服务层上提供 HTTP/OpenAPI 接口与数据模型校验。 |
| 状态与配置 | **SQLite WAL · YAML · JSON/Markdown** | 事务化保存任务、提案、审批、订单与成交；配置可审阅，研究产物可直接检查。 |
| 网页控制台 | **HTML · CSS · 原生 JavaScript** | 中英双语、同源访问、共享轮询和页面状态保留，无需前端构建。 |
| 数据与交易 | **yfinance · FRED · exchange-calendars**；**模拟 / Robinhood 适配器** | 获取研究证据、识别交易所时段，并在确定性执行检查后访问券商。 |
| 质量与运维 | **pytest · Ruff · Node test runner · GitHub Actions · macOS launchd** | 离线检查、定时任务、不可变版本发布、回滚、备份与健康监控。 |

## 关键工程设计

[TradingAgents](https://github.com/TauricResearch/TradingAgents) 提供研究图和 Agent 角色，以 Git commit 固定版本，不复制或修改上游源码。本项目围绕它实现应用层与权限边界：

| 设计 | 实现与价值 |
| --- | --- |
| **推理与执行权限分离** | Pydantic 决策记录进入确定性动作映射和硬风控。模型再有信心，也不能放宽限制。[策略](agentic_trading/policy.py) · [风控引擎](agentic_trading/risk/engine.py) |
| **收紧模型运行时权限** | Claude Code 适配器禁用内置工具，使用严格 MCP 配置，移除 API 计费覆盖变量，并记录工具轨迹。CLI 失败直接暴露，不自动切换供应商。[适配器](agentic_trading/llm/claude_code.py) |
| **在资金入口重新校验** | 审批绑定提案版本与具体条款；执行前刷新持仓和市场状态，再检查一次风险。实盘下单入口集中管理，并由测试约束。[审批服务](agentic_trading/service/approvals.py) · [资金路径测试](tests/test_money_paths.py) |
| **处理提交结果不确定的情况** | 先持久化下单意图，再访问券商；结合幂等键与事务化资金预留。结果不确定时进入对账，避免盲目重发；区分部分成交与退出完成。[执行](agentic_trading/execution.py) · [状态库](agentic_trading/state_db.py) |
| **任务持久化与账户隔离** | SQLite 任务队列支持去重、并发限制和显式状态；个人账户、Agentic 账户与演示研究保留各自范围。[任务队列](agentic_trading/research_jobs.py) · [隔离测试](tests/test_account_scope.py) |
| **评估完整决策记录** | 前瞻评估冻结研究设置，纳入不交易与失败记录，分开演示和实盘样本，并剔除现金流对收益率的影响。软件测试验证行为，投资表现由持续样本检验。[评估](agentic_trading/evaluation.py) |

<details>
<summary>展开查看研究控制台 · 示例数据</summary>

![使用示例数据的研究控制台，展示研究记录和决策详情。](docs/screenshots/readme-research.png)

</details>

## 快速开始

需要 **Python 3.12** 和 **[uv](https://docs.astral.sh/uv/)**。AI 研究另需已登录的 Claude Code CLI，并能访问 `config/investor_profile.yaml` 中配置的模型。

以下用于**全新本地克隆**：

```bash
git clone https://github.com/heyemlee/agentic_trading.git
cd agentic_trading
uv sync --frozen

mkdir -p state runs
cp -n .env.example .env
cp -n config/examples/portfolio.example.yaml state/portfolio.yaml
cp -n config/examples/micro_live_portfolio.example.yaml state/micro_live_portfolio.yaml

uv run ct-auth   # 创建控制台登录凭据
uv run ct-api    # 启动 API 与网页控制台
```

打开 [localhost:8787](http://127.0.0.1:8787/) 并登录，按 Ctrl-C 停止前台服务。

> **连接券商前：** `config/` 保存所有者纳入版本管理的部署设置，其中包含已开启的实盘执行开关。全新克隆没有券商令牌，不能提交真实订单；通用券商适配器为模拟模式。连接账户前，请先审阅[风控配置](config/risk_profile.yaml)与[操作文档](docs/OPERATIONS.md)。不要设置 `ANTHROPIC_API_KEY`，研究使用 Claude Code 的订阅登录。

```bash
uv run ct-trigger manual --ticker VTI   # 提交研究任务，消耗 Claude 订阅额度
uv run ct-trigger scheduled --dry-run   # 预览调度，不启动研究
uv run ct-state status                 # 检查状态完整性和未结订单
uv run ct-ops status                   # 检查后台任务健康状态
```

报告和决策记录保存在 `runs/<run-id>/`，也可在控制台查看。可选 `.env` 设置包括：用于宏观数据的 `FRED_API_KEY`、用于网页推送的 `CT_PUSH_SUBJECT`，以及外部监控地址 `CT_HEARTBEAT_URL`。

## 开发与文档

```bash
deploy/checks.sh  # Ruff + Python 离线测试 + 前端用例 + 密钥扫描
```

测试使用模拟模型与券商，不需要真实凭据或模型调用，并通过防护禁止测试访问生产状态。

| 入口 | 内容 |
| --- | --- |
| [应用代码](agentic_trading/) | 轻量 `api/`、共享 `service/`、`llm/` 模型适配器、确定性 `risk/` 与 `broker/` 适配器。 |
| [控制台](ui/README.md) | 前端架构、数据新鲜度、审批交互与双语界面。 |
| [个人助手](docs/PERSONAL_ASSISTANT.md) | 持仓分析、账户范围、连续对话与能力边界。 |
| [操作文档](docs/OPERATIONS.md) | 配置、执行控制、对账与恢复。 |
| [macOS 部署](docs/MACOS_DEPLOYMENT.md) | launchd、发布、回滚、备份与维护。 |
