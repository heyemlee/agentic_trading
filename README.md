<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

<h1 align="center">Agentic Trading</h1>

<p align="center">
  <strong>AI agents research, debate, and propose. Humans approve.</strong><br />
  A portfolio-aware multi-agent system with tool use, structured decisions, and auditable execution.
</p>

<p align="center">
  <a href="#how-it-works">Architecture</a> ·
  <a href="#what-i-built">Engineering</a> ·
  <a href="#run-locally">Quick start</a>
</p>

## The idea

Give a team of AI agents a ticker and your portfolio context. Analysts gather evidence, bull and bear researchers challenge the thesis, and managers synthesize a recommendation. The system turns that research into a structured proposal with risks, missing information, and reasons to act—or do nothing.

The core design question: **how much authority should an agent have?** Here, agents own research; deterministic code enforces risk limits, and execution requires human approval and a fresh state check. Live trading is disabled by default.

## How it works

![Agentic Trading architecture: context-aware research agents use Claude and MCP data tools, then pass proposals through risk checks and human approval before execution.](docs/architecture/system.svg)

<sub>Drawn with <a href="https://github.com/tt-a1i/archify">Archify</a> · <a href="docs/architecture/system.html">Interactive HTML — download and open</a> · <a href="docs/architecture/system.architecture.json">Diagram source</a></sub>

## What I built

Built on [TradingAgents](https://github.com/TauricResearch/TradingAgents), which supplies the research graph and agent roles. My work integrates that graph into a complete application:

| Area | Implementation |
| --- | --- |
| **Agent orchestration** | Inject portfolio context and investment constraints into each role; select analysts by instrument type and route roles to different model/effort settings. [Code](conservative_trading/graph.py) |
| **Runtime & tool use** | A LangChain-compatible Claude Code adapter with allowlisted MCP data tools, structured output, and captured tool-call traces. [Code](conservative_trading/llm/claude_code.py) |
| **Decision boundaries** | Pydantic proposals, deterministic action mapping, and risk checks before review and again before execution. [Policy](conservative_trading/policy.py) · [Execution](conservative_trading/execution.py) |
| **Reliability & evaluation** | Durable research jobs, idempotent order handling, SQLite audit state, and forward evaluation against a passive benchmark. [Jobs](conservative_trading/research_jobs.py) · [Evaluation](conservative_trading/evaluation.py) |

Offline tests exercise the agent graph with a fake model runtime, tool isolation, policy failures, and execution recovery. [Explore tests →](tests)

<details>
<summary>View the research console</summary>

![Research console showing recorded runs and decision details using sample data.](docs/screenshots/readme-research.png)

<sub>Sample-data preview. Review research reports and decisions from the web console.</sub>

</details>

## Personal assistant

The console's **Assistant** adds read-only Robinhood holdings reviews, sourced stock/ETF candidates and persistent profile-aware chat, reusing the same research graph and job queue. Ordinary accounts remain outside live-trading permissions. See [capabilities and boundaries](docs/PERSONAL_ASSISTANT.md).

## Run locally

Requires **Python 3.12** and **[uv](https://docs.astral.sh/uv/)**. AI research also requires a signed-in Claude Code CLI with access to the models configured in `config/investor_profile.yaml`.

```bash
git clone https://github.com/heyemlee/agentic_trading.git
cd agentic_trading
uv sync
cp -n .env.example .env
cp -n state/portfolio.example.yaml state/portfolio.yaml
cp -n state/micro_live_portfolio.example.yaml state/micro_live_portfolio.yaml
uv run ct-auth              # create your local console login
uv run ct-api
```

Open [localhost:8787/ui/](http://127.0.0.1:8787/ui/). Broker setup and operating details are in the [operations guide](docs/OPERATIONS.md).

<sub>Python · TradingAgents / LangGraph · Claude Code · MCP · FastAPI · Pydantic · SQLite · Vanilla JS</sub>
