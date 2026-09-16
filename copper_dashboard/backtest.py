"""Signal-based backtest: DXY/FXI MA breakout signals (green_count) and a
52-week new-high/new-low breakout, compared against a same-period Buy & Hold
benchmark.

This is a direct structural port of the Gold dashboard's backtest.py — same
5-stage pipeline (fetch_raw_data -> compute_signals -> trim_to_backtest_window
-> run_backtest -> compute_metrics), same green_count mechanic, same 52-week
triggers, same symmetric noise filters, same fee model. See
COPPER_TRADING_LOGIC.md for the full design writeup and 9장 for exactly what
was changed vs. Gold and why.

Every moving-average window in this module (green_count's dxy/fxi SMAs, the
noise filters' shared long-term SMA) is a calendar-day (역일) window, not
a trading-day count — see metrics.compute_sma. The day-to-day state machine in
run_backtest() itself (minimum holding period, the noise filters' D0+N check)
is likewise calendar-day based.

Buy (while flat): green_count >= BUY_GREEN_COUNT OR a fresh 52-week high
  (copper_new_52w_high)
Sell (while holding): green_count == SELL_GREEN_COUNT OR a fresh 52-week low
  (copper_new_52w_low)

All fills happen at the signal day's own close, immediately — there is no
delay/lag setting for any trigger (subject to the noise filters below
deferring execution pending confirmation).
"""

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from . import config
from . import metrics
from . import signals
from . import timeseries as ts
from .timeutil import today_kst

# Shared with the main dashboard's own MA columns/highlighting/chart shading
# (config.py) so both can never drift apart — see compute_signals below.
MA_WINDOWS = config.MA_WINDOWS

MIN_BACKTEST_YEARS = 1
MAX_BACKTEST_YEARS = 15
# Default analysis period a fresh session starts on; the 유효성 검증 page lets
# the user override this per-session (1-15 years) without affecting the main
# dashboard's own fixed-window charts (app.py's CHART_YEARS, unrelated to
# this). Deliberately independent of timeseries.YEARS. Deliberately NOT tied
# to MAX_BACKTEST_YEARS (raising the max shouldn't silently raise the default
# a fresh session starts on).
BACKTEST_YEARS = 10
# Extra calendar days of history fetched before the analysis start. The
# noise filters' LONG_TREND_WINDOW-day (calendar) SMA would only need 180
# calendar days minimum, but the 52-week new-high/new-low triggers' own
# FIFTY_TWO_WEEK_WINDOW_DAYS (365) window is the larger, binding requirement
# — deliberately independent of timeseries.BUFFER_DAYS, which only needs to
# cover MA_WINDOWS' own max (90 days) for the main dashboard's per-indicator
# chart fetches.
BUFFER_DAYS = 430

# green_count now sums 2 indicators (dxy, fxi) x 3 windows (90/30/7 calendar
# days) = 6 cells, same 0-6 range and same unanimity defaults as Gold's
# real_rate+dxy pair (see COPPER_TRADING_LOGIC.md 3장/11장 for why this
# starts at Gold's own defaults rather than a freshly-tuned number — the
# task brief asked for these to start identical and be adjusted later, if
# at all, from real backtest results, not guessed up front).
GREEN_COUNT_INDICATORS = ("dxy", "fxi")
BUY_GREEN_COUNT = 6
SELL_GREEN_COUNT = 0
# No minimum holding period by default: a qualifying sell (green_count,
# 52-week-low, or the sell-noise filter's own D0+N resolution) can fire the
# day after entry. User-adjustable — raise this to simulate a longer-horizon
# strategy that ignores sell triggers for a while after buying.
DEFAULT_MIN_HOLDING_DAYS = 0

# copper's own long-term SMA, shared by the sell- and buy-signal noise
# filters below as the "in a clear uptrend/downtrend" gate they compare
# price against — one shared column (copper_sma_long), two independent
# consumers.
LONG_TREND_WINDOW = 180  # 6 calendar months

# 52-week new-high/new-low breakout: independent buy/sell triggers (OR'd in
# alongside green_count on the buy side; OR'd in alongside green_count on the
# sell side). Each fires only on the day copper's close is strictly above
# (new-high) or below (new-low) the highest/lowest close of the prior
# FIFTY_TWO_WEEK_WINDOW_DAYS calendar days (a fresh breakout, not "currently
# at/above/below the 52-week high/low" — see compute_signals).
FIFTY_TWO_WEEK_WINDOW_DAYS = 365
DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER = True
DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER = True

# Sell-signal noise filter: while copper is well above its LONG_TREND_WINDOW-
# day (calendar) SMA (a possible sign the sell signal is a blip in an ongoing
# uptrend rather than a genuine reversal), a qualifying sell signal is ignored
# and a 7-calendar-day "wait and see" period starts instead of executing it
# immediately. See run_backtest's docstring for the exact mechanism.
DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT = 5.0

# Buy-signal noise filter — the exact mirror image of the sell-signal filter
# above, direction flipped. See run_backtest's docstring for the exact
# mechanism.
DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT = 5.0

