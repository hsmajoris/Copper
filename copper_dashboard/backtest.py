"""Signal-based backtest: the weighted copper_friendly score (config.py /
signals.py — same score the main dashboard displays) drives a buy-when-
score-crosses-above / sell-when-score-crosses-below strategy on copper
(HG=F), compared against a same-period Buy & Hold benchmark.

Backtest indicators: copper's own 200-day trend filter, DXY, WTI, gold/copper
ratio always; China PMI / US ISM PMI are added automatically once enough
monthly history has accumulated in pmi_history.csv (see
MIN_PMI_MONTHS_FOR_BACKTEST below) — there is no free, verified 10-year
historical PMI series (see README.md "PMI 히스토리컬 데이터"), so unlike the
Gold dashboard (which had 10 years of FRED/yfinance history for every
indicator on day one), Copper's PMI backtest coverage starts thin and grows
as the live dashboard accumulates saved PMI readings month by month.

COMEX stock is excluded from the backtest entirely, per the task brief — no
free historical archive exists at all for it.

--- v2 (2026-09) ---

Every mechanism the v1-vs-v2 diagnosis called for (rolling-percentile
gold/copper ratio, the copper's-own-trend safety net, whipsaw suppression,
the DXY method choice, stop loss) is a field on `StrategyParams` below,
defaulting to the v2 config.py values but independently switchable — that's
what lets scripts/validate_v2.py turn each one on/off for the required
ablation study, and what lets `StrategyParams.v1_baseline()` reproduce the
exact v1 rules for the required "v1 vs v2" comparison, all through the same
`compute_signals`/`run_backtest` engine rather than two parallel
implementations that could silently drift apart.
"""

from dataclasses import dataclass, field
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


@dataclass
class StrategyParams:
    """Every knob the strategy can be run with, defaulting to config.py's v2
    values. Passed through compute_signals -> run_backtest as a single
    object so "what changed between two backtest runs" is always a diff of
    two StrategyParams instances, and so ablation (scripts/validate_v2.py)
    can toggle exactly one mechanism at a time without touching the rest.
    """

    buy_cutoff: float = config.SCORE_BUY_FRIENDLY_CUTOFF
    sell_cutoff: float = config.SCORE_SELL_UNFRIENDLY_CUTOFF

    # fix 1: gold/copper ratio judgment
    use_rolling_ratio: bool = config.GC_RATIO_USE_ROLLING_PERCENTILE
    gc_rolling_window: int = config.GC_RATIO_ROLLING_WINDOW
    gc_percentile_buy: float = config.GC_RATIO_PERCENTILE_BUY
    gc_percentile_sell: float = config.GC_RATIO_PERCENTILE_SELL
    gc_abs_buy_threshold: float = config.DEFAULT_GC_RATIO_BUY_THRESHOLD
    gc_abs_sell_threshold: float = config.DEFAULT_GC_RATIO_SELL_THRESHOLD

    # fix 2: copper's own trend filter + partial-exit safety net
    use_copper_trend: bool = config.COPPER_TREND_ENABLED
    copper_trend_sma_window: int = config.COPPER_TREND_SMA_WINDOW
    copper_trend_partial_exit: bool = config.COPPER_TREND_PARTIAL_EXIT_ENABLED
    copper_trend_partial_exit_fraction: float = config.COPPER_TREND_PARTIAL_EXIT_FRACTION

    # fix 3: whipsaw suppression (asymmetric cutoff band + persistence + min hold)
    use_whipsaw_suppression: bool = config.WHIPSAW_SUPPRESSION_ENABLED
    signal_confirmation_days: int = config.SIGNAL_CONFIRMATION_DAYS
    min_holding_days: int = config.MIN_HOLDING_DAYS

    # fix 4: DXY judgment method
    dxy_method: str = config.DXY_SIGNAL_METHOD  # "roc" or "sma"
    dxy_roc_window: int = config.DXY_ROC_WINDOW

    # fix 5: stop loss
    use_stop_loss: bool = config.STOP_LOSS_ENABLED
    stop_loss_pct: float = config.STOP_LOSS_PCT

    # indicator weights (only keys present are looked up; see compute_signals)
    weights: dict = field(default_factory=lambda: dict(config.WEIGHTS))

    # pre-existing, independent knobs (unrelated to the v1-vs-v2 diagnosis,
    # left as optional advanced settings on the backtest page)
    use_new_high_buy: bool = False
    entry_delay_days: int = 0
    exit_delay_days: int = 0

    @classmethod
    def v1_baseline(cls) -> "StrategyParams":
        """Reproduces the exact v1 scoring/trading rules, for the "v1 vs v2"
        comparison the task brief requires: absolute gold/copper thresholds,
        no copper-trend filter, symmetric 60/40 cutoffs, no persistence/
        min-holding/stop-loss, DXY via its own-SMA position, and v1's
        original weights (no copper_trend).
        """
        return cls(
            buy_cutoff=config.LEGACY_V1_BUY_CUTOFF,
            sell_cutoff=config.LEGACY_V1_SELL_CUTOFF,
            use_rolling_ratio=False,
            use_copper_trend=False,
            copper_trend_partial_exit=False,
            use_whipsaw_suppression=False,
            signal_confirmation_days=1,
            min_holding_days=0,
            dxy_method="sma",
            use_stop_loss=False,
            weights={
                "dxy": 1.0,
                "gold_copper_ratio": 0.8,
                "china_pmi": 0.7,
                "us_pmi": 0.7,
                "wti": 0.4,
            },
        )


