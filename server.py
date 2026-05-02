#!/usr/bin/env python3
"""
market-intel-mcp — Market Intelligence MCP Server

Four tools:
  get_price          — stock quote (Alpha Vantage) or crypto price (CoinGecko)
  get_news           — ticker headlines + AI sentiment scores (Alpha Vantage)
  get_macro_calendar — upcoming FRED data releases + curated macro events
  get_context_bundle — price + overview + news + catalysts in one structured call

Environment variables (see .env.example):
  ALPHA_VANTAGE_API_KEY  — required for get_price (stocks), get_news, get_context_bundle
  FRED_API_KEY           — required for get_macro_calendar
  COINGECKO_API_KEY      — optional; free public tier works without one
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field
from mcp.server.fastmcp import FastMCP

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# API credentials
# ---------------------------------------------------------------------------

ALPHA_VANTAGE_KEY: str = os.getenv("ALPHA_VANTAGE_API_KEY", "")
FRED_KEY: str = os.getenv("FRED_API_KEY", "")
COINGECKO_KEY: str = os.getenv("COINGECKO_API_KEY", "")

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------

AV_BASE = "https://www.alphavantage.co/query"
FRED_BASE = "https://api.stlouisfed.org/fred"

# CoinGecko: use Pro endpoint when a key is supplied, free public endpoint otherwise
def _cg_url(path: str) -> str:
    base = "https://pro-api.coingecko.com/api/v3" if COINGECKO_KEY else "https://api.coingecko.com/api/v3"
    return f"{base}{path}"

# ---------------------------------------------------------------------------
# Curated crypto symbol → CoinGecko ID mapping (top ~25 assets)
# ---------------------------------------------------------------------------

CRYPTO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "DOGE": "dogecoin",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "MATIC": "matic-network",
    "LTC": "litecoin",
    "UNI": "uniswap",
    "ATOM": "cosmos",
    "ARB": "arbitrum",
    "OP": "optimism",
    "SUI": "sui",
    "APT": "aptos",
    "PEPE": "pepe",
    "SHIB": "shiba-inu",
    "TON": "the-open-network",
    "TRX": "tron",
    "WIF": "dogwifcoin",
    "BONK": "bonk",
    "FET": "fetch-ai",
}
# Accept full CoinGecko IDs too (e.g. "bitcoin", "ethereum")
_KNOWN_CG_IDS: set[str] = set(CRYPTO_IDS.values())


def _resolve_crypto_id(ticker: str) -> str:
    """Map a ticker symbol or CoinGecko ID to a canonical CoinGecko coin ID."""
    upper = ticker.upper()
    lower = ticker.lower()
    if upper in CRYPTO_IDS:
        return CRYPTO_IDS[upper]
    if lower in _KNOWN_CG_IDS:
        return lower
    # Fall back to the lowercase value — CoinGecko will 404 if it's wrong
    return lower


def _is_likely_crypto(ticker: str) -> bool:
    return ticker.upper() in CRYPTO_IDS or ticker.lower() in _KNOWN_CG_IDS

# ---------------------------------------------------------------------------
# Curated macro releases (FRED release IDs)
# ---------------------------------------------------------------------------

MACRO_RELEASES: dict[str, dict[str, str]] = {
    "CPI":      {"release_id": "10",  "name": "Consumer Price Index (CPI)"},
    "NFP":      {"release_id": "50",  "name": "Employment Situation (Nonfarm Payrolls)"},
    "GDP":      {"release_id": "53",  "name": "Gross Domestic Product (GDP)"},
    "PCE":      {"release_id": "54",  "name": "Personal Income and Outlays (PCE)"},
    "RETAIL":   {"release_id": "44",  "name": "Advance Retail Sales"},
    "PPI":      {"release_id": "48",  "name": "Producer Price Index (PPI)"},
    "FOMC":     {"release_id": "326", "name": "FOMC Summary of Economic Projections"},
    "JOBLESS":  {"release_id": "56",  "name": "Unemployment Insurance Weekly Claims"},
    "HOUSING":  {"release_id": "42",  "name": "New Residential Sales"},
    "DURABLES": {"release_id": "243", "name": "Manufacturers' Durable Goods Orders"},
    "CONFB":    {"release_id": "36",  "name": "Conference Board Consumer Confidence"},
    "ISM_MFG":  {"release_id": "52",  "name": "ISM Manufacturing PMI"},
}

# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------

async def _get(url: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def _get_with_headers(url: str, params: dict, headers: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()


def _fmt_error(e: Exception) -> str:
    """Return a concise, actionable error string."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return "Error: Invalid API key — check your environment variables."
        if code == 403:
            return "Error: Access denied. Your plan may not include this endpoint."
        if code == 404:
            return "Error: Resource not found. Verify the ticker symbol or coin ID."
        if code == 429:
            return "Error: Rate limit reached. Wait a moment then retry."
        return f"Error: Upstream API returned HTTP {code}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. The upstream API may be slow — retry in a moment."
    if isinstance(e, ValueError):
        return f"Error: {e}"
    return f"Error: {type(e).__name__}: {e}"

