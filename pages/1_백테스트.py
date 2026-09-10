"""Backtest page: the weighted copper_friendly score (DXY/WTI/gold-copper
ratio always; China/US PMI added once enough monthly history has
accumulated) drives a buy/sell strategy on copper (HG=F), compared to a
same-period Buy & Hold benchmark.
"""

from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from copper_dashboard import backtest, comex_store, config
from copper_dashboard.timeutil import today_kst

STRATEGY_COLOR = "#b5651d"
BH_COLOR = "#1baf7a"
BUY_COLOR = "#2a78d6"
SELL_COLOR = "#e34948"
STRATEGY_LABEL = "신호전략"
BH_LABEL = "Buy & Hold"

DEFAULTS = {
    "bt_years": backtest.BACKTEST_YEARS,
    "bt_buy_cutoff": backtest.BUY_SCORE_CUTOFF,
    "bt_sell_cutoff": backtest.SELL_SCORE_CUTOFF,
    "bt_gc_buy_threshold": float(backtest.GC_RATIO_BUY_THRESHOLD),
    "bt_gc_sell_threshold": float(backtest.GC_RATIO_SELL_THRESHOLD),
    "bt_use_new_high_buy": False,
    "bt_entry_delay_days": 0,
    "bt_exit_delay_days": 0,
    "bt_min_holding_days": 0,
}
for _key, _default in DEFAULTS.items():
    st.session_state.setdefault(_key, _default)

st.title("신호 기반 매매 전략 백테스트")
st.caption(
    "가중 점수제(copper_friendly, 0~100)가 설정한 임계값을 넘나들 때 매수/매도하는 규칙을, "
    "동일 시작일의 Buy & Hold와 비교합니다."
)

with st.expander("전략 규칙 보기"):
    st.markdown(
        """
- **매수** (미보유 상태일 때만): 종합 점수(copper_friendly)가 아래 "매수 조건" 카드의 임계값
  이상이면 — 고급 설정의 매수 지연일수만큼 기다린 뒤 체결. **또는** (고급 설정에서 켠 경우)
  구리(HG=F) 신고가 갱신 — 지연 없이 당일 즉시 매수
- **매도** (보유 상태일 때만): 종합 점수가 "매도 조건" 카드의 임계값 이하로 떨어지면 — 고급
  설정의 매도 지연일수만큼 기다린 뒤 체결
- 지연이 설정된 신호는 조건 충족 후 첫 거래일 **종가**로 체결됩니다(지연일수 0이면 신호 당일
  종가에 즉시 체결, 지연 기간 중 조건 재확인 없음)
- 고급 설정의 **최소 보유일수**를 설정하면, 매수 후 그 일수가 지나기 전까지는 매도 조건을
  아예 확인하지 않습니다
- 분석 기간: 아래에서 설정한 오늘 기준 최근 **{years}년** (3~10년 조정 가능, 이동평균 계산용으로
  그 이전 {buffer}캘린더일치 데이터를 추가로 사용)
        """.format(years=int(st.session_state["bt_years"]), buffer=backtest.BUFFER_DAYS)
    )

header_col, reset_col = st.columns([5, 1])
with header_col:
    st.subheader("분석 기간 · 매수·매도 조건")
with reset_col:
    st.write("")
    if st.button("↺ 기본값으로 초기화", use_container_width=True):
        for _key, _default in DEFAULTS.items():
            st.session_state[_key] = _default
        st.rerun()

years = st.number_input(
    "분석 기간 (최근 N년)",
    min_value=backtest.MIN_BACKTEST_YEARS,
    max_value=backtest.MAX_BACKTEST_YEARS,
    step=1,
    key="bt_years",
    help="이 페이지의 백테스트 결과에만 영향을 줍니다 — 메인 대시보드의 지표별 그래프는 이 값과 "
    "무관하게 항상 고정된 기간으로 표시됩니다.",
)

buy_card, sell_card = st.columns(2)
with buy_card:
    with st.container(border=True):
        st.markdown("#### 🔵 매수 조건")
        buy_cutoff = st.number_input(
            "종합 점수 임계값 (이상)",
            min_value=0.0, max_value=100.0, step=1.0, key="bt_buy_cutoff",
            help="copper_friendly 점수(0~100)가 이 값 이상이면 매수 신호.",
        )
