"""Signal-based backtest: the weighted copper_friendly score (config.py /
signals.py — same score the main dashboard displays) drives a buy-when-
score-crosses-above / sell-when-score-crosses-below strategy on copper
(HG=F), compared against a same-period Buy & Hold benchmark.

Backtest indicators: DXY, WTI, gold/copper ratio always; China PMI / US ISM
PMI are added automatically once enough monthly history has accumulated in
pmi_history.csv (see MIN_PMI_MONTHS_FOR_BACKTEST below) — there is no free,
verified 10-year historical PMI series (see README.md "PMI 히스토리컬 데이터"),
so unlike the Gold dashboard (which had 10 years of FRED/yfinance history
for every indicator on day one), Copper's PMI backtest coverage starts thin
and grows as the live dashboard accumulates saved PMI readings month by
month — the same "start day one, grow into a full backtest input" mechanism
COMEX stock uses, just with a lower bar (12 months, not 1 year+ of daily
data) since PMI only changes once a month.

COMEX stock is excluded from the backtest entirely, per the task brief —
no free historical archive exists at all for it (not even a slow-growing
one becomes useful quickly, since a meaningful multi-year backtest needs
years, not months, of daily inventory data).
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import config
from . import metrics
from . import pmi_store
from . import signals
from . import timeseries as ts
from .timeutil import today_kst

MA_WINDOWS = [60, 30, 5]

BACKTEST_YEARS = ts.YEARS  # default analysis period; the 유효성 검증 page lets the
# user override this per-session (3-10 years) without affecting the main
# dashboard's fixed-window charts.
MIN_BACKTEST_YEARS = 3
MAX_BACKTEST_YEARS = 10
BUFFER_DAYS = ts.BUFFER_DAYS

# Below this many saved monthly readings, a PMI indicator is left out of the
# backtest's score computation (its weight is excluded from the denominator
# too, same reweighting rule signals.compute_copper_friendly_score uses for
# a stale/missing live reading).
MIN_PMI_MONTHS_FOR_BACKTEST = 12

BUY_SCORE_CUTOFF = config.SCORE_BUY_FRIENDLY_CUTOFF
SELL_SCORE_CUTOFF = config.SCORE_SELL_UNFRIENDLY_CUTOFF
GC_RATIO_BUY_THRESHOLD = config.DEFAULT_GC_RATIO_BUY_THRESHOLD
GC_RATIO_SELL_THRESHOLD = config.DEFAULT_GC_RATIO_SELL_THRESHOLD


def fetch_raw_data(as_of: date | None = None, years: int = BACKTEST_YEARS) -> pd.DataFrame:
    """Fetch dxy/wti/gold/copper as one date-aligned, forward-filled frame
    covering `years` + BUFFER_DAYS of history ending at `as_of` (default
    today, KST). Thin wrapper around the shared fetcher in timeseries.py."""
    return ts.fetch_backtest_frame(as_of, years=years)


def _pmi_daily_direction(indicator_key: str, index: pd.DatetimeIndex) -> tuple[pd.Series, bool]:
    """Forward-filled daily +1/-1 direction series for one PMI indicator,
    aligned to `index`, plus whether it has enough history
    (>= MIN_PMI_MONTHS_FOR_BACKTEST) to be included in the backtest at all.
    Days before the first saved PMI reading get direction 0 (neutral) since
    there is nothing to forward-fill from yet.
    """
    history = pmi_store.history_for_backtest(indicator_key)
    if len(history) < MIN_PMI_MONTHS_FOR_BACKTEST:
        return pd.Series(0, index=index), False
    monthly = history["value"].reindex(index, method=None)
    # Reindexing a monthly (month-start) index onto a daily index leaves
    # every non-month-start day NaN; forward-fill from the last known
    # reading, same as how the price series are day-to-day carried forward
    # across non-trading days elsewhere in this pipeline.
    combined_index = index.union(history.index)
    daily_value = history["value"].reindex(combined_index).ffill().reindex(index)
    direction = daily_value.apply(lambda v: signals.pmi_direction_score(v))
    return direction.fillna(0).astype(int), True


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Adds each indicator's +1/0/-1 direction column, the combined raw/
    normalized copper_friendly score, and the gold/copper ratio itself."""
    df = df.copy()
    df["gold_copper_ratio"] = df["gold"] / df["copper"]

    for col in ("dxy", "wti"):
        direction = signals.indicator_direction(col)
        friendly_flags = []
        for window in MA_WINDOWS:
            sma = metrics.compute_sma(df[col], window)
            friendly_flags.append(signals.copper_friendly_vs_ma(df[col], sma, direction))
        all_friendly = friendly_flags[0]
        for f in friendly_flags[1:]:
            all_friendly = all_friendly & f
        all_unfriendly = ~friendly_flags[0]
        for f in friendly_flags[1:]:
            all_unfriendly = all_unfriendly & (~f)
        df[f"{col}_direction"] = np.select([all_friendly, all_unfriendly], [1, -1], default=0)

    df["gold_copper_ratio_direction"] = np.select(
        [df["gold_copper_ratio"] <= GC_RATIO_BUY_THRESHOLD, df["gold_copper_ratio"] >= GC_RATIO_SELL_THRESHOLD],
        [1, -1],
        default=0,
    )

    active_weights = {
        "dxy": config.WEIGHTS["dxy"],
        "wti": config.WEIGHTS["wti"],
        "gold_copper_ratio": config.WEIGHTS["gold_copper_ratio"],
    }
    pmi_included = {}
    for key in ("china_pmi", "us_pmi"):
        direction_series, included = _pmi_daily_direction(key, df.index)
        df[f"{key}_direction"] = direction_series
        pmi_included[key] = included
        if included:
            active_weights[key] = config.WEIGHTS[key]

    max_score = sum(active_weights.values())
    raw_score = sum(df[f"{key}_direction"] * weight for key, weight in active_weights.items())
    df["raw_score"] = raw_score
    df["max_score"] = max_score
    df["copper_friendly_score"] = (raw_score + max_score) / (2 * max_score) * 100.0

    prior_high = df["copper"].shift(1).cummax()
    df["copper_new_high"] = (df["copper"] > prior_high).fillna(False)
    df.attrs["pmi_included"] = pmi_included
    df.attrs["active_weights"] = active_weights
    return df