# ---------------------------------------------------------------------------
# Core data-fetch functions (called by both tools and the bundle)
# ---------------------------------------------------------------------------

async def _fetch_stock_quote(symbol: str) -> dict:
    """Alpha Vantage GLOBAL_QUOTE endpoint."""
    if not ALPHA_VANTAGE_KEY:
        raise ValueError("ALPHA_VANTAGE_API_KEY is not set. Get a free key at https://www.alphavantage.co/support/#api-key")
    data = await _get(AV_BASE, {
        "function": "GLOBAL_QUOTE",
        "symbol": symbol.upper(),
        "apikey": ALPHA_VANTAGE_KEY,
    })
    q = data.get("Global Quote", {})
    if not q:
        # Alpha Vantage returns an empty object or a Note on rate-limit / bad symbol
        note = data.get("Note") or data.get("Information", "")
        if note:
            raise ValueError(f"Alpha Vantage returned a rate-limit or API notice: {note}")
        raise ValueError(f"No quote data found for '{symbol}'. Verify the ticker symbol.")
    return {
        "ticker": q.get("01. symbol"),
        "price": float(q.get("05. price", 0)),
        "open": float(q.get("02. open", 0)),
        "high": float(q.get("03. high", 0)),
        "low": float(q.get("04. low", 0)),
        "volume": int(q.get("06. volume", 0)),
        "latest_trading_day": q.get("07. latest trading day"),
        "previous_close": float(q.get("08. previous close", 0)),
        "change": float(q.get("09. change", 0)),
        "change_pct": q.get("10. change percent", "0%"),
    }


async def _fetch_crypto_price(ticker: str) -> dict:
    """CoinGecko /simple/price endpoint."""
    coin_id = _resolve_crypto_id(ticker)
    headers: dict[str, str] = {}
    if COINGECKO_KEY:
        headers["x-cg-pro-api-key"] = COINGECKO_KEY

    data = await _get_with_headers(
        _cg_url("/simple/price"),
        {
            "ids": coin_id,
            "vs_currencies": "usd",
            "include_market_cap": "true",
            "include_24hr_vol": "true",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        },
        headers,
    )
    if not data or coin_id not in data:
        raise ValueError(
            f"No price data found for '{ticker}' (tried CoinGecko ID '{coin_id}'). "
            "Try using the exact CoinGecko ID, e.g. 'bitcoin', 'ethereum'."
        )
    d = data[coin_id]
    last_updated = None
    if d.get("last_updated_at"):
        last_updated = datetime.fromtimestamp(d["last_updated_at"], tz=timezone.utc).isoformat()
    return {
        "coin_id": coin_id,
        "ticker": ticker.upper(),
        "price_usd": d.get("usd"),
        "market_cap_usd": d.get("usd_market_cap"),
        "volume_24h_usd": d.get("usd_24h_vol"),
        "change_24h_pct": d.get("usd_24h_change"),
        "last_updated_at": last_updated,
    }


