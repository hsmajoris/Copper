"""v5: the green_count (consensus-count) architecture as the permanent core
logic, replacing v2's weighted 0-100 score, plus one structural fix targeted
at a failure mode found by inspecting v4's actual trade log against real
data (see scripts/root_cause_diagnosis.py's investigation and
scripts/diagnose_v5_lag.py) — not a parameter fit to the 2023-2026 window.

What v4 (a faithful port of Gold's own engine) showed on real 2016-2026 data:
  - The DXY+WTI green_count mechanism ALONE (no ratio, no new-high trigger)
    returned +86.9% vs. v1's -9.4% and v2's -20.4% over the full 10 years —
    strong evidence the *architecture* (a consensus count, not a continuous
    weighted score) was a real part of the problem, independent of which
    indicators are used.
  - The gold/copper ratio, tested in isolation, returned -7.8% on just 2
    trades (0% win rate) over 10 years — it does not deserve a vote. Per the
    task's own instruction this is why it is REMOVED from v5 entirely
    (not just de-weighted): config.GC_RATIO_* constants are untouched but
    unused by this module.
  - Even the best isolated mechanism still lost to Buy & Hold specifically
    in 2023-2026 (-21.3% vs. +49.5%), so the win on the full/in-sample
    period was not enough — something about the out-of-sample regime beats
    even the good version of v4.

Trade-log inspection of that exact failure (see the module docstring in
scripts/root_cause_diagnosis.py's companion investigation) found ONE
concrete, reproducible mechanism, not a vague "it's just hard": in the
single largest trade of the whole backtest (entry 2025-03-31 @ $5.02, exit
2025-08-11 @ $4.42, after peaking at $5.795), copper's own close had
already crossed above its 200-day SMA on **2024-01-02 at $3.87** — over two
months and ~5% before DXY+WTI's green_count reached full 6/6 unanimity and
actually triggered the buy on 2024-03-13 at $4.05. The lag is symmetric on
the way out too: copper's close crossed back below its 200-day SMA on
2025-07-31 at $4.33, before green_count's own sell confirmed on 2025-08-11
at $4.42. In both directions, requiring TWO slow-moving, copper-external
macro proxies (a currency index and a crude-oil future) to unanimously
agree is slower than copper's own price trend, which is the whole point of
having copper's own trend as a signal in the first place (this was v2's
original, pre-this-investigation rationale for the copper_trend indicator
— the new evidence here is that it *also* explains a chunk of the
lag/give-back this diagnosis was asked to find, not that the indicator
idea itself is new).

The fix: add copper's own close-vs-200-day-SMA crossover as an INDEPENDENT
entry/exit trigger, symmetric to how Gold's own architecture already layers
an independent, faster "asset's own price" trigger (its new-high buy) on
top of the green_count core — not folded into the green_count vote itself
(adding it there would only make unanimity *harder* to reach, the opposite
of what the lag diagnosis calls for). A raw "new all-time-high" trigger
(Gold's own choice) was tested directly on copper in backtest_v4.py and
made results WORSE when combined with the rest — this uses the smoother
200-day-SMA crossover instead, which is copper's own already-established
v2 trend indicator, reused here rather than a new invented number.

This module runs the SAME single design across the full period, in-sample,
and out-of-sample once — see scripts/backtest_v5_report.py — and reports
the result honestly rather than adjusting SIGNAL_CONFIRMATION-style knobs
until 2023-2026 looks better; if it still loses to Buy & Hold there, that
is reported as-is (see the report's own conclusion section).
"""

from datetime import timedelta

import pandas as pd

from . import backtest_v4 as v4
from . import metrics

MA_WINDOWS = v4.MA_WINDOWS
GREEN_COUNT_INDICATORS = v4.GREEN_COUNT_INDICATORS  # ("dxy", "wti")
BUY_GREEN_COUNT = v4.BUY_GREEN_COUNT  # 6 (unanimity)
SELL_GREEN_COUNT = v4.SELL_GREEN_COUNT  # 0 (unanimity)
COPPER_TREND_SMA_WINDOW = 200  # reused from v2's config.COPPER_TREND_SMA_WINDOW, not a new fitted number


def compute_signals(
    df: pd.DataFrame,
    indicators: tuple[str, ...] = GREEN_COUNT_INDICATORS,
    copper_trend_sma_window: int = COPPER_TREND_SMA_WINDOW,
) -> pd.DataFrame:
    """v4's green_count (DXY+WTI unanimity) plus copper's own
    close-vs-200-day-SMA crossover as an independent trigger. No ratio, no
    raw new-high trigger — both were tested on real data and either hurt
    (new-high) or contributed nothing (ratio: -7.8%, 2 trades, 0% win rate).
    """
    df = v4.compute_signals(df, indicators=indicators)
    sma = metrics.compute_sma(df["copper"], copper_trend_sma_window)
    above = (df["copper"] > sma).fillna(False)
    df["copper_trend_sma"] = sma
    df["copper_above_trend_sma"] = above
    df["copper_trend_cross_up"] = (above & ~above.shift(1).fillna(False)).fillna(False)
    df["copper_trend_cross_down"] = ((~above) & above.shift(1).fillna(False)).fillna(False)
    return df


