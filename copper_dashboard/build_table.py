"""Assembles the full dashboard payload: fetches each indicator's series,
computes SMA breakout streaks, and formats display values."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import config
from . import data_sources as ds
from . import metrics
from . import signals
from .timeutil import today_kst

# Lookback window behind the as-of date, long enough for the 90-day calendar
# SMA plus a comfortable margin for breakout-streak history (mirrors the Gold
# dashboard's "2y" default).
LOOKBACK_DAYS = 730


def _fetch_series(key: str, start_date: date, as_of_date: date, yf_end_date: date) -> pd.Series:
    if key == "dxy":
        return ds.fetch_yfinance_close(
            ["DX-Y.NYB", "^DXY", "DX=F"], start=start_date, end=yf_end_date
        )
    if key == "fxi":
        return ds.fetch_yfinance_close("FXI", start=start_date, end=yf_end_date)
    raise ValueError(f"unknown indicator key: {key}")


def _format_value(key: str, value: float) -> str:
    meta = config.INDICATOR_META[key]
    if value is None or pd.isna(value):
        return "-"
    text = f"{value:.{meta['decimals']}f}"
    if meta["unit"] == "%":
        return f"{text}%"
    if meta["unit"] == "$":
        return f"${text}"
    return text


def build_indicator(key: str, as_of: date | None = None) -> dict:
    as_of_date = as_of if as_of is not None else today_kst()
    start_date = as_of_date - timedelta(days=LOOKBACK_DAYS)
    yf_end_date = as_of_date + timedelta(days=1)  # yfinance's `end` is exclusive

    series = _fetch_series(key, start_date, as_of_date, yf_end_date).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    series = series[series.index <= pd.Timestamp(as_of_date)]
    if series.empty:
        raise RuntimeError(f"no data available for indicator {key!r} on or before {as_of_date}")

    latest_date = series.index[-1]
    latest_value = series.iloc[-1]
    close_display = _format_value(key, latest_value)

    direction = config.CORRELATION_DIRECTION[key]

    # Row highlighting answers "is this signal actually used in a real
    # buy/sell trigger, and currently copper-friendly?" — both dxy and fxi
    # feed green_count (config.GREEN_COUNT_SIGNAL_INDICATORS), unlike Gold
    # (where WTI/VIX are reference-only and never highlight).
    sma_info = {}
    for window in config.MA_WINDOWS:
        ma = metrics.compute_sma(series, window)
        streak = metrics.breakout_streak(series, ma)
        breakout = streak > 0
        ma_value = ma.iloc[-1] if not ma.empty else None
        ma_display = (
            "-" if ma_value is None or pd.isna(ma_value) else _format_value(key, ma_value)
        )
        status_text = "상향 돌파" if breakout else "이평선 아래"

        if key not in config.GREEN_COUNT_SIGNAL_INDICATORS:
            copper_friendly = False
        else:
            copper_friendly_series = signals.copper_friendly_vs_ma(series, ma, direction)
            copper_friendly = (
                bool(copper_friendly_series.iloc[-1]) if not copper_friendly_series.empty else False
            )

        sma_info[str(window)] = {
            "ma_value": None if ma_value is None or pd.isna(ma_value) else round(float(ma_value), 4),
            "ma_display": ma_display,
            "status_text": status_text,
            "display": f"{ma_display} (종가 {close_display} → {status_text})",
            "streak": streak,
            "breakout": breakout,
            "streak_display": f"{streak}일째" if breakout else "",
            "copper_friendly": copper_friendly,
        }

    return {
        "label": config.INDICATOR_META[key]["label"],
        "source": config.INDICATOR_META[key]["source"],
        "as_of": latest_date.strftime("%Y-%m-%d"),
        "prev_close": {
            "value": round(float(latest_value), 4),
            "display": close_display,
        },
        "sma": sma_info,
    }


def build(as_of: date | None = None) -> dict:
    """Build the dashboard payload.

    `as_of`: view the table as of this date (uses each indicator's last
    close on or before that date) instead of the latest available data.
    """
    indicators = {}
    as_of_dates = []
    for key in config.INDICATOR_ORDER:
        info = build_indicator(key, as_of=as_of)
        indicators[key] = info
        as_of_dates.append(info["as_of"])

    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "requested_as_of": as_of.strftime("%Y-%m-%d") if as_of else None,
        "as_of": max(as_of_dates) if as_of_dates else None,
        "indicator_order": config.INDICATOR_ORDER,
        "indicators": indicators,
        "static_rows": config.STATIC_ROWS,
        "row_order": config.ROW_ORDER,
        "ma_windows": config.MA_WINDOWS,
        "footnotes": config.FOOTNOTES,
    }
