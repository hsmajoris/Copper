"""Single source of truth for "is this indicator's signal copper-friendly?"
and for combining every indicator into the weighted 0-100 copper_friendly
score.

Every place that decides whether a daily indicator's close sits on the
copper-friendly side of its own moving average — the main table's cell
highlighting (build_table.py), the backtest (backtest.py), and the main
dashboard's chart shading (app.py) — calls `copper_friendly_vs_ma()` below
rather than re-implementing the close-vs-MA comparison, so the three
surfaces can never silently drift apart (same pattern as the Gold
dashboard's gold_friendly_vs_ma()).
"""

import pandas as pd

from . import config


def copper_friendly_vs_ma(value: pd.Series, sma: pd.Series, direction: str) -> pd.Series:
    """Boolean series: True on days `value` sits on the copper-friendly side
    of its own SMA, given the indicator's correlation `direction`
    (config.CORRELATION_DIRECTION):

    - "inverse" (DXY, gold/copper ratio, COMEX stock): copper-friendly when
      value <= sma (at/below its own MA).
    - "positive" (WTI): copper-friendly when value > sma (breakout above).
    """
    if direction == "inverse":
        return (value <= sma).fillna(False)
    return (value > sma).fillna(False)


def indicator_direction(indicator_key: str) -> str:
    return config.CORRELATION_DIRECTION[indicator_key]


def all_windows_copper_friendly_for(indicator_key: str, value: pd.Series, smas: dict) -> pd.Series:
    """AND across every {window: sma_series} in `smas`: True only on days ALL
    windows agree the indicator is copper-friendly. Used for the main
    dashboard's per-indicator chart shading.
    """
    direction = indicator_direction(indicator_key)
    flags = [copper_friendly_vs_ma(value, sma, direction) for sma in smas.values()]
    result = flags[0]
    for flag in flags[1:]:
        result = result & flag
    return result


def sma_direction_score(value: float, smas: dict[int, float], direction: str) -> int:
    """+1 / 0 / -1 direction for one daily SMA-tracked indicator (DXY, WTI),
    used as that indicator's contribution to the weighted copper_friendly
    score: +1 if `value` is on the copper-friendly side of ALL of its SMA
    windows, -1 if it's on the unfriendly side of ALL of them, 0 if the
    windows disagree (a mixed/transitional signal).
    """
    favorable_flags = []
    for sma_value in smas.values():
        if sma_value is None or pd.isna(sma_value) or value is None or pd.isna(value):
            continue
        is_friendly = value <= sma_value if direction == "inverse" else value > sma_value
        favorable_flags.append(is_friendly)
    if not favorable_flags:
        return 0
    if all(favorable_flags):
        return 1
    if not any(favorable_flags):
        return -1
    return 0


def ratio_threshold_active(ratio: pd.Series, threshold: float, comparison: str) -> pd.Series:
    """True on days the gold/copper ratio alone would trigger a threshold-
    based signal: comparison="le" for ratio <= threshold (favorable/buy),
    "ge" for ratio >= threshold (unfavorable/sell). Shared by the backtest's
    ratio trigger and the main dashboard's chart shading.
    """
    if comparison == "le":
        return (ratio <= threshold).fillna(False)
    if comparison == "ge":
        return (ratio >= threshold).fillna(False)
    raise ValueError(f"unknown comparison: {comparison}")


def ratio_direction_score(
    ratio: float,
    buy_threshold: float = config.DEFAULT_GC_RATIO_BUY_THRESHOLD,
    sell_threshold: float = config.DEFAULT_GC_RATIO_SELL_THRESHOLD,
) -> int:
    """+1 / 0 / -1 direction for the gold/copper ratio's threshold-band
    signal: +1 (favorable phase) when ratio <= buy_threshold, -1
    (unfavorable/risk-off phase) when ratio >= sell_threshold, 0 in the
    neutral band between the two thresholds.
    """
    if ratio is None or pd.isna(ratio):
        return 0
    if ratio <= buy_threshold:
        return 1
    if ratio >= sell_threshold:
        return -1
    return 0


def copper_trend_direction_score(close: float, sma: float | None) -> int:
    """+1 / -1 / 0 for copper's own price vs. its 200-day SMA (v2's new
    safety-net indicator — see config.py "copper's own price vs. its
    200-day SMA"): +1 if close is above the SMA, -1 if below, 0 if either
    value is missing (not enough history yet for a full 200-day window).
    """
    if close is None or pd.isna(close) or sma is None or pd.isna(sma):
        return 0
    return 1 if close > sma else -1


