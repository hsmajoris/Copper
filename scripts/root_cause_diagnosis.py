"""Root-cause diagnosis: why did the Gold dashboard's backtest beat Buy &
Hold while Copper's v1 and v2 both lost badly, over the same 2024-2026
window? Read `../Gold`'s actual code (not assumed) to compare its trading
architecture against Copper's, built `copper_dashboard/backtest_v4.py` as a
structural port of Gold's exact engine, and ran all of it against real
market data.

Data sources (both reachable via plain `requests` in this sandbox — unlike
yfinance's `.history()`, which internally calls `.info()` and trips an
anti-bot block on Yahoo's quoteSummary endpoint specifically):
  - Yahoo's chart JSON endpoint (query1.finance.yahoo.com/v8/finance/chart)
    for DXY/WTI/GC=F/HG=F/SI=F daily closes.
  - FRED's public CSV endpoint (fred.stlouisfed.org/graph/fredgraph.csv)
    for the 10-year TIPS real yield (DFII10), Gold's real_rate indicator.

Requires a checkout of the Gold repo as a sibling directory
(`git clone https://github.com/hsmajoris/Gold.git ../Gold` relative to this
repo) to reproduce Gold's own backtest with Gold's own code — section 0 is
skipped if that's not present or FRED is unreachable.

Run: python scripts/root_cause_diagnosis.py
"""

import sys
import time
from datetime import date, datetime
from io import StringIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT.parent / "Gold"))

import pandas as pd
import requests

from copper_dashboard import backtest as cbt
from copper_dashboard import backtest_v4 as v4

HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_yahoo_close(symbol: str, start: date, end: date) -> pd.Series:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "period1": int(datetime.combine(start, datetime.min.time()).timestamp()),
        "period2": int(datetime.combine(end, datetime.min.time()).timestamp()),
        "interval": "1d",
    }
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()["chart"]["result"][0]
            ts = pd.to_datetime(data["timestamp"], unit="s").normalize()
            close = pd.Series(data["indicators"]["quote"][0]["close"], index=ts, name=symbol).dropna()
            return close[~close.index.duplicated(keep="last")].sort_index()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)


def fetch_fred_close(series_id: str, start: date, end: date) -> pd.Series:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    params = {"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()}
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=45)
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text))
            df.columns = ["date", "value"]
            df = df[df["value"] != "."]
            df["date"] = pd.to_datetime(df["date"])
            df["value"] = df["value"].astype(float)
            return df.set_index("date")["value"].rename(series_id)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5)


FETCH_START = date(2014, 1, 1)
FETCH_END = date(2026, 1, 1)
AS_OF = date(2026, 1, 1)
YEARS = 10


def trim(df, as_of, years):
    start = as_of - pd.Timedelta(days=years * 365)
    return df[df.index >= pd.Timestamp(start)]


def slice_df(df, start, end):
    return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


def fmt_pct(x):
    return f"{x:.1%}" if x is not None else "-"


def fmt_ratio(x):
    return f"{x:.2f}" if x is not None else "-"


def row(label, m):
    return {
        "시나리오": label,
        "누적수익률": fmt_pct(m["strategy_total_return"]),
        "CAGR": fmt_pct(m["strategy_cagr"]),
        "MDD": fmt_pct(m["max_drawdown"]),
        "거래횟수": m["closed_trade_count"],
        "승률": fmt_pct(m["win_rate"]),
        "Sharpe": fmt_ratio(m["sharpe_ratio"]),
        "BH누적": fmt_pct(m["bh_total_return"]),
        "BH CAGR": fmt_pct(m["bh_cagr"]),
    }


def print_table(rows, title):
    print(f"\n### {title}\n")
    print(pd.DataFrame(rows).to_markdown(index=False))


