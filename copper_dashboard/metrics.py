"""Moving-average and breakout-streak computations."""

import pandas as pd


def compute_sma(series: pd.Series, window_days: int) -> pd.Series:
    """Calendar-day (역일) moving average: at each date, the mean of every
    observation whose own date falls within the trailing `window_days`
    calendar days (inclusive) — not a count of the most recent N rows. Since
    the input series only has rows on days data actually exists (no
    weekend/holiday rows), a calendar-day window can contain a different
    number of observations from one date to the next (e.g. a window crossing
    a market holiday has one fewer). Pandas' offset-based rolling
    (`window="{N}D"`) handles this directly against the DatetimeIndex.

    A date is only assigned a value once `window_days` calendar days have
    actually elapsed since the series' own first date — otherwise the
    "average" would silently be computed over whatever partial history
    happens to exist yet, understating the intended lookback (the same
    warm-up guarantee the previous row-count implementation gave via
    `min_periods=window`).
    """
    rolled = series.rolling(f"{window_days}D", min_periods=1).mean()
    elapsed_days = (series.index - series.index[0]).days
    return rolled.where(elapsed_days >= window_days)


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
