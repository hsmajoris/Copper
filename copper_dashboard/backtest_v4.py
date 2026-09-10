"""v4: a line-by-line structural port of the Gold dashboard's backtest engine
(`../Gold/gold_dashboard/backtest.py`) onto copper, built to test one
specific hypothesis raised after v1/v2 both lost badly to Buy & Hold while
Gold's own backtest beat Buy & Hold handily over the same 2024-2026 window:
that the gap is architectural, not just which indicators are included.

Structural facts read directly from Gold's code (not assumed):

- Gold has NO weighted 0-100 score anywhere (`grep -rn "score\\|weight"
  gold_dashboard/*.py app.py` returns nothing). The main dashboard table and
  the backtest both use a plain **green_count**: exactly 2 indicators
  (real_rate, dxy) x 3 SMA windows (60/30/5) = 6 binary "is this side of its
  own MA gold-friendly?" flags, summed to an integer 0-6.
- Buy requires green_count >= 6 (ALL SIX flags agree) OR the gold/silver
  ratio crossing an absolute threshold OR (default ON) copper — sorry, gold
  — making a fresh all-time-high close. Sell requires green_count <= 0 (ALL
  SIX bearish) OR the ratio crossing its own threshold. There is no
  intermediate "mixed signal -> partial position" case: it is a strict
  unanimity gate, which is what lets it ride out any single indicator's
  noise instead of exiting on a partial disagreement.
- WTI and VIX are configured and shown in Gold's table, but
  `gold_dashboard/backtest.py::compute_signals` never touches them —
  `for col in ("real_rate", "dxy")` is the entire indicator list feeding
  green_count. They are display-only.
- `use_new_high_buy` defaults to **True** in Gold's own backtest page
  (`pages/1_백테스트.py` DEFAULTS) — an immediate, undelayed buy the moment
  gold sets a fresh record high, on top of (not instead of) the green_count
  mechanism. This is a plain price-momentum/breakout trigger tied to the
  asset's own price, not any macro indicator.
- There is no stop loss, no position sizing/partial exits, no persistence
  or "confirmation days" requirement anywhere in Gold's engine — v2's
  whipsaw-suppression and stop-loss machinery has no Gold analog at all.
- `breakout_streak` (metrics.py) is computed only in `build_table.py`
  (display) and is never read by `backtest.py` — it is cosmetic, not part
  of the trading logic, confirming the diagnosis in the task's point 1(b).

Two adaptations were required, not a blind copy, and are called out here
rather than silently baked in:

1. **No real_rate analog.** Gold's second green_count indicator is the real
   interest rate (opportunity cost of holding a zero-yield asset) — there is
   no economically meaningful "opportunity cost" analog for copper, an
   industrial metal. The only other DAILY series Copper already tracks
   (PMI is monthly and cannot feed a 60/30/5-day SMA breakout at all) is
   WTI, so WTI fills the second green_count slot here. This is an honest
   substitution, not a like-for-like one — Copper's own v1 literature
   review rated WTI as the *weakest*-confidence indicator, which is exactly
   why `ablation_single_indicator_green_count` below exists: to show
   whether WTI is pulling its weight in this mechanism or just adding noise.
2. **Ratio trigger direction flipped, magnitude kept.** Gold's rule is
   "ratio >= 100 -> buy, ratio <= 60 -> sell" (a high gold/silver ratio is
   gold's own bullish/momentum signal in Gold's world). Copper's own
   config.py already established the opposite, economically-correct
   direction for gold/copper (a LOW ratio, i.e. copper strong vs. gold, is
   copper-friendly — see config.CORRELATION_DIRECTION). Blindly copying
   Gold's ">= high value -> buy" direction would be backwards for copper,
   so this port keeps Gold's *mechanism* (an absolute-threshold, immediate,
   preempting trigger) but uses copper's own already-calibrated 440/620
   levels with the sign that matches copper's actual economics
   (ratio <= 440 -> buy, ratio >= 620 -> sell). Every other rule (immediate
   fill, no delay, preempts a pending green_count order) is unchanged.

Everything else — the green_count unanimity gate (6/0), the immediate
new-high buy, the total absence of a weighted score, position sizing, or a
stop loss — is copied as-is.
"""

from datetime import timedelta

import pandas as pd

from . import config
from . import metrics
from . import signals

MA_WINDOWS = [60, 30, 5]

# Direct analogs of Gold's BUY_GREEN_COUNT=6 / SELL_GREEN_COUNT=0 — 2
# indicators x 3 windows = 6 flags, unanimity required both ways.
BUY_GREEN_COUNT = 6
SELL_GREEN_COUNT = 0

# Copper's own, already-calibrated absolute thresholds (see config.py) —
# same numbers v1 used, kept here as the honest choice: not re-fit for this
# exercise. Only the *direction* of use differs from Gold's ratio (see
# module docstring, adaptation 2).
BUY_RATIO = config.DEFAULT_GC_RATIO_BUY_THRESHOLD  # buy when ratio <= this
SELL_RATIO = config.DEFAULT_GC_RATIO_SELL_THRESHOLD  # sell when ratio >= this

GREEN_COUNT_INDICATORS = ("dxy", "wti")  # see module docstring, adaptation 1