async def _fetch_company_overview(symbol: str) -> dict:
    """Alpha Vantage OVERVIEW endpoint — sector, description, valuation metrics."""
    if not ALPHA_VANTAGE_KEY:
        return {}
    try:
        data = await _get(AV_BASE, {
            "function": "OVERVIEW",
            "symbol": symbol.upper(),
            "apikey": ALPHA_VANTAGE_KEY,
        })
        if not data or "Symbol" not in data:
            return {}
        desc = data.get("Description", "") or ""
        return {
            "name": data.get("Name"),
            "sector": data.get("Sector"),
            "industry": data.get("Industry"),
            "exchange": data.get("Exchange"),
            "description": desc[:500] + ("..." if len(desc) > 500 else ""),
            "market_cap": data.get("MarketCapitalization"),
            "pe_ratio": data.get("PERatio"),
            "forward_pe": data.get("ForwardPE"),
            "dividend_yield": data.get("DividendYield"),
            "52_week_high": data.get("52WeekHigh"),
            "52_week_low": data.get("52WeekLow"),
            "analyst_target_price": data.get("AnalystTargetPrice"),
        }
    except Exception:
        return {}


async def _fetch_news(ticker: str, limit: int = 10) -> list[dict]:
    """Alpha Vantage NEWS_SENTIMENT endpoint."""
    if not ALPHA_VANTAGE_KEY:
        raise ValueError("ALPHA_VANTAGE_API_KEY is not set. Get a free key at https://www.alphavantage.co/support/#api-key")
    data = await _get(AV_BASE, {
        "function": "NEWS_SENTIMENT",
        "tickers": ticker.upper(),
        "limit": min(limit, 50),
        "sort": "LATEST",
        "apikey": ALPHA_VANTAGE_KEY,
    })
    note = data.get("Note") or data.get("Information", "")
    if note:
        raise ValueError(f"Alpha Vantage API notice: {note}")
    feed = data.get("feed", [])
    articles: list[dict] = []
    for item in feed:
        ticker_sentiment_list = item.get("ticker_sentiment", [])
        # Prefer the sentiment score specific to the queried ticker
        ticker_sent = next(
            (t for t in ticker_sentiment_list if t.get("ticker", "").upper() == ticker.upper()),
            None,
        )
        articles.append({
            "title": item.get("title"),
            "source": item.get("source"),
            "url": item.get("url"),
            "published_at": item.get("time_published"),
            "summary": item.get("summary"),
            "overall_sentiment_score": item.get("overall_sentiment_score"),
            "overall_sentiment_label": item.get("overall_sentiment_label"),
            "ticker_sentiment_score": (
                float(ticker_sent["ticker_sentiment_score"]) if ticker_sent else None
            ),
            "ticker_sentiment_label": (
                ticker_sent.get("ticker_sentiment_label") if ticker_sent else None
            ),
        })
    return articles


async def _fetch_macro_calendar(days_ahead: int = 14) -> list[dict]:
    """FRED release/dates for each curated macro event."""
    if not FRED_KEY:
        return [{"_note": "FRED_API_KEY not set — set it to enable macro calendar data. Register at https://fredaccount.stlouisfed.org/apikeys"}]

    today = datetime.now(tz=timezone.utc).date()
    end_date = today + timedelta(days=days_ahead)

    async def _release_dates(abbr: str, meta: dict[str, str]) -> list[dict]:
        try:
            data = await _get(f"{FRED_BASE}/release/dates", {
                "release_id": meta["release_id"],
                "realtime_start": str(today),
                "realtime_end": str(end_date),
                "api_key": FRED_KEY,
                "file_type": "json",
                "include_release_dates_with_no_data": "true",
                "sort_order": "asc",
            })
            return [
                {
                    "event": meta["name"],
                    "abbr": abbr,
                    "date": entry.get("date"),
                    "release_id": meta["release_id"],
                }
                for entry in data.get("release_dates", [])
            ]
        except Exception:
            return []  # skip individual failed releases

    results = await asyncio.gather(*[
        _release_dates(abbr, meta) for abbr, meta in MACRO_RELEASES.items()
    ])

    events: list[dict] = [event for batch in results for event in batch]
    events.sort(key=lambda x: x.get("date", ""))
    return events

# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

class GetPriceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    ticker: str = Field(
        ...,
        description=(
            "Ticker symbol for a stock (e.g. 'AAPL', 'MSFT', 'NVDA', 'SPY') "
            "or a cryptocurrency symbol / CoinGecko ID (e.g. 'BTC', 'ETH', 'bitcoin', 'solana')."
        ),
        min_length=1,
        max_length=30,
    )
    asset_type: Literal["auto", "stock", "crypto"] = Field(
        default="auto",
        description="'stock' to force Alpha Vantage, 'crypto' to force CoinGecko, 'auto' to detect from the ticker (default: 'auto').",
    )
    response_format: Literal["markdown", "json"] = Field(
        default="markdown",
        description="'markdown' for human-readable output (default) or 'json' for structured data.",
    )


class GetNewsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    ticker: str = Field(
        ...,
        description="Stock ticker symbol to fetch news for (e.g. 'AAPL', 'TSLA', 'SPY', 'NVDA').",
        min_length=1,
        max_length=20,
    )
    limit: int = Field(
        default=10,
        description="Number of articles to return, from 1 to 50 (default: 10).",
        ge=1,
        le=50,
    )
    response_format: Literal["markdown", "json"] = Field(
        default="markdown",
        description="'markdown' (default) or 'json'.",
    )


class GetMacroCalendarInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days_ahead: int = Field(
        default=14,
        description="How many calendar days forward to search for scheduled releases (1–90, default: 14).",
        ge=1,
        le=90,
    )
    response_format: Literal["markdown", "json"] = Field(
        default="markdown",
        description="'markdown' (default) or 'json'.",
    )


class GetContextBundleInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    ticker: str = Field(
        ...,
        description="Stock ticker symbol to build the context bundle for (e.g. 'NVDA', 'AAPL', 'TSLA').",
        min_length=1,
        max_length=20,
    )
    news_limit: int = Field(
        default=5,
        description="Number of recent news articles to include (1–20, default: 5).",
        ge=1,
        le=20,
    )
    calendar_days: int = Field(
        default=14,
        description="Days ahead to scan for upcoming macro catalysts (1–30, default: 14).",
        ge=1,
        le=30,
    )

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("market_intel_mcp")


