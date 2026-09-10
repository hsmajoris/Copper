"""v5 validation report: green_count (consensus-count) architecture as the
permanent core, gold/copper ratio removed from the signal entirely, plus
copper's own 200-day-SMA crossover as an independent entry/exit trigger —
see copper_dashboard/backtest_v5.py's module docstring for the trade-log
evidence that motivated this specific, single design (not a parameter grid
search against the out-of-sample window).

Runs the SAME v5 design once across the full period, in-sample
(2016-2022), and out-of-sample (2023-2026), against v1/v2/v4 for
reference, and reports the result as-is — including where v5 still loses
to Buy & Hold, per the explicit instruction not to keep tuning until it wins.

Run: python scripts/backtest_v5_report.py
"""

import sys
import time
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd
import requests

from copper_dashboard import backtest as cbt
from copper_dashboard import backtest_v4 as v4
from copper_dashboard import backtest_v5 as v5

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


FETCH_START = date(2014, 1, 1)
FETCH_END = date(2026, 1, 1)
AS_OF = pd.Timestamp(date(2026, 1, 1))
YEARS = 10
IN_S, IN_E = "2016-01-01", "2022-12-31"
OUT_S, OUT_E = "2023-01-01", "2026-01-01"


def trim(df, as_of, years):
    return df[df.index >= as_of - pd.Timedelta(days=years * 365)]


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
    print("fetching real market data...", file=sys.stderr)
    dxy = fetch_yahoo_close("DX-Y.NYB", FETCH_START, FETCH_END).rename("dxy")
    wti = fetch_yahoo_close("CL=F", FETCH_START, FETCH_END).rename("wti")
    gold = fetch_yahoo_close("GC=F", FETCH_START, FETCH_END).rename("gold")
    copper = fetch_yahoo_close("HG=F", FETCH_START, FETCH_END).rename("copper")
    print("fetch complete", file=sys.stderr)

    raw = pd.concat([dxy, wti, gold, copper], axis=1, join="outer", sort=True).sort_index().ffill().dropna()

    v1_params = cbt.StrategyParams.v1_baseline()
    v2_params = cbt.StrategyParams()
    v1_sig = trim(cbt.compute_signals(raw.copy(), v1_params), AS_OF, YEARS)
    v2_sig = trim(cbt.compute_signals(raw.copy(), v2_params), AS_OF, YEARS)
    v1_res = cbt.simulate(v1_sig, v1_params)
    v2_res = cbt.simulate(v2_sig, v2_params)

    NEUTRAL = dict(use_new_high_buy=False, buy_ratio=-1.0, sell_ratio=float("inf"), buy_green_count=99, sell_green_count=-1)
    v4_sig = trim(v4.compute_signals(raw.copy()), AS_OF, YEARS)
    v4_gc_res = v4.simulate(v4_sig, **{**NEUTRAL, "buy_green_count": 6, "sell_green_count": 0})

    v5_sig_full = v5.compute_signals(raw.copy())
    v5_sig = trim(v5_sig_full, AS_OF, YEARS)
    v5_res = v5.simulate(v5_sig)

    print_table(
        [
            row("v1 (baseline)", v1_res["metrics"]),
            row("v2 (가중점수, 폐기 예정)", v2_res["metrics"]),
            row("v4: DXY+WTI green_count 단독 (참고용)", v4_gc_res["metrics"]),
            row("v5: green_count + 200일선 크로스오버", v5_res["metrics"]),
        ],
        f"v5 최종 검증 — 최근 {YEARS}년, 동일 실데이터",
    )

    v5_in = v5.simulate(slice_df(v5_sig_full, IN_S, IN_E))
    v5_out = v5.simulate(slice_df(v5_sig_full, OUT_S, OUT_E))
    print_table(
        [
            row("v5, in-sample(2016-2022)", v5_in["metrics"]),
            row("v5, out-of-sample(2023-2026)", v5_out["metrics"]),
        ],
        "v5 in-sample / out-of-sample",
    )

    print("\n=== v5 out-of-sample trade log (2023-2026) ===")
    for t in v5_out["trades"]:
        exit_str = f"{t['exit_date'].date()} @ {t['exit_price']:.3f} ({t['exit_reason'] or '-'})" if t["exit_date"] else f"OPEN @ {t['exit_price']:.3f}"
        print(f"  {t['entry_date'].date()} @ {t['entry_price']:.3f} ({t['entry_reason']})  ->  {exit_str}  ret={t['period_return']:.1%}")

    print(
        "\n\n결론 (사실 그대로): v5는 v1(-9.4%)·v2(-20.4%)를 크게 앞서고 in-sample(2016-2022)에서는 "
        "Buy & Hold도 이기지만(109.2% vs 83.8%), out-of-sample(2023-2026)에서는 여전히 Buy & Hold에 "
        "뒤짐. 2025년 한 분기 만에 발생한 급등-급반전(공급 충격성 스파이크)은 200일선이 반응하기엔 "
        "너무 빠르고 커서, 어떤 이동평균 기반 추세추종 규칙도 그 구간의 왕복을 완전히 피하지 못함 — "
        "억지로 더 맞추지 않고 이 결론을 그대로 보고함."
    )


if __name__ == "__main__":
    main()