# Exit/entry confirmation, method ① (default) — "매일 갱신 2시그마 밴드": every
# trading day of the observation window gets its OWN, wider confirmation
# threshold instead of one fixed checkpoint. Shared, direction-agnostic math
# for BOTH the sell- and buy-signal noise filters (only the comparison
# direction differs — see noise_band_pct and run_backtest).
#
# Derivation (√t rule for a random walk's cumulative volatility):
#   daily_vol   = monthly_vol / sqrt(trading_days_per_month)
#   band(t)     = sigma_multiplier * daily_vol * sqrt(t)     (t = trading
#                 days elapsed since D0, 1..max)
# With monthly_vol = 7.11% (copper's own ~20-year historical monthly
# volatility, computed directly from HG=F — see COPPER_TRADING_LOGIC.md
# 9장/5장 for the calculation; Gold's own equivalent constant uses gold's own
# ~30-year monthly vol of 4.9%, notably lower — copper is a more volatile,
# cyclicality-driven industrial metal, not a safe-haven asset), trading_days_
# per_month = 21, sigma_multiplier = 2:
#   daily_vol = 7.11% / sqrt(21) = 1.5517%
#   band(1)  = 2 * 1.5517% * sqrt(1)  =  3.10%
#   band(5)  = 2 * 1.5517% * sqrt(5)  =  6.94%
#   band(10) = 2 * 1.5517% * sqrt(10) =  9.81%
#   band(14) = 2 * 1.5517% * sqrt(14) = 11.61%
#   band(21) = 2 * 1.5517% * sqrt(21) = 14.22%  (= 2 * monthly_vol exactly,
#              since sqrt(21) cancels the /sqrt(21) above)
# A sell/buy actually executes the first day the close is at or beyond
# `d0_close * (1 -/+ band(t))`. If no day in 1..NOISE_BAND_MAX_TRADING_DAYS
# reaches its own band, the episode is released (D0 is discarded, as if that
# first candidate never happened).
DEFAULT_SELL_NOISE_USE_DAILY_BAND = True
DEFAULT_BUY_NOISE_USE_DAILY_BAND = True
NOISE_BAND_SIGMA_MULTIPLIER = 2.0
NOISE_BAND_MONTHLY_VOL_PCT = 7.11  # copper's own ~20-year historical monthly volatility (HG=F)
NOISE_BAND_TRADING_DAYS_PER_MONTH = 21
NOISE_BAND_MIN_CHECK_TRADING_DAYS = 8  # first day the band is actually checked
NOISE_BAND_MAX_TRADING_DAYS = 21  # observation window cap (~3 weeks)

# Confirmation, method ② (legacy, used when the checkbox above is OFF) — a
# single fixed checkpoint at D0+7 calendar days.
SELL_NOISE_FILTER_WINDOW_DAYS = 7
BUY_NOISE_FILTER_WINDOW_DAYS = 7
DEFAULT_SELL_NOISE_FILTER_DROP_PCT = 5.0
DEFAULT_BUY_NOISE_FILTER_RISE_PCT = 5.0


def noise_band_pct(elapsed_trading_days: int) -> float:
    """The method-① confirmation threshold (as a fraction, e.g. 0.031 for
    3.1%) for a D0+`elapsed_trading_days`-trading-day check — see the
    derivation above DEFAULT_SELL_NOISE_USE_DAILY_BAND. Shared by both the
    sell- and buy-signal noise filters (identical math; only the direction
    the caller compares against differs)."""
    daily_vol_pct = NOISE_BAND_MONTHLY_VOL_PCT / (NOISE_BAND_TRADING_DAYS_PER_MONTH ** 0.5)
    return NOISE_BAND_SIGMA_MULTIPLIER * daily_vol_pct * (elapsed_trading_days ** 0.5) / 100.0

# Default assumed annual yield for the "미보유기간 채권투자 가정" hybrid CAGR
# below. Adjustable per-run via simulate()'s bond_annual_yield argument.
DEFAULT_BOND_ANNUAL_YIELD = 0.10

# ---------------------------------------------------------------------------
# KODEX 구리선물(H) (138910) real trading costs — see COPPER_TRADING_LOGIC.md
# 6-2장 for the full writeup, including why this project charges only ONE
# explicit cost here (unlike Gold's KRX 금현물 model, which charges both a
# transaction fee AND a daily custody fee on top of a raw physical spot
# price that embeds neither):
#   - Transaction fee: a small, one-time online-brokerage commission on the
#     traded notional, charged at every buy and every sell fill. 0.014% here
#     is CONFIRMED against 2026 standard (non-event) online HTS/MTS ETF
#     commission schedules — 하나증권's published standard rate (0.0140%,
#     unchanged even above 1억원 notional) is the lowest of the major
#     brokerages' STANDARD rates; several brokerages run temporary
#     promotional rates as low as 0.003-0.004%, but those are time-limited
#     events, not something a long-horizon default should assume — swap
#     DEFAULT_BUY_FEE_PCT/DEFAULT_SELL_FEE_PCT for your own broker's actual
#     schedule. This is a real external cost (a brokerage fee charged on top
#     of whatever price you traded at) that is never embedded in the traded
#     price itself, so charging it here is not double-counting anything.
#   - Ongoing fund cost (총보수, 0.68%/year, CONFIRMED against Samsung Asset
#     Management's own official fund fact sheet, 기준일 2025-06-30:
#     지정판매 0.001% + 집합투자 0.599% + 신탁 0.04% + 일반사무 0.04% = 0.68%)
#     is DELIBERATELY NOT charged anywhere in this simulation as a separate
#     daily deduction. Unlike Gold's KRX 금현물 (a raw physical spot quote
#     with no expense ratio baked in at all), this project's ② KRX basis
#     uses KODEX 구리선물(H)'s own OBSERVED, ACTUALLY-TRADED market close
#     (ts.fetch_backtest_frame -> fetch_copper_price_series -> data_sources.
#     fetch_krx_copper_etf_krw, real Naver-sourced prints, not a price
#     reconstructed from HG=F). A fund's total expense ratio is accrued
#     daily against the fund's own assets and is therefore already reflected
#     in its NAV's day-to-day path, which the ETF's market price tracks
#     closely — so this observed price series already has the 0.68%/year
#     cost embedded in it. An earlier version of this module additionally
#     applied `annual_to_daily_fee_pct(0.68)` as an explicit daily_holding_
#     fee_pct on top of this same observed price, which double-charged that
#     0.68%/year (confirmed after this was flagged and investigated — see
#     COPPER_TRADING_LOGIC.md 6-2장/9장). That extra deduction has been
#     removed; only the transaction fee above remains as an explicit cost.
#     Note (relevant when comparing ① HG=F vs ② KODEX 구리선물(H) backtest
#     results): the same fact sheet shows this ETF's since-inception return
#     diverging sharply from its own benchmark index (ETF -16.40% vs index
#     +13.98% as of 2025-06-30, a -30.38%p tracking gap) — mostly futures
#     roll yield/contango cost and FX-hedging cost on the futures the fund
#     actually holds (not the 0.68%/year expense ratio alone, which would
#     only account for a few percentage points of that gap over the same
#     span). None of this is charged anywhere by this simulation as an
#     explicit fee; it is reproduced automatically, exactly once, simply by
#     using the ETF's own actual traded price series under basis ②.
# Both are meaningless for the international HG=F basis (a paper reference
# price, not a tradable domestic instrument) — the UI is responsible for
# passing 0.0 there; simulate()/run_backtest() themselves don't know or care
# which basis is in use, only the fee rates they're given.
DEFAULT_BUY_FEE_PCT = 0.014  # CONFIRMED — 하나증권 2026 standard (non-event) ETF commission
DEFAULT_SELL_FEE_PCT = 0.014  # CONFIRMED — see above


