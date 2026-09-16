"""Shared time series fetchers, reused by both the backtest
(copper_dashboard/backtest.py) and the main dashboard's per-indicator charts
(app.py) so the fetch-window logic lives in exactly one place. Each fetcher
takes an explicit `years` argument (default: YEARS below) — the backtest page
lets the user adjust this per its own independent session state, while the
main dashboard always passes its own fixed value, so the two never affect
each other.
"""

from datetime import date, timedelta

import pandas as pd

from . import config
from . import data_sources as ds
from . import metrics
from .timeutil import today_kst

DXY_TICKERS = ["DX-Y.NYB", "^DXY", "DX=F"]
FXI_TICKERS = ["FXI"]
COPPER_TICKER = "HG=F"
YEARS = 7
BUFFER_DAYS = 110  # extra calendar days of history fetched before the display/analysis
# start, so the longest calendar-day SMA (config.MA_WINDOWS' 60-day window)
# already has a full window on day 1 of that period, with a margin over the
# bare minimum of 60.

# Raw single-series sources, keyed by a short id (not all of these are dashboard
# indicator keys — "copper" is also fetched on its own for the backtest/chart
# comparisons below).
_YFINANCE_TICKERS = {"dxy": DXY_TICKERS, "fxi": FXI_TICKERS, "copper": COPPER_TICKER}


def fetch_raw_series(
    key: str, as_of: date | None = None, years: int = YEARS, buffer_days: int = BUFFER_DAYS
) -> pd.Series:
    """Fetch one raw series (dxy/fxi/copper) covering `years` + `buffer_days`
    of history ending at `as_of` (default: today, KST). `buffer_days`
    defaults to BUFFER_DAYS (enough to warm up a 60-day SMA) but callers
    needing a longer rolling window (e.g. the backtest's 52-week trigger)
    can pass a bigger value."""
    end_date = as_of or today_kst()
    fetch_start = end_date - timedelta(days=years * 365 + buffer_days)
    yf_end = end_date + timedelta(days=1)  # yfinance's `end` is exclusive

    if key in _YFINANCE_TICKERS:
        return ds.fetch_yfinance_close(_YFINANCE_TICKERS[key], start=fetch_start, end=yf_end)
    raise ValueError(f"unknown series key: {key}")


def copper_fetch_start(as_of: date | None, years: int, buffer_days: int) -> date:
    """The (unclamped) start date a copper price fetch would use for this
    years/buffer_days window — shared by fetch_copper_price_series (which
    clamps it to KRX_COPPER_ETF_EARLIEST_DATE under the "krx" basis) and the
    UI (which uses the same, unclamped, computation to decide whether to
    show a heads-up that clamping will happen)."""
    end_date = as_of or today_kst()
    return end_date - timedelta(days=years * 365 + buffer_days)


def copper_window_would_clamp_to_krx(as_of: date | None, years: int, buffer_days: int) -> bool:
    """True if fetch_copper_price_series(..., basis=KRX) would have to clamp
    this window's start date forward to KRX_COPPER_ETF_EARLIEST_DATE (i.e.
    the requested window reaches further back than KODEX 구리선물(H) has ever
    traded)."""
    return copper_fetch_start(as_of, years, buffer_days) < config.KRX_COPPER_ETF_EARLIEST_DATE


def fetch_copper_price_series(
    as_of: date | None = None,
    years: int = YEARS,
    buffer_days: int = BUFFER_DAYS,
    basis: str = config.COPPER_PRICE_BASIS_DEFAULT,
) -> pd.Series:
    """Fetch the copper price series used for signals/MAs/P&L under the given
    basis: HG=F (USD/lb) for "intl" (the direct analog of Gold's GC=F), or
    KODEX 구리선물(H) (138910, KRW) for "krx" — a real listed, tradable
    instrument, not a currency conversion of the international price. The
    "krx" window is silently clamped forward to KRX_COPPER_ETF_EARLIEST_DATE
    if it would otherwise start before the fund existed (see
    copper_window_would_clamp_to_krx, which the UI calls with the same
    arguments to warn the user before this happens)."""
    if basis == config.COPPER_PRICE_BASIS_KRX:
        end_date = as_of or today_kst()
        fetch_start = max(copper_fetch_start(end_date, years, buffer_days), config.KRX_COPPER_ETF_EARLIEST_DATE)
        full = ds.fetch_krx_copper_etf_krw()
        series = full[(full.index >= pd.Timestamp(fetch_start)) & (full.index <= pd.Timestamp(end_date))]
        if series.empty:
            raise RuntimeError("선택한 분석 기간에 해당하는 KODEX 구리선물(H) 데이터가 없습니다.")
        return series
    return fetch_raw_series("copper", as_of, years=years, buffer_days=buffer_days)


