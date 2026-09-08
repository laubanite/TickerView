# TickerView

[简体中文](README.md) · **English**

> An **AI-assisted market monitor and decision-support system** for China A-share investors.
> In one line: **the rule engine computes precisely, the LLM explains clearly, you decide** — no price prediction, no auto-trading.

<p>
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows-lightgrey">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="install" src="https://img.shields.io/badge/install-no--admin-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-yellow">
</p>

---

## Why this exists

Retail investors face concrete pain: trading apps are noisy, decisions run on gut feel, and reviews are forgotten in three days. Meanwhile most "AI stock pickers" are either black boxes or let the model invent prices — **untrustworthy, and unusable in a financial context**.

TickerView's approach is to **draw a hard boundary around the AI**: every number and signal is computed by reproducible rules; the LLM only translates the result into plain language. The tool promises no alpha — it offers **information aggregation, discipline enforcement, and verifiability**.

## Product forms

| Form | What it is |
|---|---|
| 🖥️ **Desktop floating panel** | Borderless, always-on-top, tray-resident mini window for at-a-glance live quotes (`TickerView.exe`) |
| 🌐 **Web dashboard** | Indices / watchlist / K-line / intraday + pre-market · intraday · post-market panels, opened in a local browser |
| ⚙️ **Backend engine (source)** | Data pipeline, rule-signal engine, and a backtesting engine validated trade-by-trade (for research; not part of the desktop UI) |

<!-- 📸 Screenshots: drop images into docs/screenshots/ then uncomment -->
<!-- ![Desktop floating panel](docs/screenshots/panel.png) -->
<!-- ![Web dashboard](docs/screenshots/web.png) -->

## Key features

**Real-time monitoring**
- Shanghai / Shenzhen / ChiNext index snapshots + live watchlist quotes (price / change / turnover / volume ratio)
- Daily / 30-min candlesticks with moving averages and **key-level overlays**; intraday chart (with VWAP line), volume sub-chart
- Holdings highlighting; A-share convention **red = up, green = down** (semantic colors fixed)
- Tray-resident floating panel, same data source as the web view

**Pre-market · Market-state light**
- Answers one thing: **is there an event today that says don't move the strategy**. It doesn't decide for you and makes no technical judgment
- Fully deterministic grading: source tiering → keyword hits (polarity × severity + negation exclusion) → 🟢 normal / 🟡 cautious / 🔴 alert
- Bullish news never offsets bearish; the day only escalates, never de-escalates. Red/yellow **only nudges you to "hold off, don't open new positions" — it never touches your stop-losses or auto-tunes parameters**

**Intraday · Snapshot & signals (ETF / stock dual mode)**
- **ETF mode**: deterministic snapshot + one-line signal + LLM deep analysis + post-close counterfactual reasoning
- **Stock mode**: risk-monitoring snapshot + a state machine (breakdown / cost / deep drawdown / limit-up-down & suspension…), **risk flags only — no buy/sell advice**

**Post-market · Advice-verification calendar**
- Each intraday snapshot auto-archives the day's advice; every directional advice is **auto-judged "verified / not verified" after 3 / 5 trading days**, forming an objective effectiveness ledger (fully local, zero push)

**Settings**
- Models (multi-LLM config + connectivity test) · Holdings (position cards + account cash floor) · General (refresh interval / tray preferences)

## 🧠 What the AI actually does here

**The AI does exactly one thing: translate the facts the engine already computed into plain language. It doesn't compute, and it doesn't decide.**

A real intraday flow, so you can see it immediately:

1. You click "**Refresh snapshot**" → the engine reads the live price + daily bars, computes moving averages, volume ratio and key-level relationships, and emits a **deterministic signal**, e.g. "close below MA20, volume ratio 1.8 → breakdown · trim tier". **No AI in this step — it's all rules.**
2. You click "**Deep analysis**" → those **already-computed numbers** are handed to the LLM, which writes a plain-language read: "the symbol closed below its 20-day MA on rising volume; per your rules this is a trim tier; the 250-day line still offers support below…"
3. The model **may only cite numbers the engine provided**. If it invents a price the engine never gave, or contradicts itself directionally, **the validation chain bounces it**; on a second failure it falls back to showing only the deterministic card — **say less rather than say wrong**.