@mcp.tool(
    name="get_price",
    annotations={
        "title": "Get Asset Price",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_price(params: GetPriceInput) -> str:
    """Get the current price and key market data for a stock or cryptocurrency.

    For stocks, queries Alpha Vantage's Global Quote endpoint (ALPHA_VANTAGE_API_KEY required).
    For crypto, queries CoinGecko's /simple/price endpoint (no key needed for the free tier).
    Asset type is auto-detected from the ticker unless overridden.

    Args:
        params (GetPriceInput):
            - ticker (str): e.g. "AAPL", "NVDA", "BTC", "ETH", "bitcoin"
            - asset_type (str): "auto" | "stock" | "crypto" (default: "auto")
            - response_format (str): "markdown" | "json" (default: "markdown")

    Returns:
        str: Current price, daily change %, volume, high/low, and last updated time.

    Examples:
        - "NVDA stock price"             → ticker="NVDA"
        - "Bitcoin price"                → ticker="BTC" or ticker="bitcoin"
        - "ETH quote in JSON"            → ticker="ETH", response_format="json"
        - "Get price for Solana"         → ticker="SOL", asset_type="crypto"
    """
    t = params.ticker

    is_crypto = (
        params.asset_type == "crypto"
        or (params.asset_type == "auto" and _is_likely_crypto(t))
    )

    try:
        if is_crypto:
            data = await _fetch_crypto_price(t)
            if params.response_format == "json":
                return json.dumps({"asset_type": "crypto", **data}, indent=2)
            chg = data.get("change_24h_pct")
            chg_str = f"{chg:+.2f}%" if chg is not None else "N/A"
            mc = data.get("market_cap_usd")
            vol = data.get("volume_24h_usd")
            return (
                f"## {data['ticker']} / USD\n\n"
                f"| Field | Value |\n|---|---|\n"
                f"| **Price** | ${data['price_usd']:,.4f} |\n"
                f"| **24h Change** | {chg_str} |\n"
                f"| **Market Cap** | ${mc:,.0f} |\n"
                f"| **24h Volume** | ${vol:,.0f} |\n"
                f"| **Last Updated** | {data.get('last_updated_at', 'N/A')} |\n"
            )
        else:
            data = await _fetch_stock_quote(t)
            if params.response_format == ResponseFormat.JSON:
                return json.dumps({"asset_type": "stock", **data}, indent=2)
            return (
                f"## {data['ticker']} — Stock Quote\n\n"
                f"| Field | Value |\n|---|---|\n"
                f"| **Price** | ${data['price']:,.2f} |\n"
                f"| **Change** | {data['change']:+.2f} ({data['change_pct']}) |\n"
                f"| **Open** | ${data['open']:,.2f} |\n"
                f"| **High** | ${data['high']:,.2f} |\n"
                f"| **Low** | ${data['low']:,.2f} |\n"
                f"| **Volume** | {data['volume']:,} |\n"
                f"| **Prev Close** | ${data['previous_close']:,.2f} |\n"
                f"| **Latest Trading Day** | {data['latest_trading_day']} |\n"
            )
    except Exception as e:
        return _fmt_error(e)


@mcp.tool(
    name="get_news",
    annotations={
        "title": "Get News & Sentiment",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def get_news(params: GetNewsInput) -> str:
    """Fetch recent news headlines and AI-generated sentiment scores for a stock ticker.

    Powered by Alpha Vantage's News & Sentiment API. Each article includes an
    overall sentiment score and a per-ticker sentiment score in the range [-1, 1].

    Sentiment labels:
        score ≤ -0.35          → Bearish
        -0.35 < score ≤ -0.15  → Somewhat-Bearish
        -0.15 < score < 0.15   → Neutral
        0.15 ≤ score < 0.35    → Somewhat-Bullish
        score ≥ 0.35           → Bullish

    Args:
        params (GetNewsInput):
            - ticker (str): Stock ticker symbol, e.g. "AAPL", "TSLA", "SPY"
            - limit (int): Number of articles, 1–50 (default: 10)
            - response_format (str): "markdown" | "json" (default: "markdown")

    Returns:
        str: Articles with title, source, publication date, sentiment score/label, URL.

    Examples:
        - "Latest news on TSLA"            → ticker="TSLA"
        - "Top 5 bullish NVDA headlines"   → ticker="NVDA", limit=5
        - "AAPL news as JSON"              → ticker="AAPL", response_format="json"
    """
    try:
        articles = await _fetch_news(params.ticker, params.limit)
        if not articles:
            return f"No recent news found for '{params.ticker.upper()}'."

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(
                {"ticker": params.ticker.upper(), "count": len(articles), "articles": articles},
                indent=2,
            )

        lines = [f"## News & Sentiment: {params.ticker.upper()}\n", f"*{len(articles)} articles*\n"]
        for a in articles:
            ts = a.get("ticker_sentiment_score")
            ts_label = a.get("ticker_sentiment_label") or a.get("overall_sentiment_label", "")
            ts_str = f"{ts:+.3f} ({ts_label})" if ts is not None else (ts_label or "N/A")
            pub = (a.get("published_at") or "")[:8]  # YYYYMMDD
            summary = a.get("summary", "") or ""
            lines.append(f"### {a['title']}")
            lines.append(f"**{a['source']}** · {pub} · Sentiment: `{ts_str}`")
            if summary:
                lines.append(f"> {summary[:250]}{'…' if len(summary) > 250 else ''}")
            if a.get("url"):
                lines.append(f"[Read →]({a['url']})")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool(
    name="get_macro_calendar",
    annotations={
        "title": "Get Macro Economic Calendar",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_macro_calendar(params: GetMacroCalendarInput) -> str:
    """Return upcoming scheduled macroeconomic data release dates sourced from FRED.

    Covers 12 key events: CPI, NFP (jobs), GDP, PCE, retail sales, PPI, FOMC,
    jobless claims, new home sales, durable goods, consumer confidence, ISM PMI.

    All release dates are fetched concurrently from the St. Louis Fed FRED API.
    Requires FRED_API_KEY (free registration at https://fredaccount.stlouisfed.org/apikeys).

    Args:
        params (GetMacroCalendarInput):
            - days_ahead (int): Horizon in calendar days, 1–90 (default: 14)
            - response_format (str): "markdown" | "json" (default: "markdown")

    Returns:
        str: Chronologically sorted list of upcoming scheduled macro releases.

    Examples:
        - "What macro events are due this week?"    → days_ahead=7
        - "Economic calendar for the next month"    → days_ahead=30
        - "Any CPI or NFP releases coming up?"      → days_ahead=14
    """
    try:
        events = await _fetch_macro_calendar(params.days_ahead)

        # Surface a missing-key note
        if events and "_note" in events[0]:
            return events[0]["_note"]

        if not events:
            return f"No scheduled macro releases found in the next {params.days_ahead} days."

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(
                {"horizon_days": params.days_ahead, "count": len(events), "events": events},
                indent=2,
            )

        today = datetime.now(tz=timezone.utc).date()
        lines = [
            f"## Macro Economic Calendar — Next {params.days_ahead} Days\n",
            f"*From {today} · {len(events)} scheduled releases*\n",
        ]
        for e in events:
            lines.append(f"- **{e['date']}** · {e['event']}  `{e['abbr']}`")
        return "\n".join(lines)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool(
    name="get_context_bundle",
    annotations={
        "title": "Get Full Context Bundle",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def get_context_bundle(params: GetContextBundleInput) -> str:
    """Return a structured JSON bundle combining price action, company overview, recent
    news with sentiment, and upcoming macro catalysts — all in a single concurrent call.

    Designed as a pre-analysis context block before writing research notes, answering
    questions about a stock, or reasoning about trade setups. All four data sources
    are fetched concurrently to minimise latency.

    Args:
        params (GetContextBundleInput):
            - ticker (str): Stock ticker, e.g. "NVDA", "AAPL", "TSLA"
            - news_limit (int): Articles to include, 1–20 (default: 5)
            - calendar_days (int): Days ahead for macro calendar, 1–30 (default: 14)

    Returns:
        str: JSON with the following top-level keys:
            - ticker (str)
            - fetched_at (str): ISO 8601 UTC timestamp
            - price_action (dict): Live quote — price, change, volume, high/low
            - company_overview (dict): Sector, industry, description, PE, market cap,
                                       52-week range, analyst price target
            - recent_news (list[dict]): Articles with title, source, sentiment score/label
            - upcoming_catalysts (list[dict]): Scheduled FRED macro releases with dates

    Example use cases:
        - "Give me full context on NVDA before I write a note"  → ticker="NVDA"
        - "What's the macro backdrop for AAPL earnings?"        → ticker="AAPL", calendar_days=21
    """
    ticker = params.ticker.upper()

    price_coro = _fetch_stock_quote(ticker)
    overview_coro = _fetch_company_overview(ticker)
    news_coro = _fetch_news(ticker, params.news_limit)
    macro_coro = _fetch_macro_calendar(params.calendar_days)

    price_res, overview_res, news_res, macro_res = await asyncio.gather(
        price_coro, overview_coro, news_coro, macro_coro,
        return_exceptions=True,
    )

    def _safe(result: object, fallback: object) -> object:
        return fallback if isinstance(result, Exception) else result

    # Strip internal note entries from macro if FRED key is missing
    macro_events = _safe(macro_res, [])
    if isinstance(macro_events, list):
        macro_events = [e for e in macro_events if "_note" not in e]

    bundle: dict = {
        "ticker": ticker,
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "price_action": _safe(price_res, {"error": _fmt_error(price_res) if isinstance(price_res, Exception) else "unavailable"}),
        "company_overview": _safe(overview_res, {}),
        "recent_news": _safe(news_res, []),
        "upcoming_catalysts": macro_events,
    }

    return json.dumps(bundle, indent=2, default=str)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    mcp.run(transport=transport)
