# Trading Lab — transparent daily and intraday paper trading

This is an educational research app. It is not a real-time exchange feed and not the ChatGPT model. It does not place real brokerage orders. It requires no paid OpenAI API calls.



## Streaming Research Lab (experimental, disconnected by default)

**Web viewer:** https://efguser.github.io/trading-lab/streaming.html

**Streaming backend:** `streaming_lab.py` implements an independently saved minute-bar paper account, authenticated read-only JSON and Server-Sent Events (SSE), and a WebSocket connection to Massive's stock minute aggregates feed. It does not place real trades, retrain GPT, access brokerage accounts or automatically subscribe to a market-data service.

### Status and cost

- The streaming source is **not configured** in this repository and the backend is **not deployed**. The website clearly displays **NOT CONNECTED** until a private relay is available.
- The existing **daily and 15-minute simulators continue running on GitHub Actions at no OpenAI API cost**. The experimental private streaming engine adds no charges until you separately activate an eligible provider plan and suitable running backend.
- Massive's free Stocks Basic plan does **not** include WebSocket access. Per-minute U.S. stock feeds are delayed on some paid plans and real-time only on certain higher plans; provider availability, market hours, licensing and prices can change. See https://massive.com/docs/websocket/stocks/aggregates-per-minute and the provider's current plan table.
- This is a **separate virtual account** starting at $10,000. It collects at least 80 actual completed one-minute bars per ticker before evaluating EMA12/48, RSI14, volume, ATR and breakouts. No fabricated market data is ever substituted. After the previous bar is observed, mock orders can fill only at a following observed bar's opening price. Gaps cancel unfilled paper orders; mock fills assume idealized executions, fees and slippage.

### Private server setup (only if you already have legitimate feed access)

Use Python 3.12+ on your own computer or a suitable server. Install the dependency from `requirements-stream.txt`, then set environment variables **privately** on the server:

- `MASSIVE_API_KEY` — stock minute-aggregate provider key, **never place in GitHub or frontend HTML**.
- `MASSIVE_FEED=delayed` or `MASSIVE_FEED=realtime` — use only the feed that your provider plan actually entitles you to.
- `STREAM_VIEW_TOKEN` — randomly generated private bearer token, at least 24 characters.
- `STREAM_ALLOWED_ORIGIN=https://efguser.github.io` — approved frontend origin.
- `STREAM_BIND=127.0.0.1` and `PORT=8849` — defaults for private local development.

Start `python streaming_lab.py` only after configuring the server. It exposes **authenticated read-only** `/snapshot` and `/events` routes, with the latter pushing updated snapshots over SSE. The browser viewer requires a **private HTTPS relay URL** and a viewer token entered by the user; neither the viewer token nor provider key belongs in a public repository. For public-network hosting, configure TLS, appropriate access controls, a persistent disk for the `runtime/` directory, and check market-data redistribution licenses before making any quote data accessible. The server defaults to localhost on purpose. GitHub Pages cannot run this process.

### Test without a data subscription

Run `python streaming_lab.py --self-test` for repeatable artificial-bar unit tests: warmup, duplicate rejection, no-lookahead next-bar mock fills, gap cancellation, provider outage and syntax checks. These tests do not connect to the market-data provider, and **test fixtures never appear in the live trading dashboard**. GitHub Actions runs them under **Streaming Engine Safety Tests** whenever the streaming implementation changes.

### Known limitations

- Restarting the streaming backend preserves virtual positions and journal in `runtime/` only when storage is persistent. A new host without this directory starts a separate account.
- Provider WebSockets may deliver revised bars, missing one-minute bars, exchange delays and disconnections. This prototype records original completed bars and ignores revisions/duplicates; it does not simulate order-book dynamics, exchange halts, dividends, or actual execution quality.
- Public GitHub Pages refresh alone cannot provide real-time quotes. A continuously running backend with permitted feed access is required for private near-live research. **This project remains paper-only.**

## Intraday monitoring (new)