def trim_to_backtest_window(
    df: pd.DataFrame, as_of: date | None = None, years: int = BACKTEST_YEARS
) -> pd.DataFrame:
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=years * 365)
    trimmed = df[df.index >= pd.Timestamp(start_date)]
    if trimmed.empty:
        raise RuntimeError("no data available in the requested backtest window")
    trimmed.attrs.update(df.attrs)
    return trimmed


def _buy_reason(score: float, buy_cutoff: float) -> str:
    return f"score≥{buy_cutoff:g}"


def _sell_reason(score: float, sell_cutoff: float) -> str:
    return f"score≤{sell_cutoff:g}"


def run_backtest(
    signals_df: pd.DataFrame,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    use_new_high_buy: bool = False,
    buy_cutoff: float = BUY_SCORE_CUTOFF,
    sell_cutoff: float = SELL_SCORE_CUTOFF,
    min_holding_days: int = 0,
) -> tuple[list[dict], pd.Series, pd.Series]:
    """Walks the signal frame day by day applying the buy/sell rules.

    Buy (while flat): copper_friendly_score >= buy_cutoff, delayed by
    `entry_delay_days` (a plain timer from the day the condition was first
    met — no reconfirmation before filling). Optionally (if
    `use_new_high_buy`), a fresh copper record high is an immediate,
    undelayed alternative buy trigger that also preempts any pending
    delayed order.

    Sell (while holding): copper_friendly_score <= sell_cutoff, delayed by
    `exit_delay_days` the same way. `min_holding_days`: no sell trigger is
    even evaluated until this many calendar days have passed since entry.

    Returns (trades, equity_curve, bh_equity_curve), both curves starting at
    1.0 on the first date.
    """
    dates = signals_df.index
    copper = signals_df["copper"]
    score = signals_df["copper_friendly_score"]
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
        s = float(score.loc[dt])
        price = float(copper.loc[dt])
        is_new_high = bool(new_high.loc[dt])

        if not holding:
            entry_reason_today = None
            if use_new_high_buy and is_new_high:
                entry_reason_today = "신고가 갱신"
                pending_buy_date = None
                pending_buy_reason = None
            else:
                if pending_buy_date is None:
                    if s >= buy_cutoff:
                        pending_buy_date = dt + timedelta(days=entry_delay_days)
                        pending_buy_reason = _buy_reason(s, buy_cutoff)
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
                if pending_sell_date is None:
                    if s <= sell_cutoff:
                        pending_sell_date = dt + timedelta(days=exit_delay_days)
                        pending_sell_reason = _sell_reason(s, sell_cutoff)
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

    return {
        "closed_trade_count": len(closed_trades),
        "win_rate": win_rate,
        "strategy_total_return": strategy_total_return,
        "bh_total_return": bh_total_return,
        "strategy_cagr": strategy_cagr,
        "bh_cagr": bh_cagr,
        "max_drawdown": max_drawdown,
        "invested_days": invested_days,
        "total_days": total_days,
        "has_open_position": open_trade is not None,
    }