with sell_card:
    with st.container(border=True):
        st.markdown("#### 🔴 매도 조건")
        sell_cutoff = st.number_input(
            "종합 점수 임계값 (이하)",
            min_value=0.0, max_value=100.0, step=1.0, key="bt_sell_cutoff",
            help="copper_friendly 점수가 이 값 이하로 떨어지면 매도 신호.",
        )

if sell_cutoff >= buy_cutoff:
    st.warning("매도 임계값이 매수 임계값보다 크거나 같습니다. 매수 즉시 매도 조건도 함께 만족할 수 있습니다.")

with st.expander("⚙️ 고급 설정 (지연일수 · 최소 보유일수 · 금/구리비율 임계값 · 신고가 갱신)"):
    adv_col1, adv_col2 = st.columns(2)
    with adv_col1:
        st.markdown("**매수 관련**")
        entry_delay_days = st.number_input(
            "매수 지연일수 (일)", min_value=0, max_value=180, step=1, key="bt_entry_delay_days",
        )
        st.caption(f"≈ {entry_delay_days / 30:.1f}개월 후 매수")
        use_new_high_buy = st.checkbox(
            "신고가 갱신 시 매수 조건 추가", key="bt_use_new_high_buy",
            help="켜면 구리(HG=F) 종가가 분석 기간 내 최고가를 새로 경신하는 날 그 즉시 매수합니다.",
        )
        gc_buy_threshold = st.number_input(
            "금/구리비율 매수 우호적 임계값 (이하)",
            min_value=1.0, max_value=1000.0, step=1.0, key="bt_gc_buy_threshold",
            help="점수 계산 시 금/구리비율이 이 값 이하이면 +1 방향으로 반영됩니다.",
        )
    with adv_col2:
        st.markdown("**매도 관련**")
        exit_delay_days = st.number_input(
            "매도 지연일수 (일)", min_value=0, max_value=180, step=1, key="bt_exit_delay_days",
        )
        st.caption(f"≈ {exit_delay_days / 30:.1f}개월 후 매도")
        min_holding_days = st.number_input(
            "매수 후 최소 보유일수 (일)", min_value=0, max_value=1825, step=1, key="bt_min_holding_days",
        )
        st.caption(f"≈ {min_holding_days / 30:.1f}개월간 매도 조건을 무시하고 무조건 보유")
        gc_sell_threshold = st.number_input(
            "금/구리비율 매수 비우호적 임계값 (이상)",
            min_value=1.0, max_value=1000.0, step=1.0, key="bt_gc_sell_threshold",
            help="점수 계산 시 금/구리비율이 이 값 이상이면 -1 방향으로 반영됩니다.",
        )

refresh_clicked = st.button("데이터 새로고침 (오늘 기준으로 다시 수집)")


@st.cache_data(ttl=3600, show_spinner="데이터를 내려받는 중입니다...")
def load_signals(as_of_iso: str, years: int) -> pd.DataFrame:
    return backtest.prepare_signals(as_of=date.fromisoformat(as_of_iso), years=years)


if refresh_clicked:
    st.cache_data.clear()

try:
    signals_df = load_signals(today_kst().isoformat(), int(years))
    # gold_copper_ratio direction depends on the (adjustable) ratio thresholds,
    # so it's recomputed here rather than cached inside load_signals — every
    # other column (dxy/wti/pmi direction, price data) is threshold-independent.
    import numpy as np

    signals_df = signals_df.copy()
    signals_df["gold_copper_ratio_direction"] = np.select(
        [signals_df["gold_copper_ratio"] <= gc_buy_threshold, signals_df["gold_copper_ratio"] >= gc_sell_threshold],
        [1, -1],
        default=0,
    )
    active_weights = dict(signals_df.attrs.get("active_weights", {}))
    max_score = sum(active_weights.values())
    raw_score = sum(signals_df[f"{k}_direction"] * w for k, w in active_weights.items())
    signals_df["raw_score"] = raw_score
    signals_df["copper_friendly_score"] = (raw_score + max_score) / (2 * max_score) * 100.0

    result = backtest.simulate(
        signals_df,
        entry_delay_days=int(entry_delay_days),
        exit_delay_days=int(exit_delay_days),
        use_new_high_buy=use_new_high_buy,
        buy_cutoff=float(buy_cutoff),
        sell_cutoff=float(sell_cutoff),
        min_holding_days=int(min_holding_days),
    )
