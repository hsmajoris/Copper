"""Assembles the full dashboard payload: fetches each indicator's series,
computes SMA breakout streaks / PMI favorable-month streaks, evaluates the
weighted copper_friendly score, and formats display values.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import comex_store
from . import config
from . import data_sources as ds
from . import metrics
from . import pmi_store
from . import signals
from .timeutil import today_kst

# Lookback window behind the as-of date, long enough for the 60-day SMA plus
# a comfortable margin for breakout-streak history (mirrors the Gold
# dashboard's "2y" default).
LOOKBACK_DAYS = 730


def _fetch_series(key: str, start_date: date, yf_end_date: date) -> pd.Series:
    if key == "dxy":
        return ds.fetch_yfinance_close(ds.DXY_TICKERS, start=start_date, end=yf_end_date)
    if key == "wti":
        return ds.fetch_yfinance_close(ds.WTI_TICKER, start=start_date, end=yf_end_date)
    if key == "gold_copper_ratio":
        return ds.fetch_gold_copper_ratio(start=start_date, end=yf_end_date)
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
    if meta["unit"]:
        return f"{text}{meta['unit']}"
    return text


def build_indicator(key: str, as_of: date | None = None) -> dict:
    as_of_date = as_of if as_of is not None else today_kst()
    start_date = as_of_date - timedelta(days=LOOKBACK_DAYS)
    yf_end_date = as_of_date + timedelta(days=1)  # yfinance's `end` is exclusive

    series = _fetch_series(key, start_date, yf_end_date).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    series = series[series.index <= pd.Timestamp(as_of_date)]
    if series.empty:
        raise RuntimeError(f"no data available for indicator {key!r} on or before {as_of_date}")

    latest_date = series.index[-1]
    latest_value = series.iloc[-1]
    close_display = _format_value(key, latest_value)

    direction = config.CORRELATION_DIRECTION[key]

    sma_info = {}
    sma_latest = {}
    for window in config.MA_WINDOWS:
        ma = metrics.compute_sma(series, window)
        streak = metrics.breakout_streak(series, ma)
        breakout = streak > 0
        ma_value = ma.iloc[-1] if not ma.empty else None
        ma_display = "-" if ma_value is None or pd.isna(ma_value) else _format_value(key, ma_value)
        status_text = "상향 돌파" if breakout else "이평선 아래"

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
        sma_latest[window] = None if ma_value is None or pd.isna(ma_value) else float(ma_value)

    # Score-contribution direction differs by indicator type: SMA-based
    # indicators (dxy/wti) use the all-3-windows-agree rule; the ratio uses
    # its own absolute buy/sell thresholds instead of its own MA (see
    # signals.py docstrings for why these are deliberately different from
    # the table's own-MA cell highlighting above).
    if key == "gold_copper_ratio":
        score_direction = signals.ratio_direction_score(float(latest_value))
    else:
        score_direction = signals.sma_direction_score(float(latest_value), sma_latest, direction)

    return {
        "label": config.INDICATOR_META[key]["label"],
        "source": config.INDICATOR_META[key]["source"],
        "as_of": latest_date.strftime("%Y-%m-%d"),
        "prev_close": {
            "value": round(float(latest_value), 4),
            "display": close_display,
        },
        "sma": sma_info,
        "weight": config.WEIGHTS[key],
        "score_direction": score_direction,
    }


def build_monthly_indicator(key: str, as_of: date | None = None) -> dict:
    """PMI card payload: latest saved value/period, staleness (days since
    saved > config.PMI_STALENESS_DAYS => excluded from score), and the
    consecutive-favorable-months streak computed from all saved history.
    """
    as_of_date = as_of if as_of is not None else today_kst()
    latest = pmi_store.latest_pmi(key)
    history = pmi_store.history_for_backtest(key)

    if latest is None:
        return {
            "label": config.INDICATOR_META[key]["label"],
            "source": config.INDICATOR_META[key]["source"],
            "has_data": False,
            "weight": config.WEIGHTS[key],
        }

    days_since = pmi_store.days_since_recorded(latest["recorded_at"], as_of_date)
    stale = days_since > config.PMI_STALENESS_DAYS

    favorable_by_period = (history["value"] >= config.PMI_FAVORABLE_THRESHOLD).tolist()
    streak_months = metrics.favorable_month_streak(favorable_by_period)

    score_direction = None if stale else signals.pmi_direction_score(latest["value"])

    return {
        "label": config.INDICATOR_META[key]["label"],
        "source": config.INDICATOR_META[key]["source"],
        "has_data": True,
        "value": latest["value"],
        "value_display": f"{latest['value']:.1f}",
        "period": latest["period"],
        "recorded_at": latest["recorded_at"],
        "days_since_recorded": days_since,
        "stale": stale,
        "favorable": latest["value"] >= config.PMI_FAVORABLE_THRESHOLD,
        "streak_months": streak_months,
        "streak_display": f"{streak_months}개월 연속 우호적" if streak_months > 0 else "",
        "weight": config.WEIGHTS[key],
        "score_direction": score_direction,
    }


def build_comex_card() -> dict:
    latest = comex_store.latest_comex()
    if latest is None:
        return {
            "label": config.INDICATOR_META["comex_copper_stock"]["label"],
            "has_data": False,
            "first_collection_date": None,
            "days_of_history": 0,
        }
    return {
        "label": config.INDICATOR_META["comex_copper_stock"]["label"],
        "has_data": True,
        "date": latest["date"],
        "tons": latest["tons"],
        "tons_display": f"{latest['tons']:,.0f}톤",
        "first_collection_date": comex_store.first_collection_date(),
        "days_of_history": comex_store.days_of_history(),
    }


def build(as_of: date | None = None) -> dict:
    """Build the dashboard payload.

    `as_of`: view the table as of this date (uses each daily indicator's
    last close on or before that date) instead of the latest available
    data. PMI/COMEX are always shown at their latest saved value regardless
    of `as_of`, since they're independently button-refreshed, not part of
    the daily as-of recompute.
    """
    indicators = {}
    as_of_dates = []
    for key in config.INDICATOR_ORDER:
        info = build_indicator(key, as_of=as_of)
        indicators[key] = info
        as_of_dates.append(info["as_of"])

    monthly_indicators = {
        key: build_monthly_indicator(key, as_of=as_of) for key in config.MONTHLY_INDICATOR_ORDER
    }
    comex = build_comex_card()

    directions = {}
    for key in config.INDICATOR_ORDER:
        directions[key] = indicators[key]["score_direction"]
    for key in config.MONTHLY_INDICATOR_ORDER:
        directions[key] = monthly_indicators[key].get("score_direction")
    score = signals.compute_copper_friendly_score(directions)

    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "requested_as_of": as_of.strftime("%Y-%m-%d") if as_of else None,
        "as_of": max(as_of_dates) if as_of_dates else None,
        "indicator_order": config.INDICATOR_ORDER,
        "indicators": indicators,
        "monthly_indicator_order": config.MONTHLY_INDICATOR_ORDER,
        "monthly_indicators": monthly_indicators,
        "comex": comex,
        "score": score,
        "static_rows": config.STATIC_ROWS,
        "row_order": config.ROW_ORDER,
        "monthly_static_rows": config.MONTHLY_STATIC_ROWS,
        "monthly_row_order": config.MONTHLY_ROW_ORDER,
        "ma_windows": config.MA_WINDOWS,
        "footnotes": config.FOOTNOTES,
    }
