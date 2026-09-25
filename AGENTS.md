# AGENTS.md — Agent Guide for Kalshi-Frigo

## Project Overview

AI-automated trading toolkit for [Kalshi](https://kalshi.com) prediction markets. Python 3.12+ async codebase with LLM-driven decision making (OpenRouter), SQLite telemetry, and a Streamlit dashboard.

## Essential Commands

```bash
# Setup
python setup_env.py            # creates .venv, installs deps
cp env.template .env           # add API keys

# Run (from project root)
python cli.py run --paper           # AI directional, paper mode
python cli.py run --live            # AI directional, live (real money)
python cli.py run --safe-compounder # Conservative edge-based, no LLM
python cli.py run --beast           # Aggressive (NOT default)
python cli.py health                # Verify API + DB connectivity
python cli.py dashboard             # Streamlit dashboard

# Tests
pytest -v                                    # All tests (safe, no credentials)
RUN_LIVE_TESTS=1 pytest -m live             # Real Kalshi API orders — risky

# Lint/typecheck
black . && isort . && mypy src/

# Deploy (Railway)
railway deploy
```

## Architecture

```
cli.py  →  BeastModeBot  →  src/jobs/trade.py  →  unified trading system
                    ↓
              src/jobs/decide.py   (LLM decision per market)
              src/jobs/execute.py  (order placement)
              src/jobs/ingest.py   (market data pull)
              src/jobs/track.py    (position monitoring + exits)
              src/jobs/evaluate.py (performance evaluation)
```

Key packages:
- `src/clients/` — Kalshi REST+WS client, OpenRouter/xAI client
- `src/agents/` — LLM agent definitions (bear/bull researchers, debate, ensemble)
- `src/jobs/` — Core async jobs (decide, execute, ingest, track, evaluate, trade)
- `src/strategies/` — Trading strategies (market_making, quick_flip_scalping, safe_compounder, etc.)
- `src/utils/` — Database, logging, position sizing, cash reserves, stop-loss, edge filter
- `src/config/settings.py` — All trading knobs as dataclasses

## Critical Gotchas

1. **xai_client is a misnomer** — it routes through OpenRouter, never xAI directly. Cost tracking lives on this client.
2. **Ensemble agents are NOT wired to live trading** — `src/agents/` contains scaffolding only. The live path in `src/jobs/decide.py` calls a single model with fallback.
3. **Kalshi prices are cents (integers 1-99)**; internal code uses dollars (0.0-1.0). Conversion happens in `execute.py` and `market_prices.py`.
4. **Paper vs live** is controlled by `--paper`/`--live` CLI flags AND the `LIVE_TRADING_ENABLED` env var in settings. Default is paper.
5. **Kalshi private key** must be at `kalshi_private_key.pem` in project root (git-ignored, no extension).
6. **Price sanity checks** in `execute.py` (issue #42): collection tickers return $1.00/$1.00 and are not tradeable — skip them. Prices ≤0¢ or ≥100¢ are invalid.
7. **Fail-closed balance check**: if `get_balance()` errors before placing an order, the order is skipped rather than risking an unaffordable trade.
8. **`settings.trading` defaults change between runs** — `cli.py run` mutates settings inline (confidence, position size, drawdown). The "disciplined" defaults are applied at runtime, not in the dataclass definition.
9. **Safe Compounder mode** (`--safe-compounder`) does NOT use the LLM at all — pure math/edge-based.
10. **Database initialization must complete** before trading cycles start — `beast_mode_bot.py:110-113` has explicit await for this.

## Code Patterns

- All trading logic is `async`/`await` — use `asyncio.run()` at entry points
- Logging via `get_trading_logger("name")` (structlog-based, not stdlib logging)
- Position sizing uses fractional Kelly Criterion (quarter-Kelly by default)
- Database: `aiosqlite`, single file `trading_system.db`
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"`
- Config is dataclasses + `python-dotenv`, no Pydantic for settings

## Testing Notes

- `tests/conftest.py` gates live tests behind `RUN_LIVE_TESTS=1` AND `@pytest.mark.live`
- CI runs `pytest -v` with no secrets — live tests are auto-skipped
- Test fixtures: `tests/fixtures/markets.json`
- Never run live tests against an account you're not prepared to lose money on