except Exception as exc:
    st.error(f"백테스트를 실행하지 못했습니다: {exc}")
    st.stop()

pmi_included = signals_df.attrs.get("pmi_included", {"china_pmi": False, "us_pmi": False})
included_labels = [config.INDICATOR_META[k]["label"] for k in ("dxy", "wti", "gold_copper_ratio")]
included_labels += [
    config.INDICATOR_META[k]["label"] for k, included in pmi_included.items() if included
]
excluded_pmi_labels = [
    config.INDICATOR_META[k]["label"] for k, included in pmi_included.items() if not included
]
st.info(
    f"이번 백테스트에 반영된 지표: {', '.join(included_labels)}"
    + (
        f" · 제외된 지표(월간 데이터 {backtest.MIN_PMI_MONTHS_FOR_BACKTEST}개월 미만 축적): "
        f"{', '.join(excluded_pmi_labels)}"
        if excluded_pmi_labels
        else ""
    )
)
st.caption(config.FOOTNOTES["china_pmi"] if "china_pmi" in included_labels or excluded_pmi_labels else "")

comex_days = comex_store.days_of_history()
comex_first = comex_store.first_collection_date() or "아직 없음"
st.caption(
    f"COMEX 재고 지표는 무료 히스토리컬 데이터가 없어 자동 축적 중입니다 (수집 시작일: "
    f"{comex_first}, 현재 {comex_days}일치). 충분한 데이터(권장: 1년 이상)가 쌓이면 백테스트에 "
    "포함할 예정입니다."
)

start_date = signals_df.index[0].date()
end_date = signals_df.index[-1].date()
st.caption(f"분석 기간: **{start_date} ~ {end_date}** (신호전략과 Buy & Hold 모두 이 기간의 첫날에 시작)")

# ---- 점수 분포 (컷오프 조정에 참고) ----
st.subheader("종합 점수 분포 (기간 내)")
st.caption("현재 설정한 매수/매도 임계값이 과거 점수 분포에서 어디쯤에 위치하는지 참고해 조정하세요.")
score_hist = (
    alt.Chart(pd.DataFrame({"score": signals_df["copper_friendly_score"]}))
    .mark_bar(color=STRATEGY_COLOR, opacity=0.75)
    .encode(
        x=alt.X("score:Q", bin=alt.Bin(maxbins=40), title="종합 점수 (0~100)"),
        y=alt.Y("count():Q", title="일수"),
    )
    .properties(height=220)
)
buy_rule = alt.Chart(pd.DataFrame({"x": [buy_cutoff]})).mark_rule(color=BUY_COLOR, strokeDash=[4, 4]).encode(x="x:Q")
sell_rule = alt.Chart(pd.DataFrame({"x": [sell_cutoff]})).mark_rule(color=SELL_COLOR, strokeDash=[4, 4]).encode(x="x:Q")
st.altair_chart(score_hist + buy_rule + sell_rule, use_container_width=True)
pct_above_buy = float((signals_df["copper_friendly_score"] >= buy_cutoff).mean())
pct_below_sell = float((signals_df["copper_friendly_score"] <= sell_cutoff).mean())
st.caption(
    f"매수 임계값({buy_cutoff:g}) 이상인 날: 전체의 {pct_above_buy:.1%} · "
    f"매도 임계값({sell_cutoff:g}) 이하인 날: 전체의 {pct_below_sell:.1%}"
)

# ---- 1. 요약 지표 ----
m = result["metrics"]
equity = result["equity_curve"]
bh_equity = result["bh_equity_curve"]
yearly = result["yearly_returns"]
trades = result["trades"]