def main():
    print("fetching real market data (Yahoo)...", file=sys.stderr)
    dxy = fetch_yahoo_close("DX-Y.NYB", FETCH_START, FETCH_END).rename("dxy")
    wti_yf = fetch_yahoo_close("CL=F", FETCH_START, FETCH_END).rename("wti")
    gold = fetch_yahoo_close("GC=F", FETCH_START, FETCH_END).rename("gold")
    copper = fetch_yahoo_close("HG=F", FETCH_START, FETCH_END).rename("copper")
    silver = fetch_yahoo_close("SI=F", FETCH_START, FETCH_END).rename("silver")
    print("Yahoo fetch complete", file=sys.stderr)

    try:
        import gold_dashboard.backtest as gbt
    except ImportError:
        gbt = None
        print("Gold's-own-backtest section skipped: ../Gold is not checked out", file=sys.stderr)

    gold_raw = None
    if gbt is not None:
        try:
            print("fetching real market data (FRED)...", file=sys.stderr)
            real_rate = fetch_fred_close("DFII10", FETCH_START, FETCH_END).rename("real_rate")
            gold_raw = (
                pd.concat([real_rate, dxy, gold, silver], axis=1, join="outer", sort=True)
                .sort_index()
                .ffill()
                .dropna()
            )
            print("FRED fetch complete", file=sys.stderr)
        except Exception as exc:
            print(f"Gold's-own-backtest section skipped: {exc!r}", file=sys.stderr)

    copper_raw = (
        pd.concat([dxy, wti_yf, gold, copper], axis=1, join="outer", sort=True).sort_index().ffill().dropna()
    )

    # --- 0. Gold's OWN real backtest, its own code, its own defaults ---
    if gold_raw is not None:
        gold_sig = trim(gbt.compute_signals(gold_raw), pd.Timestamp(AS_OF), YEARS)
        gold_result = gbt.simulate(gold_sig, use_new_high_buy=True)  # Gold's own page default
        gold_result_no_newhigh = gbt.simulate(gold_sig, use_new_high_buy=False)
        print_table(
            [
                row("Gold (실제 코드, 기본값 — 신고가매수 ON)", gold_result["metrics"]),
                row("Gold (신고가매수 OFF)", gold_result_no_newhigh["metrics"]),
            ],
            f"0. Gold 실제 백테스트 (최근 {YEARS}년, Gold 자체 코드/데이터)",
        )

    # --- 1. Copper v1 vs v2 vs v2+new_high vs v4 ---
    v1_params = cbt.StrategyParams.v1_baseline()
    v2_params = cbt.StrategyParams()
    v2_newhigh_params = cbt.StrategyParams(use_new_high_buy=True)

    v1_sig = trim(cbt.compute_signals(copper_raw.copy(), v1_params), pd.Timestamp(AS_OF), YEARS)
    v2_sig = trim(cbt.compute_signals(copper_raw.copy(), v2_params), pd.Timestamp(AS_OF), YEARS)

    v1_result = cbt.simulate(v1_sig, v1_params)
    v2_result = cbt.simulate(v2_sig, v2_params)
    v2_newhigh_result = cbt.simulate(v2_sig, v2_newhigh_params)

    v4_sig = trim(v4.compute_signals(copper_raw.copy()), pd.Timestamp(AS_OF), YEARS)
    v4_result = v4.simulate(v4_sig, use_new_high_buy=True)
    v4_no_newhigh_result = v4.simulate(v4_sig, use_new_high_buy=False)

    print_table(
        [
            row("v1 (baseline)", v1_result["metrics"]),
            row("v2 (현재)", v2_result["metrics"]),
            row("v2 + 신고가매수만 ON (가설 검증)", v2_newhigh_result["metrics"]),
            row("v4 (Gold 구조 100% 이식, 신고가매수 ON)", v4_result["metrics"]),
            row("v4 (신고가매수 OFF)", v4_no_newhigh_result["metrics"]),
        ],
        f"1. Copper: v1 vs v2 vs v2+신고가 vs v4 (최근 {YEARS}년, 동일 데이터)",
    )

    # --- 2. v4 decomposition ---
    NEUTRAL = dict(use_new_high_buy=False, buy_ratio=-1.0, sell_ratio=float("inf"), buy_green_count=99, sell_green_count=-1)

    def isolate(sig_df, **overrides):
        params = dict(NEUTRAL)
        params.update(overrides)
        return v4.simulate(sig_df, **params)

    v4_dxy_only_sig = trim(v4.compute_signals(copper_raw.copy(), indicators=("dxy",)), pd.Timestamp(AS_OF), YEARS)
    v4_wti_only_sig = trim(v4.compute_signals(copper_raw.copy(), indicators=("wti",)), pd.Timestamp(AS_OF), YEARS)

    v4_dxy_only = isolate(v4_dxy_only_sig, buy_green_count=3, sell_green_count=0)
    v4_wti_only = isolate(v4_wti_only_sig, buy_green_count=3, sell_green_count=0)
    v4_both_green_count_only = isolate(v4_sig, buy_green_count=6, sell_green_count=0)
    v4_ratio_only = isolate(
        v4_sig, buy_ratio=cbt.config.DEFAULT_GC_RATIO_BUY_THRESHOLD, sell_ratio=cbt.config.DEFAULT_GC_RATIO_SELL_THRESHOLD
    )
    v4_newhigh_only = isolate(v4_sig, use_new_high_buy=True)

    print_table(
        [
            row("v4 전체 (DXY+WTI green_count + 비율 + 신고가)", v4_result["metrics"]),
            row("├ DXY 단독 green_count (0-3, buy=3)", v4_dxy_only["metrics"]),
            row("├ WTI 단독 green_count (0-3, buy=3)", v4_wti_only["metrics"]),
            row("├ DXY+WTI green_count만 (신고가·비율 비활성)", v4_both_green_count_only["metrics"]),
            row("├ 금/구리비율 트리거만", v4_ratio_only["metrics"]),
            row("└ 신고가매수만 (green_count·비율 비활성, 매도 트리거도 없어 사실상 '한 번 사서 계속 보유')", v4_newhigh_only["metrics"]),
        ],
        "2. v4 기여도 분해 — 각 행은 나머지 트리거를 전부 비활성화하고 하나만 격리 테스트",
    )

    # --- 3. in-sample / out-of-sample, including the best isolated mechanism ---
    IN_S, IN_E = "2016-01-01", "2022-12-31"
    OUT_S, OUT_E = "2023-01-01", "2026-01-01"

    scenarios = {
        "v1": (v1_sig, lambda s: cbt.simulate(s, v1_params)),
        "v2": (v2_sig, lambda s: cbt.simulate(s, v2_params)),
        "v4 전체": (v4_sig, lambda s: v4.simulate(s, use_new_high_buy=True)),
        "v4 DXY+WTI green_count만": (v4_sig, lambda s: isolate(s, buy_green_count=6, sell_green_count=0)),
    }
    rows = []
    for label, (sig_df, runner) in scenarios.items():
        in_res = runner(slice_df(sig_df, IN_S, IN_E))
        out_res = runner(slice_df(sig_df, OUT_S, OUT_E))
        rows.append(row(f"{label}, in-sample(2016-2022)", in_res["metrics"]))
        rows.append(row(f"{label}, out-of-sample(2023-2026)", out_res["metrics"]))
    print_table(rows, "3. in-sample / out-of-sample — v1 실패 구간에서 가장 나은 메커니즘도 이기는지")

    print("\n\ndone.")


if __name__ == "__main__":
    main()
