"""Single source of truth for "is this indicator's signal copper-friendly?"

Every place that decides whether a close sits on the copper-friendly side of
its own moving average — the main dashboard's cell highlighting
(build_table.py), the backtest's green_count (backtest.py), and the main
dashboard's chart shading (app.py) — must call `copper_friendly_vs_ma()`
below rather than re-implementing the close-vs-MA comparison. This keeps
the three surfaces provably consistent instead of risking silent drift
between separately hand-written comparisons.
"""

import pandas as pd

from . import config


def copper_friendly_vs_ma(value: pd.Series, sma: pd.Series, direction: str) -> pd.Series:
    """Boolean series: True on days `value` sits on the copper-friendly side
    of its own SMA, given the indicator's correlation `direction`
    (config.CORRELATION_DIRECTION):

    - "inverse" (DXY): copper-friendly when value <= sma (at/below its own
      MA — a dollar breakout *above* its MA is bearish for copper).
    - "positive" (FXI): copper-friendly when value > sma (breakout above —
      China's equity market pricing in stronger industrial activity).
    """
    if direction == "inverse":
        return (value <= sma).fillna(False)
    return (value > sma).fillna(False)


def indicator_direction(indicator_key: str) -> str:
    return config.CORRELATION_DIRECTION[indicator_key]


def all_windows_copper_friendly_for(indicator_key: str, value: pd.Series, smas: dict) -> pd.Series:
    """AND across every {window: sma_series} in `smas` (e.g. {7: sma7, 30:
    sma30, 90: sma90}): True only on days ALL of those windows agree the
    indicator (resolving its correlation direction from config) is
    copper-friendly. This is the condition used for the main dashboard's
    per-indicator chart shading — a stricter, single-indicator condition
    than any one MA-row's cell highlight, and different from (though built
    from the same copper_friendly_vs_ma() primitive as) the backtest's
    green_count, which sums single-window flags across DXY AND FXI.
    """
    direction = indicator_direction(indicator_key)
    flags = [copper_friendly_vs_ma(value, sma, direction) for sma in smas.values()]
    result = flags[0]
    for flag in flags[1:]:
        result = result & flag
    return result
