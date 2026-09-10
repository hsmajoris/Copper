"""Synthetic-data correctness checks for the v2 signal-logic changes
(config.py / signals.py / backtest.py).

These deliberately do NOT hit the network (yfinance) — they build small,
hand-crafted DataFrames so the *mechanics* of each new rule (rolling
percentile, stop loss, partial exit, whipsaw suppression, DXY ROC, and that
StrategyParams.v1_baseline() reproduces v1) can be checked deterministically
in any environment, including one with no market-data access. The real
numeric backtest (real 10-year history, in/out-of-sample split, ablation)
is scripts/validate_v2.py, which does need network access.

Run with: python tests/test_v2_signal_logic.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from copper_dashboard import backtest, config, signals

FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def make_signals_df(dates, copper, score, above_trend=None, new_high=None) -> pd.DataFrame:
    idx = pd.DatetimeIndex(dates)
    df = pd.DataFrame(
        {
            "copper": copper,
            "copper_friendly_score": score,
            "copper_above_trend_sma": above_trend if above_trend is not None else [False] * len(idx),
            "copper_new_high": new_high if new_high is not None else [False] * len(idx),
        },
        index=idx,
    )
    return df


def business_dates(n, start="2020-01-01"):
    return pd.bdate_range(start=start, periods=n)


# ---------------------------------------------------------------------------
# fix 1: rolling percentile rank
# ---------------------------------------------------------------------------
def test_rolling_percentile_rank():
    series = pd.Series(range(1, 11), dtype=float)  # 1..10, strictly increasing
    result = signals.rolling_percentile_rank(series, window=5)
    # first 4 values NaN (window not filled), 5th value (index 4, value=5) is
    # the max of [1,2,3,4,5] => percentile 100
    check("rolling_percentile_rank: warmup is NaN", result.iloc[:4].isna().all())
    check("rolling_percentile_rank: max-of-window -> 100", result.iloc[4] == 100.0)
    # index 9 (value=10) is the max of [6,7,8,9,10] => 100 again (still increasing)
    check("rolling_percentile_rank: still increasing -> 100", result.iloc[9] == 100.0)

    # a series that drops back down: window of 5 constant then one low value
    dropped = pd.Series([10, 10, 10, 10, 1], dtype=float)
    pct = signals.rolling_percentile_rank(dropped, window=5).iloc[-1]
    # 1 is <= itself only (1 out of 5) => 20.0
    check("rolling_percentile_rank: lowest value in window -> 20.0", pct == 20.0, f"got {pct}")


def test_ratio_percentile_direction_score():
    check("percentile<=30 -> +1", signals.ratio_percentile_direction_score(10.0) == 1)
    check("percentile==30 -> +1 (inclusive)", signals.ratio_percentile_direction_score(30.0) == 1)
    check("percentile>=70 -> -1", signals.ratio_percentile_direction_score(90.0) == -1)
    check("percentile in between -> 0", signals.ratio_percentile_direction_score(50.0) == 0)
    check("percentile NaN -> 0", signals.ratio_percentile_direction_score(float("nan")) == 0)


# ---------------------------------------------------------------------------
# fix 2: copper trend direction
# ---------------------------------------------------------------------------
def test_copper_trend_direction_score():
    check("close above sma -> +1", signals.copper_trend_direction_score(110.0, 100.0) == 1)
    check("close below sma -> -1", signals.copper_trend_direction_score(90.0, 100.0) == -1)
    check("sma missing -> 0", signals.copper_trend_direction_score(110.0, None) == 0)


# ---------------------------------------------------------------------------
# fix 4: DXY rate of change
# ---------------------------------------------------------------------------
def test_dxy_roc():
    series = pd.Series([100.0] * 10 + [90.0])  # a 10% drop after a flat run
    roc = signals.dxy_rate_of_change(series, window=10)
    last_roc = roc.iloc[-1]
    check("dxy roc: 10% drop over window -> roc ~ -10", abs(last_roc - (-10.0)) < 1e-9, f"got {last_roc}")
    check("roc_direction_score: negative roc -> +1 (copper-friendly)", signals.roc_direction_score(last_roc) == 1)
    check("roc_direction_score: positive roc -> -1", signals.roc_direction_score(5.0) == -1)
    check("roc_direction_score: NaN -> 0", signals.roc_direction_score(float("nan")) == 0)


# ---------------------------------------------------------------------------
# fix 5: stop loss
# ---------------------------------------------------------------------------
def test_stop_loss():
    dates = business_dates(6)
    # entry triggers day0 (score>=65), then price craters well past -15% by day2;
    # score drops below the buy cutoff right after so the stop-out isn't
    # immediately followed by a fresh re-entry (which would also be correct
    # engine behavior, just not what this test is isolating).
    copper = [100.0, 95.0, 80.0, 78.0, 77.0, 76.0]
    score = [70.0, 70.0, 70.0, 30.0, 30.0, 30.0]
    df = make_signals_df(dates, copper, score)
    params = backtest.StrategyParams(
        use_whipsaw_suppression=False,
        use_copper_trend=False,
        use_rolling_ratio=False,
        use_stop_loss=True,
        stop_loss_pct=-0.15,
    )
    trades, equity, bh = backtest.run_backtest(df, params)
    check("stop loss: exactly one closed trade", len(trades) == 1 and not trades[0]["open"], f"trades={trades}")
    if trades:
        t = trades[0]
        check("stop loss: exit reason mentions 손절", "손절" in (t["exit_reason"] or ""), t["exit_reason"])
        check("stop loss: exit price is the day it first breached -15%", t["exit_price"] == 80.0, t)
        check(
            "stop loss: period_return matches (80/100 - 1)",
            abs(t["period_return"] - (80.0 / 100.0 - 1.0)) < 1e-9,
            t,
        )


# ---------------------------------------------------------------------------
# fix 2: partial exit while above the 200-day trend SMA
# ---------------------------------------------------------------------------
def test_partial_exit_above_trend():
    dates = business_dates(8)
    copper = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 112.0, 114.0]
    # buy on day0, sell-cutoff hit from day1 onward (score stays <=35), but
    # copper stays above its 200-day SMA the whole time -> should trim to
    # 50% once, then keep holding at 50% (never fully exit) since price
    # never drops below the (synthetic) trend SMA in this test.
    score = [70.0] + [20.0] * 7
    above_trend = [True] * 8
    df = make_signals_df(dates, copper, score, above_trend=above_trend)
    params = backtest.StrategyParams(
        use_whipsaw_suppression=False,
        use_stop_loss=False,
        use_rolling_ratio=False,
        use_copper_trend=True,
        copper_trend_partial_exit=True,
        copper_trend_partial_exit_fraction=0.5,
    )
    trades, equity, bh = backtest.run_backtest(df, params)
    check("partial exit: position never fully closes", len(trades) == 1 and trades[0]["open"], trades)
    if trades:
        t = trades[0]
        check("partial exit: one partial-exit event recorded", len(t["partial_exits"]) == 1, t["partial_exits"])
        # equity on the last day: 50% realized at the partial-exit price,
        # 50% still mark-to-market at the final price, both vs entry=100
        partial_price = t["partial_exits"][0]["price"]
        expected_last_equity = 0.5 * (partial_price / 100.0) + 0.5 * (114.0 / 100.0)
        check(
            "partial exit: final equity matches blended 50/50 calc",
            abs(float(equity.iloc[-1]) - expected_last_equity) < 1e-9,
            f"got {equity.iloc[-1]}, expected {expected_last_equity}",
        )


def test_full_exit_once_below_trend():
    dates = business_dates(6)
    copper = [100.0, 95.0, 90.0, 85.0, 80.0, 75.0]
    score = [70.0] + [20.0] * 5
    above_trend = [True, True, False, False, False, False]  # drops below trend SMA on day2
    df = make_signals_df(dates, copper, score, above_trend=above_trend)
    params = backtest.StrategyParams(
        use_whipsaw_suppression=False,
        use_stop_loss=False,
        use_rolling_ratio=False,
        use_copper_trend=True,
        copper_trend_partial_exit=True,
        copper_trend_partial_exit_fraction=0.5,
    )
    trades, equity, bh = backtest.run_backtest(df, params)
    check("full exit after trend breaks: exactly one closed trade", len(trades) == 1 and not trades[0]["open"], trades)
    if trades:
        t = trades[0]
        check("full exit after trend breaks: one partial + one final", len(t["partial_exits"]) == 1, t)
        check("full exit after trend breaks: exit price is day2's close (90)", t["exit_price"] == 90.0, t)


# ---------------------------------------------------------------------------
# fix 3: whipsaw suppression reduces trade count on a noisy score series
# ---------------------------------------------------------------------------
def test_whipsaw_suppression_reduces_trades():
    n = 120
    dates = business_dates(n)
    rng = np.random.default_rng(7)
    # a noisy score oscillating around the 35-65 band so it crosses back and
    # forth constantly on single-day spikes
    score = 50 + rng.normal(0, 12, size=n)
    score = np.clip(score, 0, 100)
    copper = 100 + np.cumsum(rng.normal(0, 0.3, size=n))
    df = make_signals_df(dates, copper, score)

    noisy_params = backtest.StrategyParams(
        use_whipsaw_suppression=False,
        use_stop_loss=False,
        use_copper_trend=False,
        use_rolling_ratio=False,
        buy_cutoff=config.LEGACY_V1_BUY_CUTOFF,
        sell_cutoff=config.LEGACY_V1_SELL_CUTOFF,
    )
    suppressed_params = backtest.StrategyParams(
        use_whipsaw_suppression=True,
        signal_confirmation_days=3,
        min_holding_days=10,
        use_stop_loss=False,
        use_copper_trend=False,
        use_rolling_ratio=False,
        buy_cutoff=config.SCORE_BUY_FRIENDLY_CUTOFF,
        sell_cutoff=config.SCORE_SELL_UNFRIENDLY_CUTOFF,
    )
    noisy_trades, _, _ = backtest.run_backtest(df, noisy_params)
    suppressed_trades, _, _ = backtest.run_backtest(df, suppressed_params)
    check(
        "whipsaw suppression: fewer round trips than the raw 60/40 crossing rule",
        len(suppressed_trades) < len(noisy_trades),
        f"suppressed={len(suppressed_trades)} raw={len(noisy_trades)}",
    )


# ---------------------------------------------------------------------------
# StrategyParams.v1_baseline() matches the original (pre-v2) simple
# buy-and-hold-until-sell-cutoff behavior with no stop loss / partial exit
# ---------------------------------------------------------------------------
def test_v1_baseline_simple_round_trip():
    dates = business_dates(5)
    copper = [100.0, 110.0, 120.0, 60.0, 60.0]  # a huge, unrealistic drop with NO stop loss in v1
    score = [65.0, 65.0, 65.0, 30.0, 30.0]
    df = make_signals_df(dates, copper, score)
    params = backtest.StrategyParams.v1_baseline()
    trades, equity, bh = backtest.run_backtest(df, params)
    check("v1 baseline: exactly one closed trade", len(trades) == 1 and not trades[0]["open"], trades)
    if trades:
        t = trades[0]
        # v1 has no stop loss, so even a huge intra-trade drawdown does not
        # exit early — it rides all the way down to the sell-cutoff day.
        check("v1 baseline: no early stop-loss exit (rides to sell cutoff)", t["exit_price"] == 60.0, t)
        check("v1 baseline: no partial exits recorded", len(t["partial_exits"]) == 0, t)


def test_compute_metrics_has_sharpe():
    dates = business_dates(30)
    copper = 100 + np.cumsum(np.random.default_rng(1).normal(0, 1, size=30))
    score = np.full(30, 70.0)
    df = make_signals_df(dates, copper, score)
    trades, equity, bh = backtest.run_backtest(df, backtest.StrategyParams())
    m = backtest.compute_metrics(trades, equity, bh)
    check("compute_metrics: sharpe_ratio key present", "sharpe_ratio" in m)
    check("compute_metrics: bh_sharpe_ratio key present", "bh_sharpe_ratio" in m)


def test_compute_signals_smoke():
    n = 700  # > 504-day rolling window + 200-day SMA + margin
    dates = business_dates(n)
    rng = np.random.default_rng(3)
    raw = pd.DataFrame(
        {
            "dxy": 100 + np.cumsum(rng.normal(0, 0.2, size=n)),
            "wti": 70 + np.cumsum(rng.normal(0, 0.3, size=n)),
            "gold": 2000 + np.cumsum(rng.normal(0, 5, size=n)),
            "copper": 4 + np.cumsum(rng.normal(0, 0.02, size=n)),
        },
        index=dates,
    )

    v2_df = backtest.compute_signals(raw, backtest.StrategyParams())
    check("compute_signals v2: score is within [0, 100]", v2_df["copper_friendly_score"].between(0, 100).all())
    check(
        "compute_signals v2: active weights include copper_trend",
        "copper_trend" in v2_df.attrs["active_weights"],
    )
    check(
        "compute_signals v2: percentile column present when rolling ratio enabled",
        "gold_copper_ratio_percentile" in v2_df.columns,
    )
    check(
        "compute_signals v2: max_score matches sum of v2 weights (dxy+wti+ratio+copper_trend)",
        abs(v2_df["max_score"].iloc[-1] - (0.9 + 0.3 + 0.8 + 1.2)) < 1e-9,
        v2_df["max_score"].iloc[-1],
    )

    v1_df = backtest.compute_signals(raw, backtest.StrategyParams.v1_baseline())
    check("compute_signals v1: score is within [0, 100]", v1_df["copper_friendly_score"].between(0, 100).all())
    check(
        "compute_signals v1: active weights exclude copper_trend",
        "copper_trend" not in v1_df.attrs["active_weights"],
    )
    check(
        "compute_signals v1: max_score matches sum of v1 weights (dxy+wti+ratio)",
        abs(v1_df["max_score"].iloc[-1] - (1.0 + 0.4 + 0.8)) < 1e-9,
        v1_df["max_score"].iloc[-1],
    )
    check(
        "compute_signals v1: no rolling-percentile column (legacy threshold path)",
        "gold_copper_ratio_percentile" not in v1_df.columns,
    )


def main():
    tests = [
        test_rolling_percentile_rank,
        test_ratio_percentile_direction_score,
        test_copper_trend_direction_score,
        test_dxy_roc,
        test_stop_loss,
        test_partial_exit_above_trend,
        test_full_exit_once_below_trend,
        test_whipsaw_suppression_reduces_trades,
        test_v1_baseline_simple_round_trip,
        test_compute_metrics_has_sharpe,
        test_compute_signals_smoke,
    ]
    for t in tests:
        t()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