st.subheader("요약 지표")
col1, col2, col3 = st.columns(3)
col1.metric("총 거래 횟수", f"{m['closed_trade_count']}회")
col1.metric("승률", f"{m['win_rate']:.1%}" if m["win_rate"] is not None else "-")
col2.metric(f"누적수익률 ({STRATEGY_LABEL})", f"{m['strategy_total_return']:.1%}")
col2.metric(f"누적수익률 ({BH_LABEL})", f"{m['bh_total_return']:.1%}")
col3.metric(
    f"연환산수익률(CAGR) ({STRATEGY_LABEL}, 순수투자기간)",
    f"{m['strategy_cagr']:.1%}" if m["strategy_cagr"] is not None else "-",
)
col3.metric(f"연환산수익률(CAGR) ({BH_LABEL}, 전체기간)", f"{m['bh_cagr']:.1%}")
st.metric("최대 낙폭 (MDD, 신호전략)", f"{m['max_drawdown']:.1%}")

if m["has_open_position"]:
    st.info("현재 포지션을 보유 중입니다. 마지막 거래는 미청산 상태이며 오늘 종가 기준 평가손익입니다.")
if m["strategy_cagr"] is None:
    st.caption("ℹ️ 신호전략이 이 기간 동안 한 번도 매수 신호를 내지 않아 CAGR을 계산할 수 없습니다.")

# ---- 2. CAGR 비교 ----
st.subheader("연환산수익률(CAGR) 비교")
cagr_labels = [f"{STRATEGY_LABEL} (순수투자기간)", f"{BH_LABEL} (전체기간)"]
cagr_df = pd.DataFrame({"series": cagr_labels, "cagr": [m["strategy_cagr"] or 0.0, m["bh_cagr"] or 0.0]})
cagr_chart = (
    alt.Chart(cagr_df)
    .mark_bar(size=70)
    .encode(
        x=alt.X("series:N", title=None, sort=None),
        y=alt.Y("cagr:Q", title="CAGR", axis=alt.Axis(format="%")),
        color=alt.Color("series:N", legend=None, scale=alt.Scale(domain=cagr_labels, range=[STRATEGY_COLOR, BH_COLOR])),
        tooltip=[alt.Tooltip("series:N", title="전략"), alt.Tooltip("cagr:Q", title="CAGR", format=".2%")],
    )
    .properties(height=280)
)
st.altair_chart(cagr_chart, use_container_width=True)

# ---- 3. 누적수익률 ----
st.subheader("누적수익률")
cum_df = pd.DataFrame(
    {
        "date": equity.index,
        STRATEGY_LABEL: equity.values - 1.0,
        BH_LABEL: bh_equity.reindex(equity.index).values - 1.0,
    }
).melt("date", var_name="series", value_name="return")

line_chart = (
    alt.Chart(cum_df)
    .mark_line(strokeWidth=2)
    .encode(
        x=alt.X("date:T", axis=alt.Axis(title=None, format="%Y", tickCount="year")),
        y=alt.Y("return:Q", title="누적수익률", axis=alt.Axis(format="%")),
        color=alt.Color("series:N", title=None, scale=alt.Scale(domain=[STRATEGY_LABEL, BH_LABEL], range=[STRATEGY_COLOR, BH_COLOR])),
        tooltip=[
            alt.Tooltip("date:T", title="날짜"),
            alt.Tooltip("series:N", title="전략"),
            alt.Tooltip("return:Q", title="누적수익률", format=".1%"),
        ],
    )
)

marker_rows = []
for t in trades:
    marker_rows.append(
        {"date": t["entry_date"], "구분": "매수", "return": float(equity.loc[t["entry_date"]]) - 1.0,
         "가격": round(t["entry_price"], 2), "사유": t["entry_reason"] or "-"}
    )
    if not t["open"]:
        marker_rows.append(
            {"date": t["exit_date"], "구분": "매도", "return": float(equity.loc[t["exit_date"]]) - 1.0,
             "가격": round(t["exit_price"], 2), "사유": t["exit_reason"] or "-"}
        )
marker_df = pd.DataFrame(marker_rows)

if not marker_df.empty:
    markers = (
        alt.Chart(marker_df)
        .mark_point(size=90, filled=True, opacity=0.9)
        .encode(
            x="date:T", y="return:Q",
            color=alt.Color("구분:N", title=None, scale=alt.Scale(domain=["매수", "매도"], range=[BUY_COLOR, SELL_COLOR])),
            shape=alt.Shape("구분:N", scale=alt.Scale(domain=["매수", "매도"], range=["triangle-up", "triangle-down"])),
            tooltip=[
                alt.Tooltip("date:T", title="날짜"), alt.Tooltip("구분:N", title="구분"),
                alt.Tooltip("가격:Q", title="체결가"), alt.Tooltip("return:Q", title="당시 누적수익률", format=".1%"),
                alt.Tooltip("사유:N", title="사유"),
            ],
        )
    )
    combined_chart = alt.layer(line_chart, markers).resolve_scale(color="independent", shape="independent")