def rolling_percentile_rank(series: pd.Series, window: int) -> pd.Series:
    """For each day, the percentile rank (0-100) of that day's value within
    its own trailing `window`-day history (inclusive of itself). NaN until
    `window` days of history have accumulated.

    This is what lets the gold/copper ratio's "is this high or low" judgment
    re-center automatically as the ratio's overall level drifts over time
    (e.g. gold's 2024-2026 structural re-rating), instead of comparing
    against a fixed absolute threshold calibrated to one historical regime
    (see config.GC_RATIO_USE_ROLLING_PERCENTILE).
    """
    return series.rolling(window=window, min_periods=window).apply(
        lambda arr: (arr <= arr[-1]).mean() * 100.0, raw=True
    )


def ratio_percentile_direction_score(
    percentile: float,
    buy_percentile: float = config.GC_RATIO_PERCENTILE_BUY,
    sell_percentile: float = config.GC_RATIO_PERCENTILE_SELL,
) -> int:
    """+1 / 0 / -1 direction for the gold/copper ratio's rolling-percentile
    signal (v2): +1 (favorable phase) when the ratio sits at/below
    `buy_percentile` of its own trailing window (relatively cheap gold vs.
    copper), -1 when at/above `sell_percentile`, 0 in between or if the
    rolling window hasn't filled yet (percentile is NaN).
    """
    if percentile is None or pd.isna(percentile):
        return 0
    if percentile <= buy_percentile:
        return 1
    if percentile >= sell_percentile:
        return -1
    return 0


def dxy_rate_of_change(series: pd.Series, window: int = config.DXY_ROC_WINDOW) -> pd.Series:
    """DXY's own N-day rate of change (%, e.g. 2.5 = +2.5%), the v2
    candidate replacement for the SMA-position judgment (see
    config.DXY_SIGNAL_METHOD). NaN for the first `window` days.
    """
    return series.pct_change(periods=window) * 100.0


def roc_direction_score(roc_value: float) -> int:
    """+1 / -1 / 0 for DXY's rate-of-change signal: negative (dollar
    weakening over the window) => +1 (copper-friendly), positive => -1,
    0 on an exact tie or missing value.
    """
    if roc_value is None or pd.isna(roc_value):
        return 0
    if roc_value < 0:
        return 1
    if roc_value > 0:
        return -1
    return 0


def pmi_direction_score(value: float, threshold: float = config.PMI_FAVORABLE_THRESHOLD) -> int:
    """+1 / 0 / -1 for a PMI reading: +1 above the 50-expansion line, -1
    below it, 0 only on an exact tie (per the dashboard's chosen convention
    — PMI is treated as a simple expansion/contraction signal, not an
    own-history MA breakout, since it has no daily granularity to build an
    MA from).
    """
    if value is None or pd.isna(value):
        return 0
    if value > threshold:
        return 1
    if value < threshold:
        return -1
    return 0


def compute_copper_friendly_score(directions: dict[str, int | None]) -> dict:
    """Combine every indicator's direction (+1/0/-1, or None if stale/
    unavailable and therefore excluded) into a 0-100 score.

    `directions`: {indicator_key: direction_or_None}, keys drawn from
    config.SCORE_INDICATOR_ORDER. A None value (PMI past
    config.PMI_STALENESS_DAYS, or any indicator whose data fetch failed) is
    dropped from BOTH the raw-score numerator and the max-possible
    denominator, so the remaining indicators are reweighted rather than the
    missing one being silently scored as neutral (0) — a neutral score and
    "no data" are different things and would otherwise be indistinguishable
    in the final number.

    Returns {"score": float 0-100, "raw_score": float, "max_score": float,
    "included": [keys used], "excluded": [keys dropped]}. If every indicator
    is excluded, "score" is None (can't normalize on a zero denominator).
    """
    included, excluded = [], []
    raw_score = 0.0
    max_score = 0.0
    for key in config.SCORE_INDICATOR_ORDER:
        weight = config.WEIGHTS[key]
        direction = directions.get(key)
        if direction is None:
            excluded.append(key)
            continue
        included.append(key)
        raw_score += direction * weight
        max_score += weight

    score = None if max_score == 0 else (raw_score + max_score) / (2 * max_score) * 100.0
    return {
        "score": score,
        "raw_score": raw_score,
        "max_score": max_score,
        "included": included,
        "excluded": excluded,
    }