def _daily_fee_decay(elapsed_days: float, daily_fee_pct: float) -> float:
    """Multiplicative factor for a holding fee expressed as a flat DAILY rate
    — `daily_fee_pct` is compounded once per elapsed calendar day (never
    divided by 365; it's already a per-day rate). 1.0 (no-op) when
    `daily_fee_pct` is 0."""
    return (1.0 - daily_fee_pct / 100.0) ** elapsed_days


def fetch_raw_data(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    copper_price_basis: str = config.COPPER_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """Fetch dxy/fxi/copper as one date-aligned, forward-filled frame
    covering `years` + BUFFER_DAYS of history ending at `as_of` (default
    today, KST). Thin wrapper around the shared fetcher in timeseries.py.
    See fetch_backtest_frame for what `copper_price_basis` does."""
    return ts.fetch_backtest_frame(
        as_of, years=years, buffer_days=BUFFER_DAYS, copper_price_basis=copper_price_basis
    )


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Adds SMA-based copper-friendly flags for dxy/fxi, green_count (0-6),
    copper's own long-term SMA, and the 52-week new-high/new-low flags:
    - copper_sma_long: copper's own LONG_TREND_WINDOW-day SMA — the gate the
      sell-/buy-signal noise filters compare price against.
    - copper_new_52w_high / copper_new_52w_low: copper's close today is
      strictly above the highest / below the lowest close of the prior
      FIFTY_TWO_WEEK_WINDOW_DAYS calendar days (a fresh breakout day, not
      merely "currently at/above/below the 52-week high/low").
    """
    df = df.copy()
    gf_cols = []
    for col in GREEN_COUNT_INDICATORS:
        direction = signals.indicator_direction(col)
        for window in MA_WINDOWS:
            sma = metrics.compute_sma(df[col], window)
            gf_col = f"{col}_gf_{window}"
            # Delegates to the single shared comparison in signals.py — the
            # same function build_table.py's highlighting and chart shading
            # use — so this can never silently drift from those.
            df[gf_col] = signals.copper_friendly_vs_ma(df[col], sma, direction)
            gf_cols.append(gf_col)
    df["green_count"] = df[gf_cols].sum(axis=1).astype(int)

    df["copper_sma_long"] = metrics.compute_sma(df["copper"], LONG_TREND_WINDOW)

    # closed="left" excludes today's own close from "the prior N days'
    # high/low" — otherwise every day sitting at its own new high/low would
    # trivially compare equal to (never above/below) that high/low, and no
    # breakout could ever be flagged.
    prior_52w = df["copper"].rolling(f"{FIFTY_TWO_WEEK_WINDOW_DAYS}D", closed="left", min_periods=1)
    df["copper_new_52w_high"] = (df["copper"] > prior_52w.max()).fillna(False)
    df["copper_new_52w_low"] = (df["copper"] < prior_52w.min()).fillna(False)
    return df


def trim_to_backtest_window(
    df: pd.DataFrame, as_of: date | None = None, years: int = BACKTEST_YEARS
) -> pd.DataFrame:
    """Trims to [end_date - years, end_date]. The upper bound matters even
    though every caller today fetches `df` already end-bounded at `as_of` —
    it's what makes a historical `as_of` (see 유효성 검증 page's "기준일"
    input) safe regardless of how `df` was built, instead of silently
    relying on the fetch step alone."""
    end_date = as_of or today_kst()
    start_date = end_date - timedelta(days=years * 365)
    trimmed = df[(df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))]
    if trimmed.empty:
        raise RuntimeError("no data available in the requested backtest window")
    return trimmed


def _buy_reason(gc: int, buy_green_count: int, include_new_high: bool = False) -> str:
    reasons = []
    if gc >= buy_green_count:
        reasons.append(f"green_count≥{buy_green_count}")
    if include_new_high:
        reasons.append("52주 신고가 갱신")
    return ", ".join(reasons)


def _sell_reason(gc: int, sell_green_count: int, include_new_low: bool = False) -> str:
    reasons = []
    if gc <= sell_green_count:
        reasons.append(f"green_count≤{sell_green_count}")
    if include_new_low:
        reasons.append("52주 신저가 갱신")
    return ", ".join(reasons)


def run_backtest(
    signals: pd.DataFrame,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    use_new_low_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    use_buy_noise_filter: bool = True,
    buy_noise_filter_buffer_pct: float = DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT,
    use_buy_daily_band_confirmation: bool = DEFAULT_BUY_NOISE_USE_DAILY_BAND,
    buy_noise_filter_rise_pct: float = DEFAULT_BUY_NOISE_FILTER_RISE_PCT,
    buy_fee_pct: float = 0.0,
    sell_fee_pct: float = 0.0,
    daily_holding_fee_pct: float = 0.0,
    execution_delay_days: int = 0,
    execution_delay_recheck: bool = False,
) -> tuple[list[dict], pd.Series, pd.Series, pd.Series, list[dict], list[dict]]:
    """Walks the signal frame day by day applying the buy/sell rules.

    Every trigger fills immediately at its own signal day's close by
    default — there is no delay/lag anywhere in this state machine unless
    `execution_delay_days` is set above 0 (see below):
    - **green_count** (buy: `>= buy_green_count`, sell: `<= sell_green_count`).
    - The **52-week new-high trigger** (`use_new_high_trigger`, buy side, on
      by default) and the **52-week new-low trigger** (`use_new_low_trigger`,
      sell side, on by default). Neither has a cooldown/frequency limit of
      its own.

    `execution_delay_days` (default 0 = immediate, matching the description
    above exactly): once the rules above (including any noise-filter
    resolution) decide "buy/sell today" on some day D0, this holds that fill
    back by `execution_delay_days` CALENDAR days and executes it at D0+N's
    close instead of D0's — symmetric for both buy and sell, on top of and
    independent from the noise filters. This exists to test whether the
    green_count/52-week signals carry information that only pays off with a
    delay (the correlation validation in COPPER_TRADING_LOGIC.md 11장 found
    DXY/FXI's relationship with copper concentrates almost entirely at
    lag=0, i.e. same-day, but that finding doesn't rule out the executed
    strategy itself performing differently N days later — this lets that be
    checked directly rather than assumed). While a fill is pending (D0 through
    D0+N-1), no other buy/sell evaluation happens on that side (a second
    qualifying signal during the wait does not restart or cancel it, except
    per `execution_delay_recheck` below). `execution_delay_recheck` selects
    what happens at D0+N:
    - `False` (default): fires UNCONDITIONALLY at D0+N's close regardless of
      what the signal has done in between (the purest test of "does the D0
      signal predict the price N days later").
    - `True`: only fires if the same full rule set (green_count/52-week
      trigger, post any noise filter) independently still says "buy/sell"
      again on D0+N itself; otherwise the pending fill is dropped entirely
      and D0 is discarded (as if it never fired) rather than retried.
    The trade's recorded entry/exit reason always cites the ORIGINAL D0
    trigger (annotated with the D0->D0+N date range and which of the two
    modes above resolved it), even in recheck mode, since D0's condition is
    what's being tested — not whatever happens to also be true on D0+N.

    `min_holding_days`: once a position is opened, every sell trigger
    (green_count or 52-week new-low) is ignored entirely until at least this
    many calendar days have passed since entry.

    Sell-signal noise filter (`use_sell_noise_filter`, on by default):
    applies to any sell signal (green_count or 52-week new-low) that fires on
    a day copper's close (D0) is more than `sell_noise_filter_buffer_pct`%
    above its LONG_TREND_WINDOW-day calendar SMA (`copper_sma_long`) — i.e.
    still in a clear uptrend, where an isolated sell signal is more likely
    noise than a genuine reversal. Below that buffer, every sell signal
    executes immediately exactly as if this filter didn't exist. Above it,
    the signal is ignored (position stays open) and an observation episode
    starts, recording D0's date and close price. New qualifying sell signals
    that fire while an episode is already open are recorded for reference
    only (see `occurrence_count` below) — they never change or restart D0.
    Confirmation happens one of two ways:

    - **Method ① — daily band, `use_daily_band_confirmation=True` (default)**:
      days t = 1..`NOISE_BAND_MIN_CHECK_TRADING_DAYS - 1` after D0 are never
      checked at all — the position just holds regardless of price. From
      t = `NOISE_BAND_MIN_CHECK_TRADING_DAYS` through `NOISE_BAND_MAX_
      TRADING_DAYS`, every trading day's close is compared against a
      confirmation band that widens with `sqrt(t)` — see `noise_band_pct()`.
      The first day the close is at or below `d0_close * (1 -
      noise_band_pct(t))`, the position sells that day. If no day through
      the window's end reaches its own band, the episode is released unsold
      and D0 is discarded.
    - **Method ② — fixed D0+7, `use_daily_band_confirmation=False`**: a
      single checkpoint at D0+`SELL_NOISE_FILTER_WINDOW_DAYS` calendar days.
      Sells there only if that close is at least `sell_noise_filter_drop_
      pct`% below D0's close; otherwise released unsold.

    Buy-signal noise filter (`use_buy_noise_filter`, on by default,
    independent of everything above): the EXACT mirror of the sell-signal
    noise filter, direction flipped — applies to any buy signal that fires
    on a day copper's close (D0) is more than `buy_noise_filter_buffer_pct`%
    BELOW its LONG_TREND_WINDOW-day calendar SMA.

    `buy_fee_pct`/`sell_fee_pct`: a one-time % of the traded notional charged
    exactly at the moment of that fill. `daily_holding_fee_pct`: a custody/
    fund-cost accrued only on days the position is actually held, compounded
    once per elapsed calendar day (see _daily_fee_decay). All three default
    to 0.0, reproducing pre-fee behavior exactly.

    Returns (trades, equity_curve, bh_equity_curve, holding_curve,
    sell_noise_log, buy_noise_log) — see Gold's original backtest.py
    docstring (COPPER_TRADING_LOGIC.md links back to it) for the exact shape
    of each log entry; unchanged here.
    """
    dates = signals.index
    copper = signals["copper"]
    # Pre-extracted as plain numpy arrays for the same performance reason as
    # Gold's original implementation — repeated `.loc[dt]` inside a several-
    # thousand-iteration Python loop routes through pandas' full label-lookup
    # machinery on every access, which dominates this function's cost.
    copper_arr = copper.to_numpy()
    green_count_arr = signals["green_count"].to_numpy()
    copper_sma_long_arr = signals["copper_sma_long"].to_numpy()
    new_high_trigger_arr = signals["copper_new_52w_high"].to_numpy()
    new_low_trigger_arr = signals["copper_new_52w_low"].to_numpy()

    holding = False
    entry_date = None
    entry_price = None
    entry_reason = None
    equity_at_entry = None  # strategy equity value at the moment this position was opened
    running_equity = 1.0
    sell_noise_state = None
    sell_noise_log: list[dict] = []
    buy_noise_state = None
    buy_noise_log: list[dict] = []
    buy_delay_state = None  # {"d0_date", "reason"} while a buy fill awaits execution_delay_days
    sell_delay_state = None  # symmetric, for a pending sell fill
    trades: list[dict] = []
    equity_values = []
    holding_values = []

    for i, dt in enumerate(dates):
        gc = int(green_count_arr[i])
        price = float(copper_arr[i])

        if not holding:
            new_high_ready = use_new_high_trigger and bool(new_high_trigger_arr[i])

            raw_entry_reason_today = None
            if new_high_ready or gc >= buy_green_count:
                raw_entry_reason_today = _buy_reason(
                    gc,
                    buy_green_count,
                    include_new_high=new_high_ready,
                )

            entry_reason_today = None
            if buy_noise_state is not None:
                if raw_entry_reason_today is not None:
                    buy_noise_state["occurrence_dates"].append(dt)

                d0_date = buy_noise_state["d0_date"]
                d0_price = buy_noise_state["d0_price"]
                resolved = False
                bought = False
                band_pct = None
                elapsed_trading_days = None

                if buy_noise_state["use_daily_band"]:
                    elapsed_trading_days = buy_noise_state["elapsed_trading_days"] + 1
                    buy_noise_state["elapsed_trading_days"] = elapsed_trading_days
                    if elapsed_trading_days >= NOISE_BAND_MIN_CHECK_TRADING_DAYS:
                        band_pct = noise_band_pct(elapsed_trading_days)
                        price_rose = price >= d0_price * (1.0 + band_pct)
                    else:
                        band_pct = None
                        price_rose = False
                    if price_rose:
                        resolved = True
                        bought = True
                    elif elapsed_trading_days >= NOISE_BAND_MAX_TRADING_DAYS:
                        resolved = True
                        bought = False
                else:
                    if dt >= buy_noise_state["check_date"]:
                        resolved = True
                        bought = price >= d0_price * (1.0 + buy_noise_filter_rise_pct / 100.0)

                if resolved:
                    occurrence_dates = buy_noise_state["occurrence_dates"]
                    occurrence_count = len(occurrence_dates)
                    outcome = "bought_on_rise" if bought else "released_no_rise"
                    buy_noise_log.append(
                        {
                            "d0_date": d0_date,
                            "d0_price": d0_price,
                            "check_date": dt,
                            "check_price": price,
                            "elapsed_trading_days": elapsed_trading_days,
                            "band_pct": band_pct,
                            "occurrence_count": occurrence_count,
                            "outcome": outcome,
                            "bought_date": dt if bought else None,
                        }
                    )
                    if bought:
                        base_reason = _buy_reason(gc, buy_green_count) or "관찰모드 종료"
                        rise_actual_pct = (price / d0_price - 1.0) * 100.0
                        if band_pct is not None:
                            entry_reason_today = (
                                f"{base_reason} (매수노이즈필터: D0={d0_date.date()} 종가 {d0_price:g} 대비 "
                                f"{elapsed_trading_days}거래일차 종가 {price:g}, {rise_actual_pct:.1f}% 상승"
                                f"[{band_pct * 100:.2f}%↑ 밴드 도달] → 매수)"
                            )
                        else:
                            entry_reason_today = (
                                f"{base_reason} (매수노이즈필터: D0={d0_date.date()} 종가 {d0_price:g} 대비 "
                                f"D0+7일 종가 {price:g}, {rise_actual_pct:.1f}% 상승[{buy_noise_filter_rise_pct:g}%"
                                "↑ 조건 충족] → 매수)"
                            )
                    buy_noise_state = None
            elif raw_entry_reason_today is not None:
                sma_long_today = copper_sma_long_arr[i]
                in_downtrend_zone = (
                    use_buy_noise_filter
                    and not pd.isna(sma_long_today)
                    and price < sma_long_today * (1.0 - buy_noise_filter_buffer_pct / 100.0)
                )
                if in_downtrend_zone:
                    buy_noise_state = {
                        "d0_date": dt,
                        "d0_price": price,
                        "use_daily_band": use_buy_daily_band_confirmation,
                        "elapsed_trading_days": 0,
                        "check_date": dt + timedelta(days=BUY_NOISE_FILTER_WINDOW_DAYS),
                        "occurrence_dates": [dt],
                    }
                else:
                    entry_reason_today = raw_entry_reason_today

            if execution_delay_days > 0:
                if buy_delay_state is not None:
                    elapsed_days = (dt - buy_delay_state["d0_date"]).days
                    if elapsed_days >= execution_delay_days:
                        d0_date = buy_delay_state["d0_date"]
                        if execution_delay_recheck and entry_reason_today is None:
                            entry_reason_today = None  # condition no longer holds -> D0 discarded
                        else:
                            mode_note = "조건 재확인 통과" if execution_delay_recheck else "무조건 체결"
                            entry_reason_today = (
                                f"{buy_delay_state['reason']} (지연매수 {execution_delay_days}일: "
                                f"D0={d0_date.date()} → {dt.date()} {mode_note})"
                            )
                        buy_delay_state = None
                    else:
                        entry_reason_today = None  # still waiting on the pending fill
                elif entry_reason_today is not None:
                    buy_delay_state = {"d0_date": dt, "reason": entry_reason_today}
                    entry_reason_today = None

            if entry_reason_today is not None:
                holding = True
                entry_date = dt
                entry_price = price
                entry_reason = entry_reason_today
                equity_at_entry = running_equity * (1.0 - buy_fee_pct / 100.0)
                sell_noise_state = None  # defensive: a fresh position starts with no open episode
                sell_delay_state = None  # defensive: same, for the delay-fill mechanism
        else:
            exit_reason_today = None
            if (dt - entry_date).days >= min_holding_days:
                new_low_ready = use_new_low_trigger and bool(new_low_trigger_arr[i])
                if new_low_ready or gc <= sell_green_count:
                    exit_reason_today = _sell_reason(gc, sell_green_count, include_new_low=new_low_ready)

            if sell_noise_state is not None:
                if exit_reason_today is not None:
                    sell_noise_state["occurrence_dates"].append(dt)
                exit_reason_today = None  # suppressed unconditionally while an episode is open

                d0_date = sell_noise_state["d0_date"]
                d0_price = sell_noise_state["d0_price"]
                resolved = False
                sold = False
                band_pct = None
                elapsed_trading_days = None

                if sell_noise_state["use_daily_band"]:
                    elapsed_trading_days = sell_noise_state["elapsed_trading_days"] + 1
                    sell_noise_state["elapsed_trading_days"] = elapsed_trading_days
                    if elapsed_trading_days >= NOISE_BAND_MIN_CHECK_TRADING_DAYS:
                        band_pct = noise_band_pct(elapsed_trading_days)
                        price_dropped = price <= d0_price * (1.0 - band_pct)
                    else:
                        band_pct = None
                        price_dropped = False
                    if price_dropped:
                        resolved = True
                        sold = True
                    elif elapsed_trading_days >= NOISE_BAND_MAX_TRADING_DAYS:
                        resolved = True
                        sold = False
                else:
                    if dt >= sell_noise_state["check_date"]:
                        resolved = True
                        sold = price <= d0_price * (1.0 - sell_noise_filter_drop_pct / 100.0)

                if resolved:
                    occurrence_dates = sell_noise_state["occurrence_dates"]
                    occurrence_count = len(occurrence_dates)
                    outcome = "sold_on_drop" if sold else "released_no_drop"
                    sell_noise_log.append(
                        {
                            "d0_date": d0_date,
                            "d0_price": d0_price,
                            "check_date": dt,
                            "check_price": price,
                            "elapsed_trading_days": elapsed_trading_days,
                            "band_pct": band_pct,
                            "occurrence_count": occurrence_count,
                            "outcome": outcome,
                            "sold_date": dt if sold else None,
                        }
                    )
                    if sold:
                        base_reason = _sell_reason(gc, sell_green_count) or "관찰모드 종료"
                        drop_actual_pct = (price / d0_price - 1.0) * 100.0
                        if band_pct is not None:
                            exit_reason_today = (
                                f"{base_reason} (노이즈필터: D0={d0_date.date()} 종가 {d0_price:g} 대비 "
                                f"{elapsed_trading_days}거래일차 종가 {price:g}, {drop_actual_pct:.1f}% 하락"
                                f"[{band_pct * 100:.2f}%↓ 밴드 도달] → 매도)"
                            )
                        else:
                            exit_reason_today = (
                                f"{base_reason} (노이즈필터: D0={d0_date.date()} 종가 {d0_price:g} 대비 "
                                f"D0+7일 종가 {price:g}, {drop_actual_pct:.1f}% 하락[{sell_noise_filter_drop_pct:g}%"
                                "↓ 조건 충족] → 매도)"
                            )
                    sell_noise_state = None
            elif exit_reason_today is not None:
                sma_long_today = copper_sma_long_arr[i]
                in_uptrend_zone = (
                    use_sell_noise_filter
                    and not pd.isna(sma_long_today)
                    and price > sma_long_today * (1.0 + sell_noise_filter_buffer_pct / 100.0)
                )
                if in_uptrend_zone:
                    sell_noise_state = {
                        "d0_date": dt,
                        "d0_price": price,
                        "use_daily_band": use_daily_band_confirmation,
                        "elapsed_trading_days": 0,
                        "check_date": dt + timedelta(days=SELL_NOISE_FILTER_WINDOW_DAYS),
                        "occurrence_dates": [dt],
                    }
                    exit_reason_today = None

            if execution_delay_days > 0:
                if sell_delay_state is not None:
                    elapsed_days = (dt - sell_delay_state["d0_date"]).days
                    if elapsed_days >= execution_delay_days:
                        d0_date = sell_delay_state["d0_date"]
                        if execution_delay_recheck and exit_reason_today is None:
                            exit_reason_today = None  # condition no longer holds -> D0 discarded
                        else:
                            mode_note = "조건 재확인 통과" if execution_delay_recheck else "무조건 체결"
                            exit_reason_today = (
                                f"{sell_delay_state['reason']} (지연매도 {execution_delay_days}일: "
                                f"D0={d0_date.date()} → {dt.date()} {mode_note})"
                            )
                        sell_delay_state = None
                    else:
                        exit_reason_today = None  # still waiting on the pending fill
                elif exit_reason_today is not None:
                    sell_delay_state = {"d0_date": dt, "reason": exit_reason_today}
                    exit_reason_today = None

            if exit_reason_today is not None:
                exit_price = price
                fee_factor = _daily_fee_decay((dt - entry_date).days, daily_holding_fee_pct)
                equity_before_trade = running_equity
                running_equity = (
                    equity_at_entry * (exit_price / entry_price) * fee_factor * (1.0 - sell_fee_pct / 100.0)
                )
                trades.append(
                    {
                        "entry_date": entry_date,
                        "entry_price": entry_price,
                        "entry_reason": entry_reason,
                        "exit_date": dt,
                        "exit_price": exit_price,
                        "exit_reason": exit_reason_today,
                        "hold_days": (dt - entry_date).days,
                        "gross_period_return": exit_price / entry_price - 1.0,
                        "net_period_return": running_equity / equity_before_trade - 1.0,
                        "open": False,
                    }
                )
                holding = False
                entry_date = None
                entry_price = None
                entry_reason = None
                equity_at_entry = None
                buy_delay_state = None  # defensive: a fresh flat state starts with no open episode

        if holding:
            fee_factor = _daily_fee_decay((dt - entry_date).days, daily_holding_fee_pct)
            equity_values.append(equity_at_entry * (price / entry_price) * fee_factor)
        else:
            equity_values.append(running_equity)
        holding_values.append(holding)

    if holding:
        last_dt = dates[-1]
        last_price = float(copper_arr[-1])
        fee_factor = _daily_fee_decay((last_dt - entry_date).days, daily_holding_fee_pct)
        trades.append(
            {
                "entry_date": entry_date,
                "entry_price": entry_price,
                "entry_reason": entry_reason,
                "exit_date": None,
                "exit_price": last_price,
                "exit_reason": None,
                "hold_days": (last_dt - entry_date).days,
                "gross_period_return": last_price / entry_price - 1.0,
                "net_period_return": (equity_at_entry / running_equity) * (last_price / entry_price) * fee_factor
                - 1.0,
                "open": True,
            }
        )
        equity_values[-1] *= 1.0 - sell_fee_pct / 100.0

    if sell_noise_state is not None:
        occurrence_dates = sell_noise_state["occurrence_dates"]
        sell_noise_log.append(
            {
                "d0_date": sell_noise_state["d0_date"],
                "d0_price": sell_noise_state["d0_price"],
                "check_date": None,
                "check_price": None,
                "elapsed_trading_days": (
                    sell_noise_state["elapsed_trading_days"] if sell_noise_state["use_daily_band"] else None
                ),
                "band_pct": None,
                "occurrence_count": len(occurrence_dates),
                "outcome": "unresolved_at_window_end",
                "sold_date": None,
            }
        )

    if buy_noise_state is not None:
        occurrence_dates = buy_noise_state["occurrence_dates"]
        buy_noise_log.append(
            {
                "d0_date": buy_noise_state["d0_date"],
                "d0_price": buy_noise_state["d0_price"],
                "check_date": None,
                "check_price": None,
                "elapsed_trading_days": (
                    buy_noise_state["elapsed_trading_days"] if buy_noise_state["use_daily_band"] else None
                ),
                "band_pct": None,
                "occurrence_count": len(occurrence_dates),
                "outcome": "unresolved_at_window_end",
                "bought_date": None,
            }
        )

    equity_curve = pd.Series(equity_values, index=dates, name="strategy_equity")
    elapsed_since_start = (dates - dates[0]).days.to_numpy()
    bh_holding_fee_decay = (1.0 - daily_holding_fee_pct / 100.0) ** elapsed_since_start
    bh_equity_curve = (copper / copper.iloc[0]) * bh_holding_fee_decay * (1.0 - buy_fee_pct / 100.0)
    bh_equity_curve = bh_equity_curve.rename("bh_equity")
    bh_equity_curve.iloc[-1] *= 1.0 - sell_fee_pct / 100.0
    holding_curve = pd.Series(holding_values, index=dates, name="holding")
    return trades, equity_curve, bh_equity_curve, holding_curve, sell_noise_log, buy_noise_log


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

    running_max = equity_curve.cummax().clip(lower=1.0)
    drawdown = equity_curve / running_max - 1.0
    max_drawdown = float(drawdown.min())

    win_rate = (
        sum(1 for t in closed_trades if t["net_period_return"] > 0) / len(closed_trades)
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


def compute_hybrid_cagr(
    holding_curve: pd.Series,
    copper: pd.Series,
    bond_annual_yield: float,
    buy_fee_pct: float = 0.0,
    sell_fee_pct: float = 0.0,
    daily_holding_fee_pct: float = 0.0,
) -> dict:
    """The full-period CAGR variant that fills non-holding days with an
    assumed bond return instead of leaving them flat — see Gold's original
    docstring (unchanged mechanics, `gold` renamed `copper`)."""
    dates = holding_curve.index
    holding_arr = holding_curve.to_numpy()
    copper_arr = copper.to_numpy()
    hybrid_equity = 1.0 - buy_fee_pct / 100.0 if bool(holding_arr[0]) else 1.0
    equity_values = [hybrid_equity]
    non_holding_days = 0
    total_days = 0
    for i in range(1, len(dates)):
        elapsed_days = (dates[i] - dates[i - 1]).days
        total_days += elapsed_days
        was_holding = bool(holding_arr[i - 1])
        is_holding = bool(holding_arr[i])
        if was_holding:
            factor = float(copper_arr[i] / copper_arr[i - 1]) * _daily_fee_decay(
                elapsed_days, daily_holding_fee_pct
            )
            if not is_holding:
                factor *= 1.0 - sell_fee_pct / 100.0
        else:
            factor = (1.0 + bond_annual_yield) ** (elapsed_days / 365.25)
            non_holding_days += elapsed_days
            if is_holding:
                factor *= 1.0 - buy_fee_pct / 100.0
        hybrid_equity *= factor
        equity_values.append(hybrid_equity)

    if bool(holding_arr[-1]):
        hybrid_equity *= 1.0 - sell_fee_pct / 100.0
        equity_values[-1] = hybrid_equity

    hybrid_total_return = hybrid_equity - 1.0
    hybrid_cagr = hybrid_equity ** (365.25 / total_days) - 1.0 if total_days > 0 else None
    non_holding_fraction = non_holding_days / total_days if total_days > 0 else None
    hybrid_equity_curve = pd.Series(equity_values, index=dates, name="hybrid_equity")

    return {
        "bond_annual_yield": bond_annual_yield,
        "hybrid_total_return": hybrid_total_return,
        "hybrid_cagr": hybrid_cagr,
        "non_holding_days": non_holding_days,
        "non_holding_fraction": non_holding_fraction,
        "hybrid_equity_curve": hybrid_equity_curve,
    }


def yearly_returns(equity_curve: pd.Series, bh_equity_curve: pd.Series) -> pd.DataFrame:
    """Calendar-year returns for both curves — see Gold's original docstring
    (unchanged mechanics)."""
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


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_max_window_signals(as_of_iso: str, copper_price_basis: str) -> pd.DataFrame:
    """fetch + compute_signals over the WIDEST window the "분석 기간(N년)"
    slider can ever request (MAX_BACKTEST_YEARS + BUFFER_DAYS), cached only on
    (as_of, copper_price_basis) — deliberately NOT on `years`. prepare_signals()
    below trims this down to whatever narrower `years` was actually asked
    for, entirely in memory (same caching strategy as Gold's own backtest.py
    — see its docstring for the full rationale)."""
    as_of = date.fromisoformat(as_of_iso)
    raw = fetch_raw_data(as_of, years=MAX_BACKTEST_YEARS, copper_price_basis=copper_price_basis)
    return compute_signals(raw)


def prepare_signals(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    copper_price_basis: str = config.COPPER_PRICE_BASIS_DEFAULT,
) -> pd.DataFrame:
    """The network-bound half of the pipeline: fetch + compute signals + trim
    to the backtest window."""
    as_of_date = as_of if as_of is not None else today_kst()
    full_signals = _cached_max_window_signals(as_of_date.isoformat(), copper_price_basis)
    return trim_to_backtest_window(full_signals, as_of_date, years=years)


def simulate(
    signals: pd.DataFrame,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    use_new_low_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    bond_annual_yield: float = DEFAULT_BOND_ANNUAL_YIELD,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    use_buy_noise_filter: bool = True,
    buy_noise_filter_buffer_pct: float = DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT,
    use_buy_daily_band_confirmation: bool = DEFAULT_BUY_NOISE_USE_DAILY_BAND,
    buy_noise_filter_rise_pct: float = DEFAULT_BUY_NOISE_FILTER_RISE_PCT,
    buy_fee_pct: float = 0.0,
    sell_fee_pct: float = 0.0,
    daily_holding_fee_pct: float = 0.0,
    execution_delay_days: int = 0,
    execution_delay_recheck: bool = False,
) -> dict:
    """The pure-computation half: run the trade state machine over already-
    prepared signals and derive trades/equity curves/metrics/yearly returns."""
    trades, equity_curve, bh_equity_curve, holding_curve, sell_noise_log, buy_noise_log = run_backtest(
        signals,
        use_new_high_trigger=use_new_high_trigger,
        use_new_low_trigger=use_new_low_trigger,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
        use_sell_noise_filter=use_sell_noise_filter,
        sell_noise_filter_buffer_pct=sell_noise_filter_buffer_pct,
        use_daily_band_confirmation=use_daily_band_confirmation,
        sell_noise_filter_drop_pct=sell_noise_filter_drop_pct,
        use_buy_noise_filter=use_buy_noise_filter,
        buy_noise_filter_buffer_pct=buy_noise_filter_buffer_pct,
        use_buy_daily_band_confirmation=use_buy_daily_band_confirmation,
        buy_noise_filter_rise_pct=buy_noise_filter_rise_pct,
        buy_fee_pct=buy_fee_pct,
        sell_fee_pct=sell_fee_pct,
        daily_holding_fee_pct=daily_holding_fee_pct,
        execution_delay_days=execution_delay_days,
        execution_delay_recheck=execution_delay_recheck,
    )
    metrics_out = compute_metrics(trades, equity_curve, bh_equity_curve)
    hybrid = compute_hybrid_cagr(
        holding_curve, signals["copper"], bond_annual_yield, buy_fee_pct, sell_fee_pct, daily_holding_fee_pct
    )
    hybrid_equity_curve = hybrid.pop("hybrid_equity_curve")
    metrics_out.update(hybrid)
    yearly = yearly_returns(hybrid_equity_curve, bh_equity_curve)
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "bh_equity_curve": bh_equity_curve,
        "holding_curve": holding_curve,
        "hybrid_equity_curve": hybrid_equity_curve,
        "metrics": metrics_out,
        "yearly_returns": yearly,
        "sell_noise_log": sell_noise_log,
        "buy_noise_log": buy_noise_log,
    }


def run(
    as_of: date | None = None,
    years: int = BACKTEST_YEARS,
    use_new_high_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    use_new_low_trigger: bool = DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
    buy_green_count: int = BUY_GREEN_COUNT,
    sell_green_count: int = SELL_GREEN_COUNT,
    min_holding_days: int = 0,
    bond_annual_yield: float = DEFAULT_BOND_ANNUAL_YIELD,
    copper_price_basis: str = config.COPPER_PRICE_BASIS_DEFAULT,
    use_sell_noise_filter: bool = True,
    sell_noise_filter_buffer_pct: float = DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
    use_daily_band_confirmation: bool = DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    sell_noise_filter_drop_pct: float = DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    use_buy_noise_filter: bool = True,
    buy_noise_filter_buffer_pct: float = DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT,
    use_buy_daily_band_confirmation: bool = DEFAULT_BUY_NOISE_USE_DAILY_BAND,
    buy_noise_filter_rise_pct: float = DEFAULT_BUY_NOISE_FILTER_RISE_PCT,
    buy_fee_pct: float = 0.0,
    sell_fee_pct: float = 0.0,
    daily_holding_fee_pct: float = 0.0,
    execution_delay_days: int = 0,
    execution_delay_recheck: bool = False,
) -> dict:
    signals = prepare_signals(as_of, years=years, copper_price_basis=copper_price_basis)
    result = simulate(
        signals,
        use_new_high_trigger=use_new_high_trigger,
        use_new_low_trigger=use_new_low_trigger,
        buy_green_count=buy_green_count,
        sell_green_count=sell_green_count,
        min_holding_days=min_holding_days,
        bond_annual_yield=bond_annual_yield,
        use_sell_noise_filter=use_sell_noise_filter,
        sell_noise_filter_buffer_pct=sell_noise_filter_buffer_pct,
        use_daily_band_confirmation=use_daily_band_confirmation,
        sell_noise_filter_drop_pct=sell_noise_filter_drop_pct,
        use_buy_noise_filter=use_buy_noise_filter,
        buy_noise_filter_buffer_pct=buy_noise_filter_buffer_pct,
        use_buy_daily_band_confirmation=use_buy_daily_band_confirmation,
        buy_noise_filter_rise_pct=buy_noise_filter_rise_pct,
        buy_fee_pct=buy_fee_pct,
        sell_fee_pct=sell_fee_pct,
        daily_holding_fee_pct=daily_holding_fee_pct,
        execution_delay_days=execution_delay_days,
        execution_delay_recheck=execution_delay_recheck,
    )
    result["signals"] = signals
    return result
