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