Pre-market works the same: if this morning an S-tier bearish item (e.g. "CSRC filing") is caught, the **rules** set the light to 🔴 directly; the AI only tags each news item bullish/bearish/neutral in the evidence list below — **it does not participate in grading**.

So the division of labor is crystal clear:

| Who | Does what | Traits |
|---|---|---|
| **Rule engine** | Computes price relationships, signals, pre-market grading, post-market verification | Reproducible, auditable, no LLM involved |
| **LLM** | Turns facts into fluent interpretation, tags news | Produces no numbers, makes no judgment, degrades on error |
| **You** | Read the hints, decide, place orders | The tool never trades for you |

## Data sources

All from public data sources; the tool **connects to no trading account and places no orders**:

- **Real-time quotes / indices / intraday**: Tencent market data, Tonghuashun (10jqka) on-floor snapshots
- **Daily / 30-min K-line**: Tencent market API (supplemented by akshare)
- **Overnight news**: Sina Finance 7×24 feed
- **Sector moves / hot lists**: Tonghuashun (10jqka)
- **Backtest indicator computation**: TA-Lib

## Tech stack

- **Backend**: Python · Flask · SQLite · akshare · multi-source direct fetch with fallback · TA-Lib
- **Frontend**: Vue 3 · ECharts (local vendor, no CDN dependency)
- **Desktop**: pywebview (Edge WebView2) · pystray (system tray) · Pillow (icons)
- **Packaging**: PyInstaller (onedir) · Inno Setup (per-user, no-admin install)
- **AI**: automatic failover across multiple free LLMs (keys stored locally only, never uploaded)

## 📦 Install (end users)

1. Go to **[Releases](../../releases)** and download `TickerView-Setup-0.1.0.exe`
2. Double-click to install — **per-user, no administrator rights required**; defaults to `%LOCALAPPDATA%\Programs\TickerView` (you can change it, e.g. to D:)
3. A "TickerView" entry appears in the Start menu / desktop; the app lives in the **system tray**

**Requirements**: Windows 10 / 11 (64-bit). Win11 ships WebView2; most Win10 machines already have it via Edge, and the installer also bootstraps it.

<details>
<summary>SmartScreen prompt on first run?</summary>

This project is not code-signed (paid certificate), so the first run may show "Windows protected your PC / unknown publisher". Click **More info → Run anyway**.
</details>

**Portable**: planned (please use the installer for now).

**Where your data lives**: config and database are stored under `%APPDATA%\TickerView\` (`config\` + `data\alphaprism.db`). **Uninstalling keeps your data**; reinstalling picks it right up. A fresh install starts with an empty watchlist — add your own symbols.

> The desktop app provides **real-time monitoring and deterministic signals out of the box**; pre-market news grading and intraday LLM deep analysis require network data and LLM keys configured in Settings.

## 🔨 Run from source (developers / full engine)

```bash
# 1) Install dependencies (Python 3.11+ recommended)
pip install -r requirements.txt

# 2) Desktop floating panel (auto-starts the local server + tray-resident)
python scripts/cli.py panel

# Or the pure web dashboard (open http://127.0.0.1:8765 in a browser)
python scripts/cli.py web
```

Data fetching, signals and the backtesting engine live in `scripts/cli.py` subcommands (e.g. `daily` / `signal`). LLM features need keys in `config/settings.local.yaml` (gitignored).

## 🔒 Data & privacy

- Fully local — **no account, no telemetry, nothing uploaded**; quotes come from public data sources.
- Model keys, watchlist and database stay on your own machine.

## ⚠️ Disclaimer

- This is a **research / personal tool** and does not constitute investment advice; market data may be delayed or inaccurate — rely on official disclosures.
- **No auto-trading** — you execute every trade and bear the risk. Markets carry risk; decide carefully.

## License

[MIT](LICENSE) © 2026 Mirenth

Third-party licenses are listed in [ATTRIBUTIONS.md](ATTRIBUTIONS.md).