def compute_signals(df: pd.DataFrame, indicators: tuple[str, ...] = GREEN_COUNT_INDICATORS) -> pd.DataFrame:
    """Direct port of Gold's compute_signals: green_count (0 to 3*len(indicators))
    from own-MA breakout flags on `indicators`, the raw gold/copper ratio, and
    a copper_new_high flag. No weighted score, no copper's-own-trend SMA
    indicator, no PMI — none of those exist in Gold's engine either.
    """
    df = df.copy()
    gf_cols = []
    for col in indicators:
        direction = signals.indicator_direction(col)
        for window in MA_WINDOWS:
            sma = metrics.compute_sma(df[col], window)
            gf_col = f"{col}_gf_{window}"
            df[gf_col] = signals.copper_friendly_vs_ma(df[col], sma, direction)
            gf_cols.append(gf_col)
    df["green_count"] = df[gf_cols].sum(axis=1).astype(int)
    df["max_green_count"] = len(gf_cols)
    df["gold_copper_ratio"] = df["gold"] / df["copper"]
    prior_high = df["copper"].shift(1).cummax()
    df["copper_new_high"] = (df["copper"] > prior_high).fillna(False)
    return df


def _buy_reason(gc: int, r: float, buy_ratio: float, buy_green_count: int, include_new_high: bool = False) -> str:
    reasons = []
    if gc >= buy_green_count:
        reasons.append(f"green_count≥{buy_green_count}")
    if r <= buy_ratio:
        reasons.append(f"금/구리비율≤{buy_ratio:g}")
    if include_new_high:
        reasons.append("신고가 갱신")
    return ", ".join(reasons)


def _sell_reason(gc: int, r: float, sell_ratio: float, sell_green_count: int) -> str:
    reasons = []
    if gc <= sell_green_count:
        reasons.append(f"green_count≤{sell_green_count}")
    if r >= sell_ratio:
        reasons.append(f"금/구리비율≥{sell_ratio:g}")
    return ", ".join(reasons)


def run_backtest(
    signals_df: pd.DataFrame,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    use_new_high_buy: bool = True,  # Gold's own backtest-page default
    buy_ratio: float = BUY_RATIO,
    sell_ratio: float = SELL_RATIO,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
) -> tuple[list[dict], pd.Series, pd.Series]:
    """Line-by-line port of Gold's run_backtest, with the ratio comparison
    directions flipped per the module docstring's adaptation 2 (buy on
    ratio <= buy_ratio, sell on ratio >= sell_ratio, vs. Gold's >=/<=).
    Every other rule — immediate/undelayed ratio and new-high fills that
    preempt a pending green_count order, a plain delay timer with no
    reconfirmation for green_count, min_holding_days as a flat calendar-day
    gate on every sell trigger — is unchanged from Gold's own code.
    """
    dates = signals_df.index
    copper = signals_df["copper"]
    green_count = signals_df["green_count"]
    ratio = signals_df["gold_copper_ratio"]
    new_high = signals_df["copper_new_high"]

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
        r = float(ratio.loc[dt])
        price = float(copper.loc[dt])
        is_new_high = bool(new_high.loc[dt])

        if not holding:
            entry_reason_today = None
            if (use_new_high_buy and is_new_high) or r <= buy_ratio:
                entry_reason_today = _buy_reason(
                    gc, r, buy_ratio, buy_green_count, include_new_high=(use_new_high_buy and is_new_high)
                )
                pending_buy_date = None
                pending_buy_reason = None
            else:
                if pending_buy_date is None:
                    if gc >= buy_green_count:
                        pending_buy_date = dt + timedelta(days=entry_delay_days)
                        pending_buy_reason = _buy_reason(gc, r, buy_ratio, buy_green_count)
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
                if r >= sell_ratio:
                    exit_reason_today = _sell_reason(gc, r, sell_ratio, sell_green_count)
                    pending_sell_date = None
                    pending_sell_reason = None
                else:
                    if pending_sell_date is None:
                        if gc <= sell_green_count:
                            pending_sell_date = dt + timedelta(days=exit_delay_days)
                            pending_sell_reason = _sell_reason(gc, r, sell_ratio, sell_green_count)
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


def compute_metrics(trades: list[dict], equity_curve: pd.Series, bh_equity_curve: pd.Series) -> dict:
    closed_trades = [t for t in trades if not t["open"]]
    open_trade = next((t for t in trades if t["open"]), None)

    invested_days = sum(t["hold_days"] for t in trades)
    final_equity = float(equity_curve.iloc[-1])
    strategy_total_return = final_equity - 1.0
    strategy_cagr = final_equity ** (365.25 / invested_days) - 1.0 if invested_days > 0 else None

    total_days = (equity_curve.index[-1] - equity_curve.index[0]).days
    bh_final_equity = float(bh_equity_curve.iloc[-1])
    bh_total_return = bh_final_equity - 1.0
    bh_cagr = bh_final_equity ** (365.25 / total_days) - 1.0 if total_days > 0 else None

    running_max = equity_curve.cummax()
    drawdown = equity_curve / running_max - 1.0
    max_drawdown = float(drawdown.min())

    win_rate = (
        sum(1 for t in closed_trades if t["period_return"] > 0) / len(closed_trades)
        if closed_trades
        else None
    )

    def _sharpe(curve: pd.Series) -> float | None:
        daily_returns = curve.pct_change().dropna()
        if len(daily_returns) < 2 or daily_returns.std() == 0:
            return None
        return float(daily_returns.mean() / daily_returns.std() * (252 ** 0.5))

    return {
        "closed_trade_count": len(closed_trades),
        "win_rate": win_rate,
        "strategy_total_return": strategy_total_return,
        "bh_total_return": bh_total_return,
        "strategy_cagr": strategy_cagr,
        "bh_cagr": bh_cagr,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": _sharpe(equity_curve),
        "bh_sharpe_ratio": _sharpe(bh_equity_curve),
        "invested_days": invested_days,
        "total_days": total_days,
        "has_open_position": open_trade is not None,
    }


def simulate(signals_df: pd.DataFrame, **kwargs) -> dict:
    trades, equity_curve, bh_equity_curve = run_backtest(signals_df, **kwargs)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "metrics": compute_metrics(trades, equity_curve, bh_equity_curve),
    }