def fetch_backtest_frame(
    as_of: date | None = None,
    years: int = YEARS,
    buffer_days: int = BUFFER_DAYS,
    copper_price_basis: str = config.COPPER_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """Fetch dxy/fxi/copper as one date-aligned, forward-filled frame for the
    trading backtest, covering `years` of history (+ `buffer_days` ahead of
    it, to warm up rolling-window signals before the display start — see
    fetch_raw_series). Different markets close on different days (US equities
    vs. KRX), so the series are joined on the union of their dates and gaps
    are forward-filled from the prior available value.

    `copper_price_basis` selects what the "copper" column (used for every
    MA/trend filter/trigger/P&L computation) actually is — see
    fetch_copper_price_series.

    Under the ② KRX basis, dxy/fxi's date index is shifted forward one
    calendar day before the join below (see the comment at that shift) to
    correct a one-day look-ahead bias: Yahoo dates DXY/FXI by the US trading
    day (session closes ~16:00 ET ≈ 05:00-06:00 KST the NEXT calendar day),
    while KODEX 구리선물(H)'s KRX close is dated by the KST trading day
    itself, which ends hours EARLIER the same day. Joining on identical date
    labels without the shift would pair a US date-D observation with the
    ETF's date-D KST close even though that US value isn't actually
    confirmed until the morning of KST date D+1 (same correction as Gold's
    own KRX basis — see gold_dashboard/timeseries.py's original comment).
    Not applicable under the ① HG=F basis, since HG=F is itself dated on the
    US trading calendar, same as dxy/fxi — no cross-timezone skew to correct
    there.
    """
    end_date = as_of or today_kst()
    dxy = fetch_raw_series("dxy", end_date, years=years, buffer_days=buffer_days)
    fxi = fetch_raw_series("fxi", end_date, years=years, buffer_days=buffer_days)
    copper = fetch_copper_price_series(end_date, years=years, buffer_days=buffer_days, basis=copper_price_basis)

    if copper_price_basis == config.COPPER_PRICE_BASIS_KRX:
        # A US date-D observation becomes usable starting KST date D+1; the
        # ffill()-after-outer-join below then naturally carries it forward
        # through any KST weekend/holiday gap to the next actual KST trading
        # day, with no separate trading-calendar lookup needed.
        dxy = dxy.set_axis(dxy.index + timedelta(days=1))
        fxi = fxi.set_axis(fxi.index + timedelta(days=1))

    df = pd.concat(
        [
            dxy.rename("dxy"),
            fxi.rename("fxi"),
            copper.rename("copper"),
        ],
        axis=1,
        join="outer",
    ).sort_index()
    df = df[df.index <= pd.Timestamp(end_date)]
    df = df.ffill()
    df = df.dropna()  # drop the leading stretch before all three series have started
    return df


# Dashboard indicator key -> the raw series id that carries its value.
_INDICATOR_SOURCE = {"dxy": "dxy", "fxi": "fxi"}


def build_indicator_chart_data(key: str, as_of: date | None = None, years: int = YEARS) -> dict:
    """Data for one indicator's history chart: its own daily series (trimmed to
    the last `years`), config.MA_WINDOWS calendar-day SMAs of it, and copper's
    own daily series for comparison.

    Each returned series keeps its own native trading-calendar dates (no
    cross-series alignment/forward-fill) since they're drawn as independent
    chart layers, not walked day-by-day like the backtest.
    """
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=years * 365)

    copper_full = fetch_raw_series("copper", end_date, years=years)
    copper_display = copper_full[copper_full.index >= pd.Timestamp(start_date)]

    raw_full = fetch_raw_series(_INDICATOR_SOURCE[key], end_date, years=years)
    smas_full = {window: metrics.compute_sma(raw_full, window) for window in config.MA_WINDOWS}
    indicator_display = raw_full[raw_full.index >= pd.Timestamp(start_date)]
    smas_display = {
        window: sma[sma.index >= pd.Timestamp(start_date)] for window, sma in smas_full.items()
    }
    return {"indicator": indicator_display, "copper": copper_display, "smas": smas_display}
