"""Fetches raw daily price series from free data sources (Yahoo Finance via
yfinance, and Naver's stock-price API for KODEX 구리선물(H), 138910).

Unlike the Gold project, this module has NO FRED dependency at all: real_rate
(FRED DFII10) was dropped entirely (no meaningful copper analog — see
config.py's module docstring), and both remaining indicators (DXY, FXI) plus
the international copper benchmark (HG=F) are all Yahoo Finance tickers. This
also means, unlike Gold, this project needs no FRED_API_KEY secret anywhere.
"""

import logging
import re
from datetime import date as _date

import pandas as pd
import requests
import yfinance as yf
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import config

# Naver's finance chart-data endpoint mirrors KRX's own official daily
# OHLCV for any listed ticker (stocks and ETFs alike) — a read-only JSON-ish
# GET with no authentication, unlike KRX's own data.krx.co.kr (which
# requires a logged-in session for this kind of historical query).
NAVER_SISE_JSON_URL = "https://api.finance.naver.com/siseJson.naver"
REQUEST_TIMEOUT = 60

logger = logging.getLogger(__name__)

# Max 3 attempts, waiting 5-10s between retries with exponential backoff.
_retry_network_call = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=5, min=5, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
    before_sleep=lambda state: logger.warning(
        "retrying %s after attempt %d failed: %r",
        state.fn.__name__ if state.fn else "call",
        state.attempt_number,
        state.outcome.exception() if state.outcome else None,
    ),
)


@_retry_network_call
def _download_yfinance_close(ticker: str, start, end) -> pd.Series:
    hist = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
    close = hist["Close"].dropna()
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)
    if close.empty:
        raise RuntimeError(f"no data returned for ticker {ticker!r}")
    return close.rename(ticker)


def fetch_yfinance_close(tickers, start=None, end=None) -> pd.Series:
    """Fetch daily close prices for the first working ticker in `tickers`.

    `start`/`end` (dates/strings, optional) bound the fetch window so a
    historical as-of date can be requested; `end` is exclusive, matching
    yfinance's convention. Each ticker is retried up to 3 times (5-10s
    exponential backoff) before falling through to the next candidate ticker.
    """
    if isinstance(tickers, str):
        tickers = [tickers]
    last_error = None
    for ticker in tickers:
        try:
            return _download_yfinance_close(ticker, start, end)
        except Exception as exc:  # try the next ticker candidate
            last_error = exc
            continue
    raise RuntimeError(
        f"failed to fetch data for tickers {tickers} after 3 attempts each: {last_error!r}"
    )


@_retry_network_call
def _download_naver_sise_json(symbol: str, start_time: str, end_time: str) -> str:
    resp = requests.get(
        NAVER_SISE_JSON_URL,
        params={"symbol": symbol, "requestType": 1, "startTime": start_time, "endTime": end_time, "timeframe": "day"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def fetch_krx_etf_close(symbol: str, start=None, end=None) -> pd.Series:
    """Fetch daily close prices for a KRX-listed ticker (stock or ETF, e.g.
    "138910" for KODEX 구리선물(H)) via Naver's siseJson chart-data endpoint.

    The endpoint's response isn't quite valid JSON (a bare JS array literal
    with a stray leading space and inconsistent quoting) but is close enough
    that `ast.literal_eval`-style row extraction via a small regex is more
    robust than trying to coerce it through `json.loads` directly. Returns a
    date-indexed float Series named `symbol`. `start`/`end` (dates, optional)
    bound the fetch window; both inclusive, unlike fetch_yfinance_close's
    exclusive `end` (this endpoint takes YYYYMMDD bounds directly, so there is
    no off-by-one convention to inherit from yfinance).
    """
    start_time = (start or _date(2000, 1, 1)).strftime("%Y%m%d") if hasattr(start, "strftime") else "20000101"
    end_time = (end or _date.today()).strftime("%Y%m%d") if hasattr(end, "strftime") else _date.today().strftime("%Y%m%d")

    try:
        text = _download_naver_sise_json(symbol, start_time, end_time)
    except Exception as exc:
        raise RuntimeError(f"failed to fetch Naver sise data for {symbol!r}: {exc!r}") from exc

    # Each data row looks like: ["20240102", 6845, 6845, 6725, 6780, 4237, 0.0]
    # (date, open, high, low, close, volume, foreign-ownership%) — pull out
    # just the date (group 1) and close (group 5th numeric field).
    rows = re.findall(r'\["(\d{8})",\s*[\d.]+,\s*[\d.]+,\s*[\d.]+,\s*([\d.]+),\s*[\d.]+,\s*[\d.]+\]', text)
    if not rows:
        raise RuntimeError(f"no rows parsed from Naver sise data for {symbol!r}")

    dates = pd.to_datetime([r[0] for r in rows], format="%Y%m%d")
    closes = [float(r[1]) for r in rows]
    series = pd.Series(closes, index=dates, name=symbol).sort_index()
    return series[~series.index.duplicated(keep="last")]


def fetch_krx_copper_etf_krw() -> pd.Series:
    """Fetch the full daily price history of KODEX 구리선물(H) (138910) — the
    KRW-denominated, currency-hedged, COMEX-linked ETF used as this project's
    domestic ("krx") copper price basis. See config.py's module docstring for
    why this ETF (not TIGER 구리실물, LME-based and unhedged) was chosen.
    """
    series = fetch_krx_etf_close(config.KRX_COPPER_ETF_TICKER)
    if series.empty:
        raise RuntimeError("no KODEX 구리선물(H) price data returned")
    return series.rename("krx_copper_krw")


def fetch_copx_close(start=None, end=None) -> pd.Series:
    """Fetch COPX (Global X Copper Miners ETF, USD, NYSE Arca) daily close
    prices — the reference-only "different traded instrument" comparison
    covered in COPPER_TRADING_LOGIC.md 12장, NOT part of the main copper
    price basis. Listed 2010-04-20 (config.COPX_EARLIEST_DATE, confirmed
    against yfinance's own history)."""
    return fetch_yfinance_close(config.COPX_TICKER, start=start, end=end)