else:
    combined_chart = line_chart

st.altair_chart(combined_chart.properties(height=380).interactive(), use_container_width=True)
st.caption("▲ 파란색 = 매수 시점, ▼ 빨간색 = 매도 시점 (거래 내역 표 참고)")

# ---- 4. 연도별 연환산수익률 ----
st.subheader("연도별 연환산수익률")
yearly_long = yearly.melt(
    id_vars=["year", "days_span"], value_vars=["strategy_return_annualized", "bh_return_annualized"],
    var_name="series", value_name="return",
)
yearly_long["series"] = yearly_long["series"].map(
    {"strategy_return_annualized": STRATEGY_LABEL, "bh_return_annualized": BH_LABEL}
)
raw_map = {}
for _, row in yearly.iterrows():
    raw_map[(row["year"], STRATEGY_LABEL)] = row["strategy_return"]
    raw_map[(row["year"], BH_LABEL)] = row["bh_return"]
yearly_long["raw_return"] = [raw_map[(y, s)] for y, s in zip(yearly_long["year"], yearly_long["series"])]

bar_chart = (
    alt.Chart(yearly_long)
    .mark_bar()
    .encode(
        x=alt.X("year:O", title=None),
        xOffset=alt.XOffset("series:N", sort=[STRATEGY_LABEL, BH_LABEL]),
        y=alt.Y("return:Q", title="연환산수익률", axis=alt.Axis(format="%")),
        color=alt.Color("series:N", title=None, scale=alt.Scale(domain=[STRATEGY_LABEL, BH_LABEL], range=[STRATEGY_COLOR, BH_COLOR])),
        tooltip=[
            alt.Tooltip("year:O", title="연도"), alt.Tooltip("series:N", title="전략"),
            alt.Tooltip("return:Q", title="연환산수익률", format=".1%"),
            alt.Tooltip("raw_return:Q", title="해당 연도 실제 수익률", format=".1%"),
            alt.Tooltip("days_span:Q", title="해당 연도 일수"),
        ],
    )
    .properties(height=340)
)
st.altair_chart(bar_chart, use_container_width=True)
st.caption(
    f"{int(yearly['year'].iloc[0])}년과 {int(yearly['year'].iloc[-1])}년은 분석 기간에 걸친 "
    "부분연도이며, 그 부분 기간의 실제 수익률을 연 단위로 환산한 값입니다."
)

# ---- 5. 거래 내역 ----
st.subheader("거래 내역")
if trades:
    trade_rows = [
        {
            "매수일": t["entry_date"].date(), "매수가": round(t["entry_price"], 2), "매수 사유": t["entry_reason"] or "-",
            "매도일": t["exit_date"].date() if t["exit_date"] is not None else "미청산(보유 중)",
            "매도가": round(t["exit_price"], 2), "매도 사유": t["exit_reason"] or ("미청산" if t["open"] else "-"),
            "보유일수": t["hold_days"], "구간수익률": f"{t['period_return']:.2%}" + (" (평가)" if t["open"] else ""),
        }
        for t in trades
    ]
    st.dataframe(pd.DataFrame(trade_rows), use_container_width=True, hide_index=True)
else:
    st.caption("이 기간 동안 매수 신호가 발생하지 않아 거래 내역이 없습니다.")

st.caption(
    "⚠️ 본 백테스트는 과거 데이터에 기반한 시뮬레이션 결과이며 미래 성과를 보장하지 않습니다. "
    "거래비용·세금·슬리피지는 반영되어 있지 않고, 표본 기간이 짧아 과최적화(overfitting) 위험이 "
    "있습니다. 다변량 모델 기준 표본외 설명력(R²)은 최대 약 18.5% 수준(2002~2014 LME 데이터 기준 "
    "계량연구)이라는 결과도 있어, 이 점수 하나로 구리값을 예측하는 데는 뚜렷한 한계가 있습니다."
)
