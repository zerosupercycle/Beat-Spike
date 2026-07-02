# Beat Spike

Automated trading bot for Polymarket crypto **up/down** markets. Beat Spike watches short-term price momentum on Binance or Chainlink, enters only when the move **crosses the slug-open beat price**, and buys in the **direction of the spike** (UP on a down→up cross, DOWN on an up→down cross).

Default mode is **paper trading** — no live orders until you configure credentials and enable execution.

## How it works

Each Polymarket up/down market has a **beat price** (the reference price at market open). Beat Spike runs a two-stage pipeline:

1. **Momentum trigger** — On the configured signal feed, `|price_now − price_{now−lookback}|` must exceed a USD threshold (e.g. $24 for BTC with a 3s lookback).
2. **Beat-cross direction** — The lookback window must cross the slug-open beat:
   - Price moves **below → above** beat → buy **UP**
   - Price moves **above → below** beat → buy **DOWN**
   - No cross → trade rejected

Orders are placed on the Polymarket CLOB (limit by default) during a configurable entry window (e.g. 240s after slug open on 5m BTC markets).

```text
Feed tick
  → |Δ| ≥ threshold?          (momentum spike)
  → ref and now straddle beat?
      → cross up   → BUY UP
      → cross down → BUY DOWN
  → CLOB order on chosen token
```

## Architecture

| Component | Command | Port | Role |
|-----------|---------|------|------|
| Dashboard server | `make server` | 8788 | Chainlink + Binance WebSocket feeds, beat resolution, REST/WS API |
| Trading bot | `make bot` | — | Detection, beat filter, order execution |
| Web UI | `make web` | 5174 | Live feeds, charts, trade history |
| Profile monitor | `make monitor` | — | Optional wallet watcher (disabled by default) |

The bot reads feeds from the dashboard server by default (`feeds.source: server`), so start the server before the bot.

## Getting started

### Prerequisites

- **Git**
- **Python 3.10+** with `python3` and `venv` on your PATH
- **Node.js 18+** and **npm** (for the web dashboard)
- **make** (Linux/macOS; on Windows use WSL or Git Bash)

Optional for production-style background runs: **PM2** (`npm install -g pm2`).

### 1. Clone the repository

```bash
git clone https://github.com/zerosupercycle/Beat-Spike.git
cd Beat-Spike
```

### 2. Install dependencies

```bash
make install
```

This creates a Python virtualenv, installs bot and server requirements, installs web npm packages, and copies `.env.example` to `.env` if `.env` does not exist yet.

### 3. Configure (optional for paper trading)

Paper mode works out of the box. To customize behavior, edit [`config/default.yaml`](config/default.yaml).

For live trading, set wallet and CLOB credentials in `.env` (see [Environment variables](#environment-variables)) and set `bot.mode: live` plus `execution.enabled: true` in config.

### 4. Run the stack

Open three terminals from the project root:

```bash
# Terminal 1 — feeds + API (start this first)
make server

# Terminal 2 — paper trading bot
make bot

# Terminal 3 — dashboard UI
make web
```

Then open http://localhost:5174 for the dashboard.

Verify the server is up:

```bash
make health
```

### 5. Latency check (automatic)

When you open this project in VS Code or Cursor, a Polymarket latency probe runs automatically for your OS. Results are saved to [`data/latency-probe.txt`](data/latency-probe.txt). Use this to compare endpoint latency from your machine or VPS before choosing feed sources or colocation.

To run it manually:

```bash
# Linux / macOS
bash .vscode/scripts/run-latency-probe.sh

# Windows
powershell -ExecutionPolicy Bypass -File .vscode/scripts/run-latency-probe.ps1
```

### PM2 (production-style)

Run all services in the background:

```bash
make pm2-start    # server + bot + web + monitor
make pm2-status
make pm2-logs
make pm2-stop
```

## Configuration

Primary config: [`config/default.yaml`](config/default.yaml)

| Section | Purpose |
|---------|---------|
| `bot.mode` | `paper` or `live` |
| `markets` | Assets and intervals (e.g. BTC 5m) |
| `entry` | Seconds after slug open / before close to allow entries |
| `strategy` | Signal feed, lookback, thresholds, beat-cross logic |
| `trading` | Share size, limit price, order style |
| `execution` | CLOB credentials gate (keep secrets in `.env`) |
| `feeds` | `server` (recommended) or `local` WebSocket feeds |
| `chainlink` | Optional Data Streams credentials for precise beat lookup |

Per-asset thresholds live under `strategy.by_asset`. Signal feed options: **`binance`** or **`chainlink`**.

### Environment variables

Set in `.env` (see [`.env.example`](.env.example)):

| Variable | Purpose |
|----------|---------|
| `BEAT_SPIKE_EXECUTION__PRIVATE_KEY` | Wallet private key (live only) |
| `BEAT_SPIKE_EXECUTION__FUNDER` | Polymarket proxy/funder address |
| `BEAT_SPIKE_EXECUTION__API_KEY` | CLOB API key |
| `BEAT_SPIKE_EXECUTION__API_SECRET` | CLOB API secret |
| `BEAT_SPIKE_EXECUTION__API_PASSPHRASE` | CLOB passphrase |
| `BEAT_SPIKE_BOT__MODE` | Override `bot.mode` |
| `BEAT_SPIKE_CHAINLINK__STREAMS_*` | Chainlink Data Streams credentials |

Legacy `DAWN_*` env names are still accepted as fallbacks.

### Live trading

1. Set wallet and CLOB credentials in `.env`.
2. Set `execution.enabled: true` and `bot.mode: live` in config (or via env).
3. Confirm risk limits under `risk` if enabled.
4. Run `make server` then `make bot`.

**Never commit `.env` or private keys.**

## Price feeds

| Feed | Source | Used for |
|------|--------|----------|
| Binance | Spot ticker WebSocket | Default signal feed; fast momentum |
| Chainlink | Polymarket RTDS or Data Streams | Resolution-aligned beat; optional signal feed |

Coinbase is not supported — use Binance or Chainlink.

## Monitor (optional)

[`config/monitor.yaml`](config/monitor.yaml) watches Polymarket profiles for up/down buys and captures price charts. It is **disabled by default**. Add your own profile URLs to `targets` and set `enabled: true` to use it.

## Data & logs

| Path | Contents |
|------|----------|
| `data/trades.jsonl` | Trade log |
| `data/bot_status.json` | Bot state |
| `data/trade_snapshots/` | Chart snapshots at trade time |
| `data/beat-spike.log` | Bot log file |
| `data/feed_beats.json` | Persisted per-slug beat prices |
| `data/latency-probe.txt` | Latest Polymarket endpoint latency report |

## Project layout

```text
bot/           Trading logic, feeds, Polymarket CLOB integration
server/        FastAPI dashboard + feed aggregator
web/           React/Vite dashboard
config/        YAML configuration
data/          Runtime logs and snapshots (gitignored in production)
```

## Makefile reference

Run `make help` for all targets. Common commands:

- `make install` — Install dependencies
- `make server` / `make bot` / `make web` — Run components
- `make bot-stop` — Stop a running bot
- `make web-build` — Production UI build
- `make clean` — Remove venv and node_modules

## Disclaimer

This software is for educational and research purposes. Trading on prediction markets involves financial risk. You are responsible for compliance with applicable laws and Polymarket terms of service. Use at your own risk.
