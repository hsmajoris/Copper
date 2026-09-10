"""Fetches raw daily price series (Yahoo Finance via yfinance) and attempts
best-effort scraping of the two monthly PMI headline numbers.

Design note on the PMI scrapers: every public source we found (see
README.md "PMI 스크래핑 소스") is a page whose HTML structure is NOT a
stable, versioned API — it can change without notice. So
`scrape_china_pmi()`/`scrape_us_pmi()` are deliberately best-effort: any
failure (network, parsing, structure change) is caught and turned into a
`None` return rather than raising, matching the UI's "실패 시 에러 없이
조용히 입력창을 비워두고 수기 입력으로 폴백" behavior (see app.py's PMI
refresh button).

COMEX copper warehouse stocks are NOT scraped here. The documented free
endpoint (cmegroup.com/delivery_reports/Copper_Stocks.xls) returned HTTP 403
with an explicit "prohibited under CME Group's Data Terms of Use" message
when checked — i.e. automated access to that endpoint is against the data
provider's stated terms, not merely technically inconvenient. COMEX stock is
therefore a manual-entry-only indicator (see comex_store.py), same pattern
as the PMI manual fallback but without an auto-fetch attempt.
"""

import logging
import re

import pandas as pd
import requests
import yfinance as yf
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

REQUEST_TIMEOUT = 30
DXY_TICKERS = ["DX-Y.NYB", "^DXY", "DX=F"]
COPPER_TICKER = "HG=F"
GOLD_TICKER = "GC=F"
WTI_TICKER = "CL=F"

logger = logging.getLogger(__name__)

# Max 3 attempts, waiting 5-10s between retries with exponential backoff —
# for the yfinance price fetchers only. PMI scraping uses its own single-
# attempt-per-source helper (_get_text) since a failed scrape should fall
# through to manual entry quickly, not hang the "PMI 새로고침" button click
# on repeated retries against a page whose structure may have simply changed.
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
    if close.empty:
        # Checked before touching .index.tz: on a connection failure
        # yfinance's own history() can swallow the error and return an
        # empty frame rather than raising, and an empty result's Index is a
        # plain Index (no .tz attribute at all, unlike a real DatetimeIndex)
        # — accessing .tz first would raise a confusing AttributeError
        # instead of this clear, retryable RuntimeError.
        raise RuntimeError(f"no data returned for ticker {ticker!r}")
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)
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


def fetch_gold_copper_ratio(start=None, end=None) -> pd.Series:
    """Daily gold/copper ratio computed from GC=F and HG=F closes."""
    gold = fetch_yfinance_close(GOLD_TICKER, start=start, end=end)
    copper = fetch_yfinance_close(COPPER_TICKER, start=start, end=end)
    df = pd.concat([gold, copper], axis=1, keys=["gold", "copper"]).dropna()
    return (df["gold"] / df["copper"]).rename("gold_copper_ratio")


# ---------------------------------------------------------------------------
# PMI best-effort scraping. Each function returns a dict
# {"value": float, "period": "YYYY-MM"} on success, or None on any failure —
# never raises, so a broken scraper degrades to the manual-entry UI instead
# of crashing the "PMI 새로고침" button.
# ---------------------------------------------------------------------------

_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; copper-dashboard-pmi-refresh/1.0)"
}


def _get_text(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.info("PMI scrape fetch failed for %s: %r", url, exc)
        return None


def scrape_china_pmi() -> dict | None:
    """Best-effort: TradingEconomics' China manufacturing PMI page, which
    renders the current headline figure as plain text with no login/JS
    requirement. Not a stable API — if TradingEconomics changes its markup,
    this silently returns None and the UI falls back to manual entry.
    """
    html = _get_text("https://tradingeconomics.com/china/manufacturing-pmi")
    if html is None:
        return None
    # TradingEconomics renders the latest value inside the page's headline
    # stats table; look for the first plausible PMI-range decimal near the
    # word "Manufacturing PMI".
    match = re.search(r"Manufacturing PMI[^0-9]{0,200}?(\d{2}\.\d)", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if not (20.0 <= value <= 80.0):  # sanity bound — PMI is conventionally 0-100, realistically 30-65
        return None
    return {"value": value, "period": None}


def scrape_us_pmi() -> dict | None:
    """Best-effort: TradingEconomics' US ISM/business-confidence page.
    ISM's own site requires an SSO login for the full report and is not
    scrapable; TradingEconomics mirrors the headline number for free.
    """
    html = _get_text("https://tradingeconomics.com/united-states/business-confidence")
    if html is None:
        return None
    match = re.search(r"ISM Manufacturing PMI[^0-9]{0,200}?(\d{2}\.\d)", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if not (20.0 <= value <= 80.0):
        return None
    return {"value": value, "period": None}