def _buy_reason(gc: int, buy_green_count: int, is_cross_up: bool) -> str:
    reasons = []
    if gc >= buy_green_count:
        reasons.append(f"green_count≥{buy_green_count}")
    if is_cross_up:
        reasons.append("200일선 상향돌파")
    return ", ".join(reasons)


def _sell_reason(gc: int, sell_green_count: int, is_cross_down: bool) -> str:
    reasons = []
    if gc <= sell_green_count:
        reasons.append(f"green_count≤{sell_green_count}")
    if is_cross_down:
        reasons.append("200일선 하향돌파")
    return ", ".join(reasons)


def run_backtest(
    signals_df: pd.DataFrame,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
) -> tuple[list[dict], pd.Series, pd.Series]:
    """Buy (while flat): green_count >= buy_green_count OR copper's close
    crosses above its own 200-day SMA — either fires immediately (no delay
    on the crossover; green_count can optionally be delayed via
    entry_delay_days, same plain-timer semantics as v4/Gold).

    Sell (while holding, after min_holding_days): the mirror-image condition
    on the downside. No ratio trigger, no stop loss, no position sizing —
    same binary-position philosophy as Gold's own engine and v4.
    """
    dates = signals_df.index
    copper = signals_df["copper"]
    green_count = signals_df["green_count"]
    cross_up = signals_df["copper_trend_cross_up"]
    cross_down = signals_df["copper_trend_cross_down"]

    holding = False
    entry_date = None
    entry_price = None
    entry_reason = None
    equity_at_entry = None
    running_equity = 1.0
    pending_buy_date = None
    pending_buy_reason = None
    pending_sell_date = None
    pending_sell_reason = None
    trades: list[dict] = []
    equity_values = []

    for dt in dates:
        gc = int(green_count.loc[dt])
        price = float(copper.loc[dt])
        is_cross_up = bool(cross_up.loc[dt])
        is_cross_down = bool(cross_down.loc[dt])

        if not holding:
            entry_reason_today = None
            if is_cross_up:
                # Immediate, undelayed — mirrors how Gold's own new-high
                # trigger and ratio trigger are immediate/preempting.
                entry_reason_today = _buy_reason(gc, buy_green_count, True)
                pending_buy_date = None
                pending_buy_reason = None
            else:
                if pending_buy_date is None:
                    if gc >= buy_green_count:
                        pending_buy_date = dt + timedelta(days=entry_delay_days)
                        pending_buy_reason = _buy_reason(gc, buy_green_count, False)
                if pending_buy_date is not None and dt >= pending_buy_date:
                    entry_reason_today = pending_buy_reason
                    pending_buy_date = None
                    pending_buy_reason = None

            if entry_reason_today is not None:
                holding = True
                entry_date = dt
                entry_price = price
                entry_reason = entry_reason_today
                equity_at_entry = running_equity
        else:
            exit_reason_today = None
            if (dt - entry_date).days >= min_holding_days:
                if is_cross_down:
                    exit_reason_today = _sell_reason(gc, sell_green_count, True)
                    pending_sell_date = None
                    pending_sell_reason = None
                else:
                    if pending_sell_date is None:
                        if gc <= sell_green_count:
                            pending_sell_date = dt + timedelta(days=exit_delay_days)
                            pending_sell_reason = _sell_reason(gc, sell_green_count, False)
                    if pending_sell_date is not None and dt >= pending_sell_date:
                        exit_reason_today = pending_sell_reason
                        pending_sell_date = None
                        pending_sell_reason = None

            if exit_reason_today is not None:
                exit_price = price
                running_equity = equity_at_entry * (exit_price / entry_price)
                trades.append(
                    {
                        "entry_date": entry_date,
                        "entry_price": entry_price,
                        "entry_reason": entry_reason,
                        "exit_date": dt,
                        "exit_price": exit_price,
                        "exit_reason": exit_reason_today,
                        "hold_days": (dt - entry_date).days,
                        "period_return": exit_price / entry_price - 1.0,
                        "open": False,
                    }
                )
                holding = False
                entry_date = None
                entry_price = None
                entry_reason = None
                equity_at_entry = None

        equity_values.append(equity_at_entry * (price / entry_price) if holding else running_equity)

    if holding:
        last_dt = dates[-1]
        last_price = float(copper.loc[last_dt])
        trades.append(
            {
                "entry_date": entry_date,
                "entry_price": entry_price,
                "entry_reason": entry_reason,
                "exit_date": None,
                "exit_price": last_price,
                "exit_reason": None,
                "hold_days": (last_dt - entry_date).days,
                "period_return": last_price / entry_price - 1.0,
                "open": True,
            }
        )

    equity_curve = pd.Series(equity_values, index=dates, name="strategy_equity")
    bh_equity_curve = (copper / copper.iloc[0]).rename("bh_equity")
    return trades, equity_curve, bh_equity_curve


compute_metrics = v4.compute_metrics


def simulate(signals_df: pd.DataFrame, **kwargs) -> dict:
    trades, equity_curve, bh_equity_curve = run_backtest(signals_df, **kwargs)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "metrics": compute_metrics(trades, equity_curve, bh_equity_curve),
    }
