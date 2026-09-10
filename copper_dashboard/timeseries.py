"""Shared time series fetchers, reused by both the backtest
(copper_dashboard/backtest.py) and the main dashboard's per-indicator charts
(app.py) so the fetch-window logic lives in exactly one place. Each fetcher
takes an explicit `years` argument (default: YEARS below) — the backtest
page lets the user adjust this per its own independent session state, while
the main dashboard always passes its own fixed value, so the two never
affect each other (same separation the Gold dashboard uses).
"""

from datetime import date, timedelta

import pandas as pd

from . import config
from . import data_sources as ds
from . import metrics
from .timeutil import today_kst

YEARS = 7
BUFFER_DAYS = 90  # extra calendar days of history fetched before the display/analysis
# start, so a 60-day SMA already has a full window on day 1 of that period.

_YFINANCE_TICKERS = {
    "dxy": ds.DXY_TICKERS,
    "wti": ds.WTI_TICKER,
    "gold": ds.GOLD_TICKER,
    "copper": ds.COPPER_TICKER,
}


def fetch_raw_series(key: str, as_of: date | None = None, years: int = YEARS) -> pd.Series:
    """Fetch one raw daily series (dxy/wti/gold/copper) covering `years` +
    BUFFER_DAYS of history ending at `as_of` (default: today, KST)."""
    end_date = as_of or today_kst()
    fetch_start = end_date - timedelta(days=years * 365 + BUFFER_DAYS)
    yf_end = end_date + timedelta(days=1)  # yfinance's `end` is exclusive

    if key in _YFINANCE_TICKERS:
        return ds.fetch_yfinance_close(_YFINANCE_TICKERS[key], start=fetch_start, end=yf_end)
    raise ValueError(f"unknown series key: {key}")


def fetch_backtest_frame(as_of: date | None = None, years: int = YEARS) -> pd.DataFrame:
    """Fetch dxy/wti/gold/copper as one date-aligned, forward-filled frame
    for the trading backtest, covering `years` of history. Different
    contracts can have slightly different trading-holiday calendars, so the
    series are joined on the union of their dates and gaps are forward-
    filled from the prior available value.
    """
    end_date = as_of or today_kst()
    dxy = fetch_raw_series("dxy", end_date, years=years)
    wti = fetch_raw_series("wti", end_date, years=years)
    gold = fetch_raw_series("gold", end_date, years=years)
    copper = fetch_raw_series("copper", end_date, years=years)

    df = pd.concat(
        [
            dxy.rename("dxy"),
            wti.rename("wti"),
            gold.rename("gold"),
            copper.rename("copper"),
        ],
        axis=1,
        join="outer",
    ).sort_index()
    df = df[df.index <= pd.Timestamp(end_date)]
    df = df.ffill()
    df = df.dropna()  # drop the leading stretch before all four series have started
    return df


# Dashboard indicator key -> the raw series id that carries its value.
_INDICATOR_SOURCE = {"dxy": "dxy", "wti": "wti"}


def build_indicator_chart_data(key: str, as_of: date | None = None, years: int = YEARS) -> dict:
    """Data for one indicator's history chart: its own daily series (trimmed
    to the last `years`), 5/30/60-day SMAs of it (skipped for
    gold_copper_ratio, which has no MA concept for this chart), and copper's
    own daily series for comparison.
    """
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=years * 365)

    copper_full = fetch_raw_series("copper", end_date, years=years)
    copper_display = copper_full[copper_full.index >= pd.Timestamp(start_date)]

    if key == "gold_copper_ratio":
        gold_full = fetch_raw_series("gold", end_date, years=years)
        combined = pd.concat([gold_full, copper_full], axis=1, keys=["gold", "copper"]).dropna()
        ratio_full = (combined["gold"] / combined["copper"]).rename("value")
        ratio_display = ratio_full[ratio_full.index >= pd.Timestamp(start_date)]
        return {"kind": "ratio", "indicator": ratio_display, "copper": copper_display, "smas": {}}

    raw_full = fetch_raw_series(_INDICATOR_SOURCE[key], end_date, years=years)
    smas_full = {window: metrics.compute_sma(raw_full, window) for window in config.MA_WINDOWS}
    indicator_display = raw_full[raw_full.index >= pd.Timestamp(start_date)]
    smas_display = {
        window: sma[sma.index >= pd.Timestamp(start_date)] for window, sma in smas_full.items()
    }
    return {"kind": "ma", "indicator": indicator_display, "copper": copper_display, "smas": smas_display}