def required_buffer_days(params: StrategyParams) -> int:
    """How many extra calendar days of history to fetch before the display
    window starts, so every rolling calculation (60-day SMA, the 2-year
    gold/copper percentile window, the 200-day copper trend SMA, DXY's ROC
    window — whichever of these `params` actually enables) already has a
    full window on day 1 of the requested backtest period, instead of
    silently reporting a neutral (0) direction for its first year or two.
    """
    trading_windows = list(MA_WINDOWS)
    if params.use_rolling_ratio:
        trading_windows.append(params.gc_rolling_window)
    if params.use_copper_trend:
        trading_windows.append(params.copper_trend_sma_window)
    if params.dxy_method == "roc":
        trading_windows.append(params.dxy_roc_window)
    max_window = max(trading_windows)
    return max(BUFFER_DAYS, int(max_window * 366 / 252) + 30)


def fetch_raw_data(as_of: date | None = None, years: int = BACKTEST_YEARS, buffer_days: int = BUFFER_DAYS) -> pd.DataFrame:
    """Fetch dxy/wti/gold/copper as one date-aligned, forward-filled frame
    covering `years` + `buffer_days` of history ending at `as_of` (default
    today, KST). Thin wrapper around the shared fetcher in timeseries.py."""
    return ts.fetch_backtest_frame(as_of, years=years, buffer_days=buffer_days)


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


def _own_sma_all_windows_direction(series: pd.Series, direction: str) -> pd.Series:
    """+1/0/-1 direction series: +1 where `series` sits on its own-MA
    copper-friendly side for ALL of MA_WINDOWS simultaneously, -1 where it's
    on the unfriendly side for all of them, 0 where the windows disagree.
    Shared by WTI (always) and DXY (when dxy_method="sma", i.e. v1's rule).
    """
    friendly_flags = [
        signals.copper_friendly_vs_ma(series, metrics.compute_sma(series, window), direction)
        for window in MA_WINDOWS
    ]
    all_friendly = friendly_flags[0]
    for f in friendly_flags[1:]:
        all_friendly = all_friendly & f
    all_unfriendly = ~friendly_flags[0]
    for f in friendly_flags[1:]:
        all_unfriendly = all_unfriendly & (~f)
    return np.select([all_friendly, all_unfriendly], [1, -1], default=0)


