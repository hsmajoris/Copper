"""Moving-average, breakout-streak, and PMI favorable-month-streak
computations.

compute_sma/breakout_streak are the same day-granularity calculations the
Gold dashboard uses for its daily indicators (DXY, WTI, gold/copper ratio
here). PMI is a monthly step series with no MA concept, so it gets its own
month-granularity streak function instead of being forced through the same
day-count logic (see README.md section "돌파 지속일수 로직").
"""

import pandas as pd


def compute_sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def breakout_streak(price: pd.Series, ma: pd.Series) -> int:
    """Number of consecutive most-recent days where price stayed above the MA.

    Resets to 0 as soon as price closes back at or below the MA.
    """
    aligned = pd.concat([price, ma], axis=1, keys=["price", "ma"]).dropna()
    if aligned.empty:
        return 0
    above = aligned["price"] > aligned["ma"]
    streak = 0
    for is_above in reversed(above.tolist()):
        if is_above:
            streak += 1
        else:
            break
    return streak


def favorable_month_streak(favorable_by_period: list[bool]) -> int:
    """Number of consecutive most-recent months a PMI reading was favorable
    (>= config.PMI_FAVORABLE_THRESHOLD), given a chronologically-ordered list
    of per-month booleans. Resets to 0 at the first unfavorable month walking
    backward from the most recent. Separate from breakout_streak because PMI
    is a monthly step series, not a daily one — counting "days since
    breakout" would misrepresent a value that only changes once a month.
    """
    streak = 0
    for is_favorable in reversed(favorable_by_period):
        if is_favorable:
            streak += 1
        else:
            break
    return streak
