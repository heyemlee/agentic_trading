"""Conservative, portfolio-aware wrapper around the upstream TradingAgents framework.

Design principles (see MANDATE.md and the gap analysis of 2026-09-03):
- Upstream TradingAgents is a pinned dependency and is never modified.
- Personal preferences live in config/*.yaml, outside upstream code.
- The LLM layer runs on the investor's Claude subscription via the official
  Claude Code CLI; no Anthropic API key is ever used.
- Safety-critical limits are enforced by deterministic code (from Stage 2),
  never by prompts.
"""

__version__ = "0.1.0"

# The upstream commit this package was written against (v0.4.1 merge, 2026-09-01).
UPSTREAM_TRADINGAGENTS_COMMIT = "9dee508c44662702281a8dbaad1f7b42179b5ba7"
