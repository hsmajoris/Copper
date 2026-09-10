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

# gold_copper_ratio's v2 rolling-percentile score needs a full
# GC_RATIO_ROLLING_WINDOW trading days of its OWN history to be non-NaN on
# the as-of date — comfortably more than LOOKBACK_DAYS' calendar-day margin
# covers on its own, so this indicator gets extra lookback specifically for
# that rolling window (converting trading days to calendar days with a
# holiday margin, same conversion backtest.required_buffer_days uses).
_RATIO_LOOKBACK_DAYS = max(LOOKBACK_DAYS, int(config.GC_RATIO_ROLLING_WINDOW * 366 / 252) + 60)

# copper_trend's 200-day SMA needs the same kind of extra margin.
_COPPER_TREND_LOOKBACK_DAYS = max(LOOKBACK_DAYS, int(config.COPPER_TREND_SMA_WINDOW * 366 / 252) + 60)

_INDICATOR_LOOKBACK_DAYS = {
    "gold_copper_ratio": _RATIO_LOOKBACK_DAYS,
    "copper_trend": _COPPER_TREND_LOOKBACK_DAYS,
}


def _fetch_series(key: str, start_date: date, yf_end_date: date) -> pd.Series:
    if key == "dxy":
        return ds.fetch_yfinance_close(ds.DXY_TICKERS, start=start_date, end=yf_end_date)
    if key == "wti":
        return ds.fetch_yfinance_close(ds.WTI_TICKER, start=start_date, end=yf_end_date)
    if key == "gold_copper_ratio":
        return ds.fetch_gold_copper_ratio(start=start_date, end=yf_end_date)
    if key == "copper_trend":
        return ds.fetch_yfinance_close(ds.COPPER_TICKER, start=start_date, end=yf_end_date)
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
    lookback_days = _INDICATOR_LOOKBACK_DAYS.get(key, LOOKBACK_DAYS)
    start_date = as_of_date - timedelta(days=lookback_days)
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

    # Score-contribution direction differs by indicator type — and, since
    # v2, by a config-selectable *method* within a type — from the table's
    # own-MA cell highlighting above (see signals.py docstrings for why
    # those two concepts are deliberately kept separate):
    #   - gold_copper_ratio: v1's absolute buy/sell thresholds, or v2's
    #     rolling percentile rank (config.GC_RATIO_USE_ROLLING_PERCENTILE).
    #   - dxy: v1's all-3-windows-agree own-SMA rule, or v2's N-day rate of
    #     change (config.DXY_SIGNAL_METHOD).
    #   - wti/copper_trend: always the own-SMA rule (copper_trend has just
    #     the one 200-day window, so sma_direction_score's "all windows
    #     agree" degenerates cleanly to a single-window check).
    if key == "gold_copper_ratio":
        if config.GC_RATIO_USE_ROLLING_PERCENTILE:
            percentile = signals.rolling_percentile_rank(series, config.GC_RATIO_ROLLING_WINDOW)
            latest_percentile = percentile.iloc[-1] if not percentile.empty else None
            score_direction = signals.ratio_percentile_direction_score(latest_percentile)
        else:
            score_direction = signals.ratio_direction_score(float(latest_value))
    elif key == "dxy" and config.DXY_SIGNAL_METHOD == "roc":
        roc = signals.dxy_rate_of_change(series, window=config.DXY_ROC_WINDOW)
        latest_roc = roc.iloc[-1] if not roc.empty else None
        score_direction = signals.roc_direction_score(latest_roc)
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


def build_copper_trend_card(as_of: date | None = None) -> dict:
    """copper_trend card payload (v2's new safety-net indicator): copper's
    own close vs. its 200-day SMA. Kept separate from build_indicator()
    (rather than added to INDICATOR_ORDER) because it's a single-window
    trend check on copper itself, not a 60/30/5-day breakout table entry —
    see config.py's "copper's own price vs. its 200-day SMA" section.
    """
    as_of_date = as_of if as_of is not None else today_kst()
    lookback_days = _INDICATOR_LOOKBACK_DAYS["copper_trend"]
    start_date = as_of_date - timedelta(days=lookback_days)
    yf_end_date = as_of_date + timedelta(days=1)

    series = _fetch_series("copper_trend", start_date, yf_end_date).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    series = series[series.index <= pd.Timestamp(as_of_date)]
    if series.empty:
        raise RuntimeError(f"no copper data available for copper_trend on or before {as_of_date}")

    latest_date = series.index[-1]
    latest_value = float(series.iloc[-1])
    sma = metrics.compute_sma(series, config.COPPER_TREND_SMA_WINDOW)
    sma_value = None if sma.empty or pd.isna(sma.iloc[-1]) else float(sma.iloc[-1])
    score_direction = signals.copper_trend_direction_score(latest_value, sma_value)
    above_sma = sma_value is not None and latest_value > sma_value

    return {
        "label": config.INDICATOR_META["copper_trend"]["label"],
        "source": config.INDICATOR_META["copper_trend"]["source"],
        "as_of": latest_date.strftime("%Y-%m-%d"),
        "close": round(latest_value, 2),
        "close_display": _format_value("copper_trend", latest_value),
        "sma_value": None if sma_value is None else round(sma_value, 2),
        "sma_display": "-" if sma_value is None else _format_value("copper_trend", sma_value),
        "above_sma": above_sma,
        "status_text": f"{config.COPPER_TREND_SMA_WINDOW}일선 위 (추세 우호적)" if above_sma else f"{config.COPPER_TREND_SMA_WINDOW}일선 아래 (추세 비우호적)",
        "weight": config.WEIGHTS["copper_trend"],
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
    copper_trend = build_copper_trend_card(as_of=as_of)

    directions = {}
    for key in config.INDICATOR_ORDER:
        directions[key] = indicators[key]["score_direction"]
    directions["copper_trend"] = copper_trend.get("score_direction")
    for key in config.MONTHLY_INDICATOR_ORDER:
        directions[key] = monthly_indicators[key].get("score_direction")
    score = signals.compute_copper_friendly_score(directions)

    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "requested_as_of": as_of.strftime("%Y-%m-%d") if as_of else None,
        "as_of": max(as_of_dates) if as_of_dates else None,
        "indicator_order": config.INDICATOR_ORDER,
        "indicators": indicators,
        "copper_trend": copper_trend,
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
