# AlphaTap

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives AI agents clean, structured access to financial market data.

**Four tools. Three data sources. No bloat.**

| Tool | Data source | Key |
|---|---|---|
| `get_price` | Alpha Vantage (stocks) · CoinGecko (crypto) | AV required · CG optional |
| `get_news` | Alpha Vantage News & Sentiment | AV required |
| `get_macro_calendar` | St. Louis Fed FRED | FRED required |
| `get_context_bundle` | All of the above, concurrently | AV + FRED required |

---

## Prerequisites

- Python 3.11+
- Free API keys (takes ~2 minutes):
  - [Alpha Vantage](https://www.alphavantage.co/support/#api-key) — stocks, news/sentiment
  - [FRED](https://fredaccount.stlouisfed.org/apikeys) — macro calendar
  - [CoinGecko](https://www.coingecko.com/en/api) — optional; public free tier works without a key

---

## Setup

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/market-intel-mcp.git
cd market-intel-mcp

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
# Edit .env and fill in your API keys
```

---

## Running

### studio (default — for Claude Desktop, Cursor, etc.)

```bash
python server.py
```

### Streamable HTTP (for remote/multi-client use)

```bash
python server.py --http
# Listens on http://localhost:8000
```

### Test with MCP Inspector

```bash
npx @modelcontextprotocol/inspector python server.py
```

---

## Claude Desktop integration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or  
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "market-intel": {
      "command": "python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "ALPHA_VANTAGE_API_KEY": "your_key",
        "FRED_API_KEY": "your_key"
      }
    }
  }
}
```

---

## Tool Reference

### `get_price`

Get the current price and key market data for a stock or cryptocurrency.  
Auto-detects asset type from the ticker symbol.

**Input schema**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `ticker` | string | ✅ | — | `"AAPL"`, `"NVDA"`, `"BTC"`, `"ethereum"` |
| `asset_type` | `"auto"` \| `"stock"` \| `"crypto"` | ❌ | `"auto"` | Force a data source or let the server detect |
| `response_format` | `"markdown"` \| `"json"` | ❌ | `"markdown"` | Output format |

**Stock output fields** (Alpha Vantage)

```
ticker, price, change, change_pct, open, high, low, volume,
previous_close, latest_trading_day
```

**Crypto output fields** (CoinGecko)

```
ticker, coin_id, price_usd, change_24h_pct, market_cap_usd,
volume_24h_usd, last_updated_at
```

**Supported crypto tickers** (auto-detected):  
`BTC ETH SOL BNB XRP ADA AVAX DOGE DOT LINK MATIC LTC UNI ATOM ARB OP SUI APT PEPE SHIB TON TRX`  
Full CoinGecko IDs (e.g. `"bitcoin"`, `"solana"`) are also accepted.

---

### `get_news`

Fetch recent headlines and AI-generated sentiment scores for a stock ticker.

**Input schema**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `ticker` | string | ✅ | — | Stock ticker, e.g. `"AAPL"`, `"TSLA"`, `"SPY"` |
| `limit` | integer 1–50 | ❌ | `10` | Number of articles |
| `response_format` | `"markdown"` \| `"json"` | ❌ | `"markdown"` | Output format |

**Per-article output fields**

```
title, source, url, published_at, summary,
overall_sentiment_score, overall_sentiment_label,
ticker_sentiment_score, ticker_sentiment_label
```

**Sentiment scale**

| Score range | Label |
|---|---|
| ≤ −0.35 | Bearish |
| −0.35 to −0.15 | Somewhat-Bearish |
| −0.15 to 0.15 | Neutral |
| 0.15 to 0.35 | Somewhat-Bullish |
| ≥ 0.35 | Bullish |

---

### `get_macro_calendar`

Return upcoming scheduled macroeconomic data releases from the St. Louis Fed FRED API.

**Input schema**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `days_ahead` | integer 1–90 | ❌ | `14` | Calendar days forward to search |
| `response_format` | `"markdown"` \| `"json"` | ❌ | `"markdown"` | Output format |

**Covered events**

| Abbreviation | Release |
|---|---|
| `CPI` | Consumer Price Index |
| `NFP` | Employment Situation (Nonfarm Payrolls) |
| `GDP` | Gross Domestic Product |
| `PCE` | Personal Income and Outlays |
| `RETAIL` | Advance Retail Sales |
| `PPI` | Producer Price Index |
| `FOMC` | FOMC Summary of Economic Projections |
| `JOBLESS` | Unemployment Insurance Weekly Claims |
| `HOUSING` | New Residential Sales |
| `DURABLES` | Manufacturers' Durable Goods Orders |
| `CONFB` | Conference Board Consumer Confidence |
| `ISM_MFG` | ISM Manufacturing PMI |

**Per-event output fields**

```
event, abbr, date (YYYY-MM-DD), release_id
```

---

### `get_context_bundle`

Fetch price action, company overview, recent news with sentiment, and upcoming macro catalysts for a stock — all in one concurrent call.

Designed as a pre-analysis context block. All four data sources are fetched concurrently to minimise latency. Partial failures (e.g. FRED key not set) are surfaced gracefully inside the bundle rather than causing a full error.

**Input schema**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `ticker` | string | ✅ | — | Stock ticker, e.g. `"NVDA"`, `"AAPL"` |
| `news_limit` | integer 1–20 | ❌ | `5` | Articles to include |
| `calendar_days` | integer 1–30 | ❌ | `14` | Days ahead for macro catalysts |

**Output structure**

```jsonc
{
  "ticker": "NVDA",
  "fetched_at": "2025-01-15T10:30:00+00:00",
  "price_action": {
    "ticker": "NVDA",
    "price": 142.35,
    "change": 3.21,
    "change_pct": "2.31%",
    "open": 139.80,
    "high": 143.10,
    "low": 138.95,
    "volume": 42391000,
    "previous_close": 139.14,
    "latest_trading_day": "2025-01-15"
  },
  "company_overview": {
    "name": "NVIDIA Corporation",
    "sector": "Technology",
    "industry": "Semiconductors",
    "description": "...",
    "market_cap": "3480000000000",
    "pe_ratio": "52.3",
    "forward_pe": "38.1",
    "dividend_yield": "0.0003",
    "52_week_high": "153.13",
    "52_week_low": "75.61",
    "analyst_target_price": "165.00"
  },
  "recent_news": [
    {
      "title": "...",
      "source": "...",
      "url": "...",
      "published_at": "20250115T103000",
      "summary": "...",
      "ticker_sentiment_score": 0.312,
      "ticker_sentiment_label": "Somewhat-Bullish"
    }
  ],
  "upcoming_catalysts": [
    {
      "event": "Consumer Price Index (CPI)",
      "abbr": "CPI",
      "date": "2025-01-17",
      "release_id": "10"
    }
  ]
}
```

---

## Rate limits

| Service | Free tier |
|---|---|
| Alpha Vantage | 25 requests/day, 5 req/min |
| CoinGecko | 30 calls/min (no key) |
| FRED | 120 requests/min |

`get_context_bundle` makes up to 4 Alpha Vantage requests (quote + overview + news + macro×12 FRED calls). Keep this in mind on the free Alpha Vantage tier.

---

## Project structure

```
market-intel-mcp/
├── server.py          # FastMCP server — all tools in one file
├── requirements.txt
├── .env.example       # Copy to .env and fill in your keys
├── .gitignore
└── README.md
```

---

## Usage Note
**This repository is for portfolio display and review only.** 
The code is not licensed for public use, distribution, or modification. If you are interested in using this server for a project or contributing to its development, please see the **Copyright & Contributions** section below.

---

## Setup (For Authorized Collaborators)

```bash
# 1. Accessing the code
# Note: Cloning is permitted only for authorized contributors.
cd market-intel-mcp

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
# Edit .env and fill in your API keys
```

---

## Copyright & Contributions

**© 2026 Udysses. All rights reserved.**

I am not currently accepting unsolicited Pull Requests. To maintain the integrity of this project:

1. **Viewing only:** You are welcome to explore the code here on GitHub.
2. **No Unauthorized Redistribution:** Use, reproduction, or distribution of this code without express written permission is prohibited.
3. **How to contribute:** If you have an idea for a feature or a bug fix, please **send me a Direct Message (DM)** or open a **GitHub Issue** first. 
4. **Pull Requests:** PRs are restricted to approved collaborators. If we agree on a change, I will add you as a collaborator to allow your submission.