def compute_signals(df: pd.DataFrame, params: StrategyParams | None = None) -> pd.DataFrame:
    """Adds each indicator's +1/0/-1 direction column, the combined raw/
    normalized copper_friendly score, and the gold/copper ratio itself —
    all driven by `params` (defaults to the current v2 config values; pass
    StrategyParams.v1_baseline() to reproduce v1 exactly).
    """
    params = params or StrategyParams()
    df = df.copy()
    df["gold_copper_ratio"] = df["gold"] / df["copper"]

    # --- fix 4: DXY via v1's own-SMA position, or v2's rate of change ---
    if params.dxy_method == "roc":
        roc = signals.dxy_rate_of_change(df["dxy"], window=params.dxy_roc_window)
        df["dxy_roc"] = roc
        df["dxy_direction"] = np.select([roc < 0, roc > 0], [1, -1], default=0)
    else:
        df["dxy_direction"] = _own_sma_all_windows_direction(df["dxy"], signals.indicator_direction("dxy"))

    # --- WTI: unchanged own-SMA-all-windows-agree rule ---
    df["wti_direction"] = _own_sma_all_windows_direction(df["wti"], signals.indicator_direction("wti"))

    # --- fix 1: gold/copper ratio via v1's absolute thresholds, or v2's
    # rolling percentile rank ---
    if params.use_rolling_ratio:
        percentile = signals.rolling_percentile_rank(df["gold_copper_ratio"], params.gc_rolling_window)
        df["gold_copper_ratio_percentile"] = percentile
        df["gold_copper_ratio_direction"] = np.select(
            [percentile <= params.gc_percentile_buy, percentile >= params.gc_percentile_sell],
            [1, -1],
            default=0,
        )
    else:
        df["gold_copper_ratio_direction"] = np.select(
            [
                df["gold_copper_ratio"] <= params.gc_abs_buy_threshold,
                df["gold_copper_ratio"] >= params.gc_abs_sell_threshold,
            ],
            [1, -1],
            default=0,
        )

    # --- fix 2: copper's own 200-day trend filter (new indicator, also
    # drives the partial-exit safety net inside run_backtest) ---
    copper_sma = metrics.compute_sma(df["copper"], params.copper_trend_sma_window)
    df["copper_trend_sma"] = copper_sma
    df["copper_above_trend_sma"] = (df["copper"] > copper_sma).fillna(False)
    df["copper_trend_direction"] = np.select(
        [df["copper"] > copper_sma, df["copper"] <= copper_sma], [1, -1], default=0
    )

    active_weights = {
        "dxy": params.weights.get("dxy", config.WEIGHTS["dxy"]),
        "wti": params.weights.get("wti", config.WEIGHTS["wti"]),
        "gold_copper_ratio": params.weights.get("gold_copper_ratio", config.WEIGHTS["gold_copper_ratio"]),
    }
    if params.use_copper_trend:
        active_weights["copper_trend"] = params.weights.get("copper_trend", config.WEIGHTS["copper_trend"])

    pmi_included = {}
    for key in ("china_pmi", "us_pmi"):
        direction_series, included = _pmi_daily_direction(key, df.index)
        df[f"{key}_direction"] = direction_series
        pmi_included[key] = included
        if included:
            active_weights[key] = params.weights.get(key, config.WEIGHTS[key])

    max_score = sum(active_weights.values())
    raw_score = sum(df[f"{key}_direction"] * weight for key, weight in active_weights.items())
    df["raw_score"] = raw_score
    df["max_score"] = max_score
    df["copper_friendly_score"] = (raw_score + max_score) / (2 * max_score) * 100.0

    prior_high = df["copper"].shift(1).cummax()
    df["copper_new_high"] = (df["copper"] > prior_high).fillna(False)
    df.attrs["pmi_included"] = pmi_included
    df.attrs["active_weights"] = active_weights
    df.attrs["params"] = params
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
    params: StrategyParams | None = None,
) -> tuple[list[dict], pd.Series, pd.Series]:
    """Walks the signal frame day by day applying the buy/sell rules.

    Buy (while flat): copper_friendly_score >= buy_cutoff, held for
    `signal_confirmation_days` consecutive trading days if whipsaw
    suppression is on (else any single day), then delayed by
    `entry_delay_days` from the day that confirmation completed. Optionally
    (`use_new_high_buy`), a fresh copper record high is an immediate,
    undelayed, unconfirmed alternative buy trigger that also preempts any
    pending delayed order.

    Sell (while holding): the mirror-image confirmed/delayed condition on
    `sell_cutoff`, not even evaluated until `min_holding_days` TRADING days
    have passed since entry — except the stop loss below, which is checked
    unconditionally every day regardless of confirmation or min-holding.

    Stop loss (fix 5): once price falls `stop_loss_pct` (e.g. -15%) below
    entry, the entire remaining position is closed immediately, bypassing
    every other rule above. Re-entry afterwards still needs a fresh
    confirmed buy signal.

    Copper-trend partial exit (fix 2): if `use_copper_trend` and
    `copper_trend_partial_exit` are both on, a sell signal that fires while
    copper's close is still above its own 200-day SMA trims the position to
    `copper_trend_partial_exit_fraction` instead of fully closing it (once
    per holding period) — full exit still happens once copper's close drops
    below its 200-day SMA (or on a stop loss).

    Returns (trades, equity_curve, bh_equity_curve), both curves starting at
    1.0 on the first date. Each trade dict covers one full entry-to-flat
    round trip (a partial exit does not end the round trip — see its
    `partial_exits` list); `period_return` is the size-weighted blend of
    every closing price against the single entry price.
    """
    params = params or StrategyParams()
    dates = signals_df.index
    copper = signals_df["copper"]
    score = signals_df["copper_friendly_score"]
    new_high = signals_df["copper_new_high"]
    above_trend_sma = signals_df["copper_above_trend_sma"] if "copper_above_trend_sma" in signals_df else pd.Series(False, index=dates)

    confirm_days = max(1, int(params.signal_confirmation_days)) if params.use_whipsaw_suppression else 1
    min_holding_days = int(params.min_holding_days) if params.use_whipsaw_suppression else 0

    buy_raw = score >= params.buy_cutoff
    sell_raw = score <= params.sell_cutoff
    buy_confirmed = buy_raw.rolling(confirm_days, min_periods=confirm_days).sum().eq(confirm_days).fillna(False)
    sell_confirmed = sell_raw.rolling(confirm_days, min_periods=confirm_days).sum().eq(confirm_days).fillna(False)

    holding = False
    position = 0.0  # fraction of the original entry size still held
    entry_date = None
    entry_price = None
    entry_reason = None
    equity_at_entry = None
    realized_cash = 0.0
    bars_since_entry = 0
    partial_exit_done = False
    segments: list[dict] = []  # closing events (partial + final) for the currently-open round trip

    running_equity = 1.0
    pending_buy_date = None
    pending_buy_reason = None
    pending_sell_date = None
    pending_sell_reason = None

    trades: list[dict] = []
    equity_values = []

    def _close_remaining(dt, price, reason):
        nonlocal holding, position, realized_cash
        remaining = position
        realized_cash += remaining * equity_at_entry * (price / entry_price)
        segments.append({"date": dt, "price": price, "size": remaining, "reason": reason})
        total_size = sum(seg["size"] for seg in segments)
        blended_return = sum(seg["size"] * (seg["price"] / entry_price - 1.0) for seg in segments) / total_size
        trades.append(
            {
                "entry_date": entry_date,
                "entry_price": entry_price,
                "entry_reason": entry_reason,
                "exit_date": dt,
                "exit_price": price,
                "exit_reason": reason,
                "hold_days": bars_since_entry,
                "period_return": blended_return,
                "open": False,
                "partial_exits": list(segments[:-1]),
            }
        )
        holding = False
        position = 0.0
        return realized_cash

    for dt in dates:
        s = float(score.loc[dt])
        price = float(copper.loc[dt])
        is_new_high = bool(new_high.loc[dt])
        is_above_trend = bool(above_trend_sma.loc[dt]) if params.use_copper_trend else False

        if not holding:
            entry_reason_today = None
            if params.use_new_high_buy and is_new_high:
                entry_reason_today = "신고가 갱신"
                pending_buy_date = None
                pending_buy_reason = None
            else:
                if pending_buy_date is None and bool(buy_confirmed.loc[dt]):
                    pending_buy_date = dt + timedelta(days=params.entry_delay_days)
                    pending_buy_reason = _buy_reason(s, params.buy_cutoff)
                if pending_buy_date is not None and dt >= pending_buy_date:
                    entry_reason_today = pending_buy_reason
                    pending_buy_date = None
                    pending_buy_reason = None

            if entry_reason_today is not None:
                holding = True
                position = 1.0
                entry_date = dt
                entry_price = price
                entry_reason = entry_reason_today
                equity_at_entry = running_equity
                realized_cash = 0.0
                bars_since_entry = 0
                partial_exit_done = False
                segments = []
        else:
            bars_since_entry += 1
            stop_loss_hit = params.use_stop_loss and (price / entry_price - 1.0) <= params.stop_loss_pct

            if stop_loss_hit:
                running_equity = _close_remaining(dt, price, f"손절(진입가 대비 {params.stop_loss_pct:.0%})")
                pending_sell_date = None
                pending_sell_reason = None
            elif bars_since_entry >= min_holding_days:
                sell_signal_today = False
                exit_reason_today = None
                if pending_sell_date is None and bool(sell_confirmed.loc[dt]):
                    pending_sell_date = dt + timedelta(days=params.exit_delay_days)
                    pending_sell_reason = _sell_reason(s, params.sell_cutoff)
                if pending_sell_date is not None and dt >= pending_sell_date:
                    sell_signal_today = True
                    exit_reason_today = pending_sell_reason
                    pending_sell_date = None
                    pending_sell_reason = None

                if sell_signal_today:
                    trend_shield_active = params.use_copper_trend and params.copper_trend_partial_exit and is_above_trend
                    if trend_shield_active and not partial_exit_done and position > params.copper_trend_partial_exit_fraction:
                        exit_size = position - params.copper_trend_partial_exit_fraction
                        realized_cash += exit_size * equity_at_entry * (price / entry_price)
                        segments.append(
                            {
                                "date": dt,
                                "price": price,
                                "size": exit_size,
                                "reason": f"{exit_reason_today} (200일선 위 — {params.copper_trend_partial_exit_fraction:.0%} 유지)",
                            }
                        )
                        position = params.copper_trend_partial_exit_fraction
                        partial_exit_done = True
                    elif trend_shield_active:
                        pass  # already trimmed to the floor while copper stays above its 200-day SMA
                    else:
                        running_equity = _close_remaining(dt, price, exit_reason_today)

        equity_values.append(
            realized_cash + position * equity_at_entry * (price / entry_price) if holding else running_equity
        )

    if holding:
        last_dt = dates[-1]
        last_price = float(copper.loc[last_dt])
        total_size_so_far = sum(seg["size"] for seg in segments) + position
        blended_return = (
            sum(seg["size"] * (seg["price"] / entry_price - 1.0) for seg in segments)
            + position * (last_price / entry_price - 1.0)
        ) / total_size_so_far
        trades.append(
            {
                "entry_date": entry_date,
                "entry_price": entry_price,
                "entry_reason": entry_reason,
                "exit_date": None,
                "exit_price": last_price,
                "exit_reason": None,
                "hold_days": bars_since_entry,
                "period_return": blended_return,
                "open": True,
                "partial_exits": list(segments),
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

    # Sharpe ratio (annualized, 0% risk-free assumption — a simplification
    # noted here rather than silently baked in): daily strategy/BH returns
    # include flat-period zero-return days, same convention both series
    # share, so the comparison between them stays apples-to-apples.
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


def prepare_signals(
    as_of: date | None = None, years: int = BACKTEST_YEARS, params: StrategyParams | None = None
) -> pd.DataFrame:
    params = params or StrategyParams()
    raw = fetch_raw_data(as_of, years=years, buffer_days=required_buffer_days(params))
    sig = compute_signals(raw, params)
    return trim_to_backtest_window(sig, as_of, years=years)


def simulate(signals_df: pd.DataFrame, params: StrategyParams | None = None) -> dict:
    params = params or StrategyParams()
    trades, equity_curve, bh_equity_curve = run_backtest(signals_df, params)
    metrics_out = compute_metrics(trades, equity_curve, bh_equity_curve)
    yearly = yearly_returns(equity_curve, bh_equity_curve)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "metrics": metrics_out,
        "yearly_returns": yearly,
        "params": params,
    }