Visit the **[15-minute paper trading monitor](https://efguser.github.io/trading-lab/intraday.html)**. The homepage also checks the intraday report every 60 seconds.

- An additional **Intraday Paper Trading Monitor** workflow is scheduled at minute 08, 23, 38, and 53 of UTC hours 13–22 on weekdays. The script requests data only during approximately 9:45 a.m.–4:40 p.m. New York local time, handling daylight-saving time. Holidays, provider outages, GitHub delays and missing 15-minute bars may result in no new results.
- It uses completed **15-minute Yahoo Finance bars** (unofficial yfinance), with an additional eight-minute buffer. It is **NOT guaranteed real-time market data**. A separate TradingView chart can update independently, but does NOT drive the paper-trading algorithm and may also be delayed.
- A different mock $10,000 account evaluates EMA12 vs EMA48, 14-period RSI, relative volume, ATR volatility, and a breakout/reclaim trigger. It records every scan, filtered-out stock, blocked order, queued order, hypothetical fill, and data warning. No actual brokerage orders are possible.
- A virtual signal on one bar can fill only using a **later completed bar's OPEN** and is labeled as an estimated fill. It may be cancelled if scans are missed. Fees, slippage, 1% risk budget, 20% allocation cap and a 3-position limit are used.
- Each update writes `data/intraday_state.json`, `data/intraday_audit.jsonl`, and `site/data/intraday_snapshot.json`. The dashboard re-fetches the published snapshot every minute while open. Snapshot publication can lag behind a market event.
- Includes an SPY buy-and-hold return comparison from the monitor's first observed SPY price.

**About "live":** GitHub Pages is static and cannot maintain a stream or run an agent continuously. The free configuration provides automatically refreshed *near-live/delayed* snapshots, not guaranteed tick-by-tick quotes or instantaneous mock execution. Truly streaming exchange quotes and immediate execution simulations require a suitable licensed data service and always-running backend, potentially with fees.

## Overview

The market scanner examines SPY, QQQ, AAPL, NVDA, MSFT, AMZN, AMD, GOOGL and TE each time a new completed daily bar is available from Yahoo Finance via the unofficial yfinance client. It checks SMA20/SMA50 trend, RSI14 momentum, volume confirmation, ATR14 volatility, and either a 10-day breakout or a 10-day moving-average recovery. Position exits are reviewed using the 20-day moving average, RSI, and a 2.5 ATR trailing threshold.

The independent virtual account starts with $10,000 cash, can hold up to three symbols, and limits each position to a 20% virtual allocation and 1% initial risk budget. It estimates a $1 fee and 0.1% slippage. It never assumes trades occur at the historical signal close: paper instructions queued after an observed close can fill at the next observed daily open.

Every symbol scan (including HOLD reasons), blocked or pending order, paper fill, provider error, and no-new-bar status is written as an event in data/market_audit.jsonl. Saved paper balances are in data/market_state.json; the public dashboard reads site/data/market_snapshot.json. There is no fake-price fallback if market data is unavailable. The old fictional-price trading research project remains in research.py and site/synthetic.html, with separate state.

## First run and publication

1. Go to Settings → Actions → General and enable Actions, with Workflow Permissions set to Read and write.
2. Go to Settings → Pages and choose GitHub Actions as the deployment source.
3. Open Actions → Paper Trading Research → Run workflow. Code updates also trigger the workflow.
4. Check the run's validation, provider and Pages deployment steps. After success, the repo's GitHub Pages settings will show the web address.

The GitHub Actions workflow is scheduled at 23:20 UTC Monday–Friday, after the U.S. market close. Scheduled runs may be delayed or missed. Github Pages is static: it shows the latest saved result, not continuous intraday trading.

## Data and modeling caveats

yfinance is an unofficial personal-use research library, not an exchange-licensed real-time data product. Prices can be delayed, unavailable, incomplete or adjusted for corporate events. Yahoo's data terms govern use and redistribution; do not treat this public dashboard as a licensed market-data feed.

Bars before 5:15 p.m. America/New_York on the current date are excluded to avoid incomplete-session signals. If data is more than five calendar days old or missing, a warning is logged and no new orders are generated for the affected symbols. Repeated scans of an unchanged trading date are idempotent. Next-day simulated open fills are assumptions; broker execution, intraday stops, bid-ask spread and real capital constraints are not fully modeled.

## Repository files

- market_research.py: EOD data ingestion, indicators, rules, virtual orders, state, audit and self-tests.
- site/index.html: accessible public snapshot of every scan, trade and order.
- .github/workflows/paper-trading.yml: scheduled daily research and publication.
- requirements.txt: pinned free research data library.
- data/market_audit.jsonl: detailed append-only audit (generated by runs).

Local safety check: python market_research.py --self-test

All experiments are educational and paper-only. Past simulated performance is not a forecast of investment returns.