"""v1-vs-v2 backtest validation script — the "검증 요구사항" from the v2
signal-logic change request:

  1. v1 vs v2 performance comparison (total return, CAGR, MDD, trade count,
     win rate, Sharpe ratio)
  2. trade count: did it drop meaningfully from v1's 45 (target: <= 20)?
  3. 2023-2026-only performance (the window v1 failed in)
  4. ablation: toggle each of fixes 1-5 off (one at a time) from the v2
     baseline and see which one actually moved the needle
  5. in-sample (2016-2022) / out-of-sample (2023-2026) split

This script needs real network access to Yahoo Finance (via yfinance) to
fetch ~12 years of DXY/WTI/GC=F/HG=F daily history — it was written and its
*mechanics* were verified against synthetic data (see
tests/test_v2_signal_logic.py) in a sandboxed environment with no market-
data access, but it has NOT been run against real history. Run it yourself
with:

    python scripts/validate_v2.py

and paste the printed markdown tables into README.md's "v2 백테스트 검증
결과" section — see the note left there.

Per the task brief's explicit "과최적화 금지" instruction, none of the
numbers this script prints should be used to hand-tune config.py's
constants after the fact. If v2 still doesn't beat Buy & Hold, report that
plainly (see config.py and README.md for the same point) rather than
grid-searching these parameters until it does.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from copper_dashboard import backtest, config

FULL_YEARS = 10
IN_SAMPLE_START, IN_SAMPLE_END = "2016-01-01", "2022-12-31"
OUT_SAMPLE_START, OUT_SAMPLE_END = "2023-01-01", "2026-12-31"


def _fmt_pct(x):
    return f"{x:.1%}" if x is not None else "-"


def _fmt_ratio(x):
    return f"{x:.2f}" if x is not None else "-"


def metrics_row(label: str, m: dict) -> dict:
    return {
        "시나리오": label,
        "누적수익률": _fmt_pct(m["strategy_total_return"]),
        "CAGR": _fmt_pct(m["strategy_cagr"]),
        "MDD": _fmt_pct(m["max_drawdown"]),
        "거래횟수": m["closed_trade_count"],
        "승률": _fmt_pct(m["win_rate"]),
        "Sharpe": _fmt_ratio(m["sharpe_ratio"]),
        "BH 누적수익률": _fmt_pct(m["bh_total_return"]),
        "BH CAGR": _fmt_pct(m["bh_cagr"]),
        "BH Sharpe": _fmt_ratio(m["bh_sharpe_ratio"]),
    }


def print_table(rows: list[dict], title: str) -> None:
    print(f"\n### {title}\n")
    df = pd.DataFrame(rows)
    print(df.to_markdown(index=False))


def run_scenario(raw: pd.DataFrame, params: backtest.StrategyParams, as_of: date, years: int) -> dict:
    sig = backtest.compute_signals(raw.copy(), params)
    trimmed = backtest.trim_to_backtest_window(sig, as_of=as_of, years=years)
    return backtest.simulate(trimmed, params)


def run_scenario_on_slice(raw: pd.DataFrame, params: backtest.StrategyParams, start: str, end: str) -> dict:
    sig = backtest.compute_signals(raw.copy(), params)
    sliced = sig[(sig.index >= pd.Timestamp(start)) & (sig.index <= pd.Timestamp(end))]
    if sliced.empty:
        raise RuntimeError(f"no data in slice {start}..{end}")
    sliced = sliced.copy()
    sliced.attrs.update(sig.attrs)
    return backtest.simulate(sliced, params)


def main() -> None:
    as_of = date.today()
    v1 = backtest.StrategyParams.v1_baseline()
    v2 = backtest.StrategyParams()

    # Fetch one big raw frame (12y + whatever buffer the hungriest scenario
    # needs) and reuse it for every scenario below — only compute_signals
    # differs per params, no need to re-hit the network per scenario.
    fetch_years = FULL_YEARS + 2  # +2y so the 2016 in-sample start already has full rolling windows
    buffer_days = max(backtest.required_buffer_days(v1), backtest.required_buffer_days(v2))
    print(f"fetching {fetch_years}y + {buffer_days}d buffer of history ending {as_of}...", file=sys.stderr)
    raw = backtest.fetch_raw_data(as_of=as_of, years=fetch_years, buffer_days=buffer_days)

    # --- 1 & 2: v1 vs v2, full period ---
    v1_full = run_scenario(raw, v1, as_of, FULL_YEARS)
    v2_full = run_scenario(raw, v2, as_of, FULL_YEARS)
    print_table(
        [metrics_row("v1 (baseline)", v1_full["metrics"]), metrics_row("v2 (전체 수정 적용)", v2_full["metrics"])],
        f"1-2. v1 vs v2 — 최근 {FULL_YEARS}년 전체",
    )
    v1_trades = v1_full["metrics"]["closed_trade_count"]
    v2_trades = v2_full["metrics"]["closed_trade_count"]
    print(
        f"\n거래 횟수: v1 {v1_trades}회 -> v2 {v2_trades}회 "
        f"({'목표(<=20회) 달성' if v2_trades <= 20 else '목표(<=20회) 미달성'})"
    )

    # --- 3 & 5: in-sample / out-of-sample split ---
    v1_in = run_scenario_on_slice(raw, v1, IN_SAMPLE_START, IN_SAMPLE_END)
    v2_in = run_scenario_on_slice(raw, v2, IN_SAMPLE_START, IN_SAMPLE_END)
    v1_out = run_scenario_on_slice(raw, v1, OUT_SAMPLE_START, OUT_SAMPLE_END)
    v2_out = run_scenario_on_slice(raw, v2, OUT_SAMPLE_START, OUT_SAMPLE_END)
    print_table(
        [
            metrics_row(f"v1, in-sample ({IN_SAMPLE_START[:4]}-{IN_SAMPLE_END[:4]})", v1_in["metrics"]),
            metrics_row(f"v2, in-sample ({IN_SAMPLE_START[:4]}-{IN_SAMPLE_END[:4]})", v2_in["metrics"]),
            metrics_row(f"v1, out-of-sample ({OUT_SAMPLE_START[:4]}-{OUT_SAMPLE_END[:4]})", v1_out["metrics"]),
            metrics_row(f"v2, out-of-sample ({OUT_SAMPLE_START[:4]}-{OUT_SAMPLE_END[:4]})", v2_out["metrics"]),
        ],
        "3 & 5. in-sample(2016-2022) / out-of-sample(2023-2026) — v1 실패 구간에서 v2가 실제로 개선됐는지",
    )

    # --- 4: ablation — turn OFF one fix at a time from the v2 baseline ---
    ablations = {
        "fix1 OFF (금/구리비율: 절대임계값으로 복귀)": backtest.StrategyParams(use_rolling_ratio=False),
        "fix2 OFF (구리 200일 추세 필터 제거)": backtest.StrategyParams(use_copper_trend=False, copper_trend_partial_exit=False),
        "fix3 OFF (whipsaw 억제 해제 — 컷오프도 60/40으로 복귀)": backtest.StrategyParams(
            use_whipsaw_suppression=False,
            signal_confirmation_days=1,
            min_holding_days=0,
            buy_cutoff=config.LEGACY_V1_BUY_CUTOFF,
            sell_cutoff=config.LEGACY_V1_SELL_CUTOFF,
        ),
        "fix4 OFF (DXY: SMA 방식으로 복귀)": backtest.StrategyParams(dxy_method="sma"),
        "fix5 OFF (손절 해제)": backtest.StrategyParams(use_stop_loss=False),
    }
    rows = [metrics_row("v2 (전체 적용, 기준선)", v2_full["metrics"])]
    for label, params in ablations.items():
        result = run_scenario(raw, params, as_of, FULL_YEARS)
        rows.append(metrics_row(label, result["metrics"]))
    print_table(rows, "4. Ablation — v2에서 항목을 하나씩 껐을 때의 변화 (기준선과 비교)")

    # --- fix4 bonus: DXY roc vs sma head-to-head (explicitly requested) ---
    dxy_roc = run_scenario(raw, backtest.StrategyParams(dxy_method="roc"), as_of, FULL_YEARS)
    dxy_sma = run_scenario(raw, backtest.StrategyParams(dxy_method="sma"), as_of, FULL_YEARS)
    print_table(
        [metrics_row("DXY: 60일 변화율(v2 후보)", dxy_roc["metrics"]), metrics_row("DXY: 자체 이평선(v1)", dxy_sma["metrics"])],
        "4번 부록. DXY 판정 방식 단독 비교 (그 외 모든 설정은 v2 동일)",
    )

    print(
        "\n\n과최적화 금지: 위 수치를 보고 config.py의 상수(504/30-70/65-35/3일/10일/-15%/200일)를 "
        "다시 미세조정하지 마십시오. v2가 Buy & Hold를 넘지 못한다면 '구리는 강한 추세 자산이라 "
        "타이밍 전략이 Buy & Hold를 이기기 어렵다'는 결론을 그대로 보고하십시오."
    )


if __name__ == "__main__":
    main()