def yearly_returns(equity_curve: pd.Series, bh_equity_curve: pd.Series) -> pd.DataFrame:
    years = sorted(set(equity_curve.index.year))
    rows = []
    prev_strategy = 1.0
    prev_bh = 1.0
    prev_date = equity_curve.index[0]
    for year in years:
        year_dates = equity_curve.index[equity_curve.index.year == year]
        last_date = year_dates[-1]
        days_span = max((last_date - prev_date).days, 1)
        year_end_strategy = float(equity_curve.loc[last_date])
        year_end_bh = float(bh_equity_curve.loc[last_date])

        strategy_return = year_end_strategy / prev_strategy - 1.0
        bh_return = year_end_bh / prev_bh - 1.0
        rows.append(
            {
                "year": year,
                "days_span": days_span,
                "strategy_return": strategy_return,
                "bh_return": bh_return,
                "strategy_return_annualized": (1.0 + strategy_return) ** (365.25 / days_span) - 1.0,
                "bh_return_annualized": (1.0 + bh_return) ** (365.25 / days_span) - 1.0,
            }
        )
        prev_strategy = year_end_strategy
        prev_bh = year_end_bh
        prev_date = last_date
    return pd.DataFrame(rows)


def prepare_signals(as_of: date | None = None, years: int = BACKTEST_YEARS) -> pd.DataFrame:
    raw = fetch_raw_data(as_of, years=years)
    sig = compute_signals(raw)
    return trim_to_backtest_window(sig, as_of, years=years)


def simulate(
    signals_df: pd.DataFrame,
    entry_delay_days: int = 0,
    exit_delay_days: int = 0,
    use_new_high_buy: bool = False,
    buy_cutoff: float = BUY_SCORE_CUTOFF,
    sell_cutoff: float = SELL_SCORE_CUTOFF,
    min_holding_days: int = 0,
) -> dict:
    trades, equity_curve, bh_equity_curve = run_backtest(
        signals_df,
        entry_delay_days=entry_delay_days,
        exit_delay_days=exit_delay_days,
        use_new_high_buy=use_new_high_buy,
        buy_cutoff=buy_cutoff,
        sell_cutoff=sell_cutoff,
        min_holding_days=min_holding_days,
    )
    metrics_out = compute_metrics(trades, equity_curve, bh_equity_curve)
    yearly = yearly_returns(equity_curve, bh_equity_curve)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "metrics": metrics_out,
        "yearly_returns": yearly,
    }
