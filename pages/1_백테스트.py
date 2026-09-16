"""Backtest page: DXY/FXI MA breakout signal (green_count) + 52-week
new-high/new-low triggers, vs. a same-period Buy & Hold benchmark.

This is a structural port of the Gold dashboard's own backtest page, with two
deliberate simplifications made for this project (disclosed in the
completion report, not asked about first — see COPPER_TRADING_LOGIC.md 9-10
장 and this file's own comments):
1. No 국면(regime) shading / participation-rate cards — regime.py has no
   copper equivalent in this first pass (excluded per explicit instruction:
   "매매 로직이 먼저 안정적으로 검증된 다음에... 지금은 손대지 마").
2. The cumulative-return chart's net/gross-fee and strategy/BH line toggles
   are plain Streamlit checkboxes (a rerun swaps which traces are built)
   instead of Gold's zoom-state-preserving client-side JS/iframe trick — this
   keeps the chart a single st.plotly_chart call with no custom HTML/JS, at
   the cost of losing zoom-position preservation across a toggle click.
Only one domestic instrument exists for copper (KODEX 구리선물(H)), so unlike
Gold's page there is no "종목" selector — the fee section always shows that
ETF's own two cost inputs directly.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from copper_dashboard import backtest, config
from copper_dashboard import timeseries
from copper_dashboard.timeutil import today_kst

STRATEGY_COLOR = "#eb6834"
BH_COLOR = "#1baf7a"
BUY_COLOR = "#2a78d6"
SELL_COLOR = "#e34948"
STRATEGY_HYBRID_COLOR = "#7b5ea8"
STRATEGY_HYBRID_NONHOLDING_COLOR = "#c9bfe0"
STRATEGY_LABEL = "신호전략"
BH_LABEL = "Buy & Hold"

DEFAULTS = {
    "bt_years": backtest.BACKTEST_YEARS,
    "bt_asof_years_ago": 0,
    "bt_buy_green_count": backtest.BUY_GREEN_COUNT,
    "bt_sell_green_count": backtest.SELL_GREEN_COUNT,
    "bt_use_new_high_trigger": backtest.DEFAULT_USE_FIFTY_TWO_WEEK_HIGH_TRIGGER,
    "bt_use_new_low_trigger": backtest.DEFAULT_USE_FIFTY_TWO_WEEK_LOW_TRIGGER,
    "bt_min_holding_days": backtest.DEFAULT_MIN_HOLDING_DAYS,
    "bt_bond_yield_pct": backtest.DEFAULT_BOND_ANNUAL_YIELD * 100.0,
    "bt_use_sell_noise_filter": False,
    "bt_use_daily_band_confirmation": backtest.DEFAULT_SELL_NOISE_USE_DAILY_BAND,
    "bt_sell_noise_filter_drop_pct": backtest.DEFAULT_SELL_NOISE_FILTER_DROP_PCT,
    "bt_use_buy_noise_filter": False,
    "bt_use_buy_daily_band_confirmation": backtest.DEFAULT_BUY_NOISE_USE_DAILY_BAND,
    "bt_buy_noise_filter_rise_pct": backtest.DEFAULT_BUY_NOISE_FILTER_RISE_PCT,
    "bt_apply_fees": False,
    "bt_buy_fee_pct": backtest.DEFAULT_BUY_FEE_PCT,
    "bt_sell_fee_pct": backtest.DEFAULT_SELL_FEE_PCT,
    "bt_etf_expense_ratio_pct": backtest.DEFAULT_ETF_ANNUAL_EXPENSE_RATIO_PCT,
}
for _key, _default in DEFAULTS.items():
    st.session_state.setdefault(_key, _default)

st.title("신호 기반 매매 전략 백테스트")
st.caption(
    "달러인덱스(DXY)·중국 대형주 ETF(FXI)의 이동평균 돌파 신호와 52주 신고가/신저가 갱신을 결합한 "
    "매수·매도 규칙을, 동일 시작일의 Buy & Hold와 비교합니다."
)

_copper_basis_options = [config.COPPER_PRICE_BASIS_INTL, config.COPPER_PRICE_BASIS_KRX]
st.session_state.setdefault(config.COPPER_PRICE_BASIS_STATE_KEY, config.COPPER_PRICE_BASIS_DEFAULT)
copper_price_basis = st.radio(
    "구리 가격 기준",
    options=_copper_basis_options,
    format_func=lambda v: config.COPPER_PRICE_BASIS_LABELS[v],
    index=_copper_basis_options.index(st.session_state[config.COPPER_PRICE_BASIS_STATE_KEY]),
    key="_copper_price_basis_widget_backtest",
    horizontal=True,
    help="이 페이지 전체(이동평균·장기추세 필터·매수매도 신호·백테스트·요약지표·그래프)가 이 "
    "기준으로 다시 계산됩니다. 대시보드 페이지와 상태를 공유하므로 여기서 바꾸면 그쪽에도 "
    "반영됩니다.",
)
st.session_state[config.COPPER_PRICE_BASIS_STATE_KEY] = copper_price_basis
st.caption(
    "② KODEX 구리선물(H)(138910)은 국제 구리 시세에 환율을 곱해 환산한 값이 아니라, KRX에 실제 "
    "상장된 이 ETF의 실거래가(KRW)를 그대로 사용합니다(출처: Naver 증권). 이 상품은 COMEX 구리 "
    "선물 연동지수를 환헤지(H)하여 추종합니다."
)

with st.expander("전략 규칙 보기"):
    st.markdown(
        """
- **매수** (미보유 상태일 때만): 아래 "매수 조건" 카드의 `green_count ≥ 임계값` — 지연 없이
  **당일 즉시 매수**. **또는** (고급 설정의 **52주 신고가 갱신 시 매수**가 켜져 있을 때만,
  기본값 ON) 구리 종가가 직전 365일(역일) 중 최고 종가를 처음으로 넘어서는 날(52주 신고가
  신규 경신일) — 이것도 당일 즉시 매수, 자체 빈도 제한 없음. 아무 조건이나 먼저 만족하면
  매수합니다
- **매도** (보유 상태일 때만): "매도 조건" 카드의 `green_count ≤ 임계값` — 지연 없이 **당일
  즉시 매도**. **또는** (고급 설정의 **52주 신저가 갱신 시 매도**가 켜져 있을 때만, 기본값 ON)
  구리 종가가 직전 365일(역일) 중 최저 종가보다 낮아지는 날(52주 신저가 신규 경신일) — 이것도
  당일 즉시 매도, 자체 빈도 제한 없음. 둘 중 아무 조건이나 먼저 만족하면 매도합니다
- 모든 매수·매도 조건은 지연 없이 신호 당일 종가에 즉시 체결됩니다
- 고급 설정의 **최소 보유일수**(역일/달력일 기준, 주말·공휴일 관계없이 매수일로부터의 날짜
  차이로 계산)를 설정하면, 매수 후 그 일수가 지나기 전까지는 매도 조건(green_count·52주
  신저가 갱신 모두)을 아예 확인하지 않습니다
- 고급 설정의 **상승추세 중 매도신호 노이즈 필터** (기본값 OFF)는 매도신호가 실제로
  체결되기 직전(green_count 또는 52주 신저가 갱신)에 개입합니다.
  그날(D0) 종가가 180일(역일) 이동평균보다 {sell_buf:g}% 이상 높을 때만 작동하며
  (미만이면 이 필터 없이 항상 그대로 즉시 매도), 작동하면 D0의 매도신호는 무시하고 관찰을
  시작합니다. 매도 실행 여부를 확인하는 방식은 **매도 확인 - 매일 갱신 2시그마 밴드** 설정에
  따라 둘 중 하나입니다:
  - **켜짐(기본값)**: D0 이후 1~7거래일은 하락폭과 무관하게 **무조건 보류**합니다. 8거래일차부터
    21거래일차(약 3주)까지는 매일, 그날까지 경과한 거래일수의 제곱근에 비례해 넓어지는 확인
    밴드를 계산합니다 — `그날의 밴드(%) = 2 × 일간표준편차 × √(경과 거래일수)`, 일간표준편차는
    구리(HG=F)의 ~20년 월간 변동성({monthly_vol:g}%)을 √21(한 달 거래일수)로 나눠 환산(예:
    8거래일차 -{b8:.2f}%, 10거래일차 -{b10:.2f}%, 14거래일차 -{b14:.2f}%, 21거래일차
    -{b21:.2f}%). 종가가 D0 종가 대비 그날의 밴드만큼(또는 그 이상) 하락한 **첫날** 즉시
    매도합니다. 21거래일 동안 한 번도 도달하지 못하면 관찰을 종료하고 D0의 신호는 없었던
    것으로 처리합니다.
  - **꺼짐**: D0+7일(역일 기준) 고정 시점의 종가만을 D0 종가와 비교합니다 — "매도 확인
    하락률"(기본 5%) 이상 낮으면 그날 매도, 그만큼 낮지 않으면 관찰모드를 해제하고 D0의 신호는
    없었던 것으로 처리합니다
- 고급 설정의 **하락추세 중 매수신호 노이즈 필터** (기본값 OFF)는 위 매도신호 노이즈 필터를
  방향만 반대로 완전히 대칭시킨 조건입니다(180일선 -{buy_buf:g}% 이하일 때만 작동, 매일 갱신
  2시그마 밴드는 위와 동일한 공식으로 상승 기준 8거래일차 +{b8:.2f}% ~ 21거래일차 +{b21:.2f}%).
- 고급 설정의 **수수료**(② KODEX 구리선물(H) 선택 시에만 적용, ① 국제 구리 시세에는 적용되지
  않음)는 KODEX 구리선물(H)의 실제 비용 구조를 반영합니다: **매수/매도 수수료**(각 기본
  {buy_fee:g}%, 편도, 2026년 기준 국내 증권사 상시요율 중 최저가(하나증권) — CONFIRMED, 매매가
  일어날 때마다 1회성 차감)와 **총보수**(기본 연 {expense:g}%, 삼성자산운용 공식 펀드 팩트시트
  기준 — CONFIRMED, 보유 잔량에 대해 일할 복리 환산해 매일 누적 적용). 이 모델은 15년에 30건
  미만(연 2건 미만)의 저빈도 매매 구조라, 매매수수료가 결과에 미치는 영향은 크지 않고
  (누적해도 총 1%p 미만) 매일 무조건 차감되는 총보수 쪽이 15년 누적 기준 복리로 훨씬 큰
  영향(약 9~10%p)을 줍니다. "수수료 반영" 체크박스로 전부 껐다 켤 수 있고, 차트에서는
  이 값과 별개로 수수료 반영/미반영 곡선을 토글로 비교할 수 있습니다
- 분석 기간: **{years}년** (1~15년 조정 가능, 이동평균 계산용으로 그 이전 {buffer}캘린더일치
  데이터를 추가로 사용). 기본은 오늘을 기준으로 최근 {years}년이지만, "기준일 (오늘로부터
  N년 전)"을 0보다 크게 설정하면 분석 종료일 자체가 그만큼 과거로 이동합니다 (현재 설정:
  기준일 {asof_years_ago}년 전)
        """.format(
            sell_buf=backtest.DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT,
            buy_buf=backtest.DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT,
            monthly_vol=backtest.NOISE_BAND_MONTHLY_VOL_PCT,
            b8=backtest.noise_band_pct(8) * 100.0,
            b10=backtest.noise_band_pct(10) * 100.0,
            b14=backtest.noise_band_pct(14) * 100.0,
            b21=backtest.noise_band_pct(21) * 100.0,
            buy_fee=backtest.DEFAULT_BUY_FEE_PCT,
            expense=backtest.DEFAULT_ETF_ANNUAL_EXPENSE_RATIO_PCT,
            years=int(st.session_state["bt_years"]),
            buffer=backtest.BUFFER_DAYS,
            asof_years_ago=int(st.session_state["bt_asof_years_ago"]),
        )
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

asof_col, years_col = st.columns(2)
with asof_col:
    asof_years_ago = st.number_input(
        "기준일 (오늘로부터 N년 전)",
        min_value=0,
        max_value=backtest.MAX_BACKTEST_YEARS,
        step=1,
        key="bt_asof_years_ago",
        help="0이면(기본값) 오늘을 기준으로 분석 종료일을 잡습니다. N을 입력하면 분석 종료일 "
        "자체가 오늘로부터 N년 전으로 이동하고, 분석 시작일은 거기서 다시 '분석 기간'만큼 더 "
        "과거로 이동합니다.",
    )
with years_col:
    years = st.number_input(
        "분석 기간 (기준일로부터 N년)",
        min_value=backtest.MIN_BACKTEST_YEARS,
        max_value=backtest.MAX_BACKTEST_YEARS,
        step=1,
        key="bt_years",
        help="이 페이지의 백테스트 결과(거래 내역·승률·CAGR·아래 그래프)에만 영향을 줍니다 — "
        "메인 대시보드의 지표별 그래프는 이 값과 무관하게 항상 고정된 기간으로 표시됩니다.",
    )

as_of_date = today_kst() - timedelta(days=int(asof_years_ago) * 365)

if copper_price_basis == config.COPPER_PRICE_BASIS_KRX and timeseries.copper_window_would_clamp_to_krx(
    as_of_date, int(years), backtest.BUFFER_DAYS
):
    st.info(
        f"KODEX 구리선물(H)은 {config.KRX_COPPER_ETF_EARLIEST_DATE} 이후 데이터만 존재합니다. "
        f"분석 시작일이 자동으로 {config.KRX_COPPER_ETF_EARLIEST_DATE}로 조정됩니다(이동평균 "
        "계산용 사전 데이터가 짧아지는 만큼, 분석 기간 첫 구간의 180일선/장기추세 필터·"
        "52주 신고가·신저가 판정 신뢰도가 낮을 수 있습니다)."
    )

buy_card, sell_card = st.columns(2)
with buy_card:
    with st.container(border=True):
        st.markdown("#### 🔵 매수 조건")
        buy_green_count = st.number_input(
            "green_count 임계값 (이상)",
            min_value=0, max_value=6, step=1, key="bt_buy_green_count",
            help="DXY·FXI × 7/30/90일(역일) 이평선, 총 6개 셀 중 구리 가격에 우호적인 셀 수가 "
            "이 값 이상이면 그날 즉시 매수 신호.",
        )
with sell_card:
    with st.container(border=True):
        st.markdown("#### 🔴 매도 조건")
        sell_green_count = st.number_input(
            "green_count 임계값 (이하)",
            min_value=0, max_value=6, step=1, key="bt_sell_green_count",
            help="구리 가격에 우호적인 셀 수가 이 값 이하로 떨어지면 그날 즉시 매도 신호.",
        )

if sell_green_count >= buy_green_count:
    st.warning(
        "매도 임계값이 매수 임계값보다 크거나 같습니다. 매수 즉시 매도 조건도 함께 만족해 "
        "거의 바로 청산될 수 있습니다."
    )

with st.expander("⚙️ 고급 설정 (최소 보유일수 등 — 기본값 그대로 둬도 무방)"):
    st.caption("모든 매수·매도 조건은 지연 없이 항상 신호 당일 즉시 체결됩니다.")
    adv_buy_col, adv_sell_col = st.columns(2)
    with adv_buy_col:
        st.markdown("**매수 관련**")
        use_new_high_trigger = st.checkbox(
            "52주 신고가 갱신 시 매수",
            key="bt_use_new_high_trigger",
            help="종가가 직전 365일(역일) 중 최고 종가보다 높아지는 날(신규 52주 신고가 경신일) "
            "즉시 매수합니다(green_count와 무관하게 추가로 작동).",
        )
        use_buy_noise_filter = st.checkbox(
            "하락추세 중 매수신호 노이즈 필터 (180일선 -5% 이하)",
            key="bt_use_buy_noise_filter",
            help="매수신호가 발생한 날(D0) 종가가 180일 이동평균보다 "
            f"{backtest.DEFAULT_BUY_NOISE_FILTER_BUFFER_PCT:g}% 이상 낮을 때만 작동합니다. 켜두면 D0의 "
            "매수신호는 무시하고 미보유를 유지하며, 아래 확인 방식에 따라 매수 여부를 확인합니다.",
        )
        use_buy_daily_band_confirmation = st.checkbox(
            "매수 확인 - 매일 갱신 2시그마 밴드",
            key="bt_use_buy_daily_band_confirmation",
            disabled=not use_buy_noise_filter,
            help="켜두면(기본값): D0+1~7거래일은 무조건 보류, 8~21거래일차까지 매일 넓어지는 "
            "밴드(√t 법칙)로 확인합니다. 끄면 아래 '매수 확인 상승률'을 사용하는 D0+7일 고정 "
            "시점 방식으로 동작합니다.",
        )
        buy_noise_filter_rise_pct = st.number_input(
            "매수 확인 상승률 (%, D0 대비 D0+7일 — 위 2시그마 밴드가 꺼져 있을 때만 사용)",
            min_value=0.0, max_value=50.0, step=0.5, key="bt_buy_noise_filter_rise_pct",
            disabled=not use_buy_noise_filter or use_buy_daily_band_confirmation,
        )

    with adv_sell_col:
        st.markdown("**매도 관련**")
        use_new_low_trigger = st.checkbox(
            "52주 신저가 갱신 시 매도",
            key="bt_use_new_low_trigger",
            help="종가가 직전 365일(역일) 중 최저 종가보다 낮아지는 날(신규 52주 신저가 경신일) "
            "즉시 매도합니다(green_count 조건과 무관하게 추가로 작동).",
        )
        use_sell_noise_filter = st.checkbox(
            "상승추세 중 매도신호 노이즈 필터 (180일선 +5% 이상)",
            key="bt_use_sell_noise_filter",
            help="매도신호가 발생한 날(D0) 종가가 180일 이동평균보다 "
            f"{backtest.DEFAULT_SELL_NOISE_FILTER_BUFFER_PCT:g}% 이상 높을 때만 작동합니다. 켜두면 D0의 "
            "매도신호는 무시하고 보유를 유지하며, 아래 확인 방식에 따라 매도 여부를 확인합니다.",
        )
        use_daily_band_confirmation = st.checkbox(
            "매도 확인 - 매일 갱신 2시그마 밴드",
            key="bt_use_daily_band_confirmation",
            disabled=not use_sell_noise_filter,
            help="켜두면(기본값): D0+1~7거래일은 무조건 보류, 8~21거래일차까지 매일 넓어지는 "
            "밴드(√t 법칙)로 확인합니다. 끄면 아래 '매도 확인 하락률'을 사용하는 D0+7일 고정 "
            "시점 방식으로 동작합니다.",
        )
        sell_noise_filter_drop_pct = st.number_input(
            "매도 확인 하락률 (%, D0 대비 D0+7일 — 위 2시그마 밴드가 꺼져 있을 때만 사용)",
            min_value=0.0, max_value=50.0, step=0.5, key="bt_sell_noise_filter_drop_pct",
            disabled=not use_sell_noise_filter or use_daily_band_confirmation,
        )
        min_holding_days = st.number_input(
            "매수 후 최소 보유일수 (일, 역일 기준)",
            min_value=0, max_value=1825, step=1, key="bt_min_holding_days",
            help="매수 이후 이 일수가 지나기 전까지는 매도 조건(green_count·52주 신저가 갱신 "
            "모두)을 아예 확인하지 않습니다.",
        )
        st.caption(f"≈ {min_holding_days / 30:.1f}개월간 매도 조건을 무시하고 무조건 보유")

    st.markdown(
        "**수수료** (② KODEX 구리선물(H) 선택 시에만 적용 — ① 국제 구리 시세는 실물이 아닌 "
        "참고 가격이라 적용되지 않음)"
    )
    apply_fees = st.checkbox(
        "수수료 반영",
        key="bt_apply_fees",
        disabled=copper_price_basis != config.COPPER_PRICE_BASIS_KRX,
        help="체크를 해제하면 아래 입력값과 무관하게 전부 0으로 두고 계산합니다(입력값 자체는 "
        "그대로 남아있어 다시 체크하면 복원됩니다).",
    )
    fee_buy_col, fee_sell_col, fee_holding_col = st.columns(3)
    with fee_buy_col:
        buy_fee_pct = st.number_input(
            "매수 수수료 (%, 편도, CONFIRMED)",
            min_value=0.0, max_value=5.0, step=0.001, format="%.3f", key="bt_buy_fee_pct",
            disabled=copper_price_basis != config.COPPER_PRICE_BASIS_KRX or not apply_fees,
            help="매수 체결 시마다 그날 매수금액에 부과되는 1회성 수수료입니다(기본값 0.014% — "
            "2026년 기준 국내 증권사 상시요율(이벤트 미적용) 중 최저인 하나증권 공시 요율,"
            "1억원 이상 거래에서도 동일). 이 모델은 15년에 30건 미만(연 2건 미만)의 저빈도 매매라 "
            "이 수수료가 누적 결과에 미치는 영향은 크지 않습니다(15년 누적 왕복 기준 1%p 미만).",
        )
    with fee_sell_col:
        sell_fee_pct = st.number_input(
            "매도 수수료 (%, 편도, CONFIRMED)",
            min_value=0.0, max_value=5.0, step=0.001, format="%.3f", key="bt_sell_fee_pct",
            disabled=copper_price_basis != config.COPPER_PRICE_BASIS_KRX or not apply_fees,
            help="매도 체결 시마다 그날 매도금액에 부과되는 1회성 수수료입니다. Buy & Hold는 "
            "분석기간 종료 시점에 전량 매도한다고 가정해 이 수수료를 마지막 날 1회 반영합니다.",
        )
    with fee_holding_col:
        etf_expense_ratio_pct = st.number_input(
            "총보수 (%, 연율, CONFIRMED)",
            min_value=0.0, max_value=5.0, step=0.001, format="%.3f",
            key="bt_etf_expense_ratio_pct",
            disabled=copper_price_basis != config.COPPER_PRICE_BASIS_KRX or not apply_fees,
            help="KODEX 구리선물(H)의 연간 총보수입니다(기본값 0.68%, 삼성자산운용 공식 펀드 "
            "팩트시트(2025-06-30 기준) 기준 — 지정판매 0.001%+집합투자 0.599%+신탁 0.04%+"
            "일반사무 0.04%=0.68%). 매수/매도 수수료와 달리 실제 보유 기간에만(신호전략은 보유 "
            "중일 때만, Buy & Hold는 전체 기간) 발생하며, 이 모델은 매일 무조건 차감되는 구조라 "
            "15년 누적 시 복리로 약 9~10%p 수준의 훨씬 큰 영향을 줍니다. 계산 시에는 이 연율을 "
            "일할 복리 환산((1+연율)^(1/365)-1)해 하루 치 요율로 바꿔 매일 잔량에 누적 적용합니다.",
        )

bond_yield_pct = float(st.session_state["bt_bond_yield_pct"])


@st.cache_data(ttl=3600, show_spinner="데이터를 내려받는 중입니다...")
def load_signals(as_of_iso: str, years: int, copper_price_basis: str) -> pd.DataFrame:
    return backtest.prepare_signals(
        as_of=date.fromisoformat(as_of_iso), years=years, copper_price_basis=copper_price_basis
    )


def _format_copper_price(value: float, basis: str = copper_price_basis) -> str:
    if basis == config.COPPER_PRICE_BASIS_KRX:
        return f"{value:,.0f}원"
    return f"${value:,.4f}"


_fees_active = apply_fees and copper_price_basis == config.COPPER_PRICE_BASIS_KRX
effective_buy_fee_pct = float(buy_fee_pct) if _fees_active else 0.0
effective_sell_fee_pct = float(sell_fee_pct) if _fees_active else 0.0
# etf_expense_ratio_pct는 연율 입력값이므로 simulate()의 daily_holding_fee_pct
# (하루 치 요율) 계약에 맞춰 여기서만 일할 복리 환산한다.
_daily_holding_fee_pct = backtest.annual_to_daily_fee_pct(float(etf_expense_ratio_pct))
effective_daily_holding_fee_pct = _daily_holding_fee_pct if _fees_active else 0.0

_shared_sim_kwargs = dict(
    use_new_high_trigger=use_new_high_trigger,
    use_new_low_trigger=use_new_low_trigger,
    min_holding_days=int(min_holding_days),
    bond_annual_yield=float(bond_yield_pct) / 100.0,
    use_sell_noise_filter=use_sell_noise_filter,
    use_daily_band_confirmation=use_daily_band_confirmation,
    sell_noise_filter_drop_pct=float(sell_noise_filter_drop_pct),
    use_buy_noise_filter=use_buy_noise_filter,
    use_buy_daily_band_confirmation=use_buy_daily_band_confirmation,
    buy_noise_filter_rise_pct=float(buy_noise_filter_rise_pct),
)

try:
    signals = load_signals(as_of_date.isoformat(), int(years), copper_price_basis)
    result = backtest.simulate(
        signals,
        **_shared_sim_kwargs,
        buy_green_count=int(buy_green_count),
        sell_green_count=int(sell_green_count),
        buy_fee_pct=effective_buy_fee_pct,
        sell_fee_pct=effective_sell_fee_pct,
        daily_holding_fee_pct=effective_daily_holding_fee_pct,
    )
    result_gross = backtest.simulate(
        signals,
        **_shared_sim_kwargs,
        buy_green_count=int(buy_green_count),
        sell_green_count=int(sell_green_count),
        buy_fee_pct=0.0,
        sell_fee_pct=0.0,
        daily_holding_fee_pct=0.0,
    )
except Exception as exc:
    st.error(f"백테스트를 실행하지 못했습니다: {exc}")
    result = None
    result_gross = None

if result is not None:
    m = result["metrics"]
    equity = result["equity_curve"]
    bh_equity = result["bh_equity_curve"]
    holding_curve = result["holding_curve"]
    hybrid_equity = result["hybrid_equity_curve"]
    yearly = result["yearly_returns"]
    trades = result["trades"]

    bh_equity_gross = result_gross["bh_equity_curve"]
    hybrid_equity_gross = result_gross["hybrid_equity_curve"]

    start_date = equity.index[0].date()
    end_date = equity.index[-1].date()
    st.caption(
        f"분석 기간: **{start_date} ~ {end_date}** "
        "(신호전략과 Buy & Hold 모두 이 기간의 첫날에 시작 — 동일 시작일 비교)"
    )
else:
    m = None

st.subheader("요약 지표")
holding_fraction = (
    1.0 - m["non_holding_fraction"]
    if m is not None and m["non_holding_fraction"] is not None
    else None
)

overview_col, bh_col, strategy_held_col, strategy_hybrid_col = st.columns(4)
with overview_col:
    st.markdown("###### ① 매매 개요")
    st.metric("매매횟수", f"{m['closed_trade_count']}회" if m is not None else "-")
    st.metric(
        "보유기간",
        f"{holding_fraction:.1%}" if holding_fraction is not None else "-",
        help="분석 기간 전체(캘린더일 기준) 중 신호전략이 실제로 구리를 보유하고 있던 기간의 비중.",
    )
    st.metric("승률", f"{m['win_rate']:.1%}" if m is not None and m["win_rate"] is not None else "-")
with bh_col:
    st.markdown(f"###### ② {BH_LABEL}")
    st.metric("누적수익률", f"{m['bh_total_return']:.1%}" if m is not None else "-")
    st.metric("연환산수익률(CAGR)", f"{m['bh_cagr']:.1%}" if m is not None else "-")
with strategy_held_col:
    st.markdown(f"###### ③ {STRATEGY_LABEL} (보유기간)")
    st.metric(
        "누적수익률",
        f"{m['strategy_total_return']:.1%}" if m is not None else "-",
        help="보유 기간에만 투자했다고 가정한 누적수익률(미보유 기간은 반영하지 않음).",
    )
    st.metric(
        "연환산수익률(CAGR)",
        f"{m['strategy_cagr']:.1%}" if m is not None and m["strategy_cagr"] is not None else "-",
        help="실제로 구리를 보유했던 기간의 일수만 분모로 사용한 연환산수익률 — 분모가 달라 "
        "Buy & Hold의 CAGR과 직접 비교할 수 없습니다.",
    )
with strategy_hybrid_col:
    st.markdown(f"###### ④ {STRATEGY_LABEL} (미보유기간 기대수익률 포함)")
    st.metric(
        "누적수익률",
        f"{m['hybrid_total_return']:.1%}" if m is not None and m["hybrid_total_return"] is not None else "-",
        help="보유 기간엔 실제 구리 수익률을, 미보유 기간엔 아래 '기대수익률'을 적용해 이어 붙인 "
        "전체 분석기간 기준 누적수익률입니다.",
    )
    st.metric(
        "연환산수익률(CAGR)",
        f"{m['hybrid_cagr']:.1%}" if m is not None and m["hybrid_cagr"] is not None else "-",
        help="위 누적수익률을 분석 기간 전체를 기준으로 연환산한 값입니다.",
    )
    bond_yield_pct = st.number_input(
        "기대수익률 (연, %)",
        min_value=0.0, max_value=20.0, step=0.1, key="bt_bond_yield_pct",
        help="신호가 없어 구리를 보유하지 않는 기간 동안, 그 돈을 이 연이율로 운용했다고 "
        "가정합니다(예: 채권 매입). 값을 바꾸면 이 그룹의 누적수익률·CAGR이 바로 재계산됩니다.",
    )
    bond_yield_pct = float(bond_yield_pct)

if m is not None:
    if m["has_open_position"]:
        st.info(
            "현재 포지션을 보유 중입니다. 마지막 거래는 미청산 상태이며, 위 수익률·아래 거래 내역에 "
            "표시된 값은 오늘 종가 기준 평가손익입니다."
        )
    if m["strategy_cagr"] is None:
        st.caption("ℹ️ 신호전략이 이 기간 동안 한 번도 매수 신호를 내지 않아 CAGR을 계산할 수 없습니다.")

if result is None:
    st.stop()

# ---- 2. 누적수익률 라인차트 (+ 매수/매도 시점 마커) ----
st.subheader("누적수익률")
STRAT_HYBRID_LABEL = f"{STRATEGY_LABEL}(기대수익률 포함)"

chart_opt_col1, chart_opt_col2, chart_opt_col3 = st.columns(3)
with chart_opt_col1:
    show_strategy_line = st.checkbox(f"{STRAT_HYBRID_LABEL} 표시", value=True, key="bt_chart_show_strategy")
with chart_opt_col2:
    show_bh_line = st.checkbox(f"{BH_LABEL} 표시", value=True, key="bt_chart_show_bh")
with chart_opt_col3:
    show_net_fees = st.checkbox("수수료 반영 곡선 표시", value=True, key="bt_chart_show_net")

dates = equity.index
bh_returns = (bh_equity if show_net_fees else bh_equity_gross).reindex(dates).to_numpy() - 1.0
hybrid_returns = (hybrid_equity if show_net_fees else hybrid_equity_gross).reindex(dates).to_numpy() - 1.0
holding_bool = holding_curve.reindex(dates).fillna(False).to_numpy()
n_points = len(hybrid_returns)
point_in_holding = np.zeros(n_points, dtype=bool)
point_in_nonholding = np.zeros(n_points, dtype=bool)
if n_points > 1:
    seg_holding = holding_bool[:-1]
    point_in_holding[:-1] |= seg_holding
    point_in_holding[1:] |= seg_holding
    point_in_nonholding[:-1] |= ~seg_holding
    point_in_nonholding[1:] |= ~seg_holding
else:
    point_in_holding[:] = holding_bool
    point_in_nonholding[:] = ~holding_bool

nonholding_note = f"기대수익률 연 {bond_yield_pct:g}% 가정 적용 구간"
fee_note = "" if show_net_fees else "(수수료 미반영)"

fig = go.Figure()

if show_bh_line:
    fig.add_trace(
        go.Scatter(
            x=dates, y=bh_returns, mode="lines", name=BH_LABEL,
            line=dict(color=BH_COLOR, width=2),
            hovertemplate="%{x|%Y-%m-%d}<br>" + BH_LABEL + fee_note + ": %{y:.1%}<extra></extra>",
        )
    )

if show_strategy_line:
    fig.add_trace(
        go.Scatter(
            x=dates, y=np.where(point_in_holding, hybrid_returns, np.nan), mode="lines",
            name=STRAT_HYBRID_LABEL, connectgaps=False,
            line=dict(color=STRATEGY_HYBRID_COLOR, width=2),
            hovertemplate="%{x|%Y-%m-%d}<br>" + STRAT_HYBRID_LABEL + fee_note + ": %{y:.1%}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=dates, y=np.where(point_in_nonholding, hybrid_returns, np.nan), mode="lines",
            name=STRAT_HYBRID_LABEL, showlegend=False, connectgaps=False,
            line=dict(color=STRATEGY_HYBRID_NONHOLDING_COLOR, width=2, dash="dash"),
            customdata=np.full(n_points, nonholding_note),
            hovertemplate=(
                "%{x|%Y-%m-%d}<br>누적수익률" + fee_note + ": %{y:.1%}<br>%{customdata}<extra></extra>"
            ),
        )
    )

    marker_rows = []
    _hybrid_eq_for_markers = hybrid_equity if show_net_fees else hybrid_equity_gross
    for t in trades:
        marker_rows.append(
            {
                "date": t["entry_date"], "구분": "매수",
                "return": float(_hybrid_eq_for_markers.loc[t["entry_date"]]) - 1.0,
                "가격": t["entry_price"], "사유": t["entry_reason"] or "-",
            }
        )
        if not t["open"]:
            marker_rows.append(
                {
                    "date": t["exit_date"], "구분": "매도",
                    "return": float(_hybrid_eq_for_markers.loc[t["exit_date"]]) - 1.0,
                    "가격": t["exit_price"], "사유": t["exit_reason"] or "-",
                }
            )
    marker_df = pd.DataFrame(marker_rows)

    _price_tooltip_format = "$,.4f" if copper_price_basis == config.COPPER_PRICE_BASIS_INTL else ",.0f"
    _price_tooltip_title = "체결가" if copper_price_basis == config.COPPER_PRICE_BASIS_INTL else "체결가 (원)"

    if not marker_df.empty:
        for label, color, symbol in (("매수", BUY_COLOR, "triangle-up"), ("매도", SELL_COLOR, "triangle-down")):
            sub = marker_df[marker_df["구분"] == label]
            if sub.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=sub["date"], y=sub["return"], mode="markers", name=label,
                    marker=dict(symbol=symbol, color=color, size=11, line=dict(color="white", width=2)),
                    customdata=np.stack([sub["가격"].to_numpy(), sub["사유"].to_numpy()], axis=-1),
                    hovertemplate=(
                        "%{x|%Y-%m-%d}<br>구분: " + label + "<br>" + _price_tooltip_title
                        + ": %{customdata[0]:" + _price_tooltip_format
                        + "}<br>당시 누적수익률: %{y:.1%}<br>사유: %{customdata[1]}<extra></extra>"
                    ),
                )
            )

fig.update_xaxes(
    tickformat="%Y",
    dtick="M12",
    rangeslider=dict(visible=True),
    rangeselector=dict(
        buttons=list(
            [
                dict(count=1, label="1년", step="year", stepmode="backward"),
                dict(count=3, label="3년", step="year", stepmode="backward"),
                dict(count=5, label="5년", step="year", stepmode="backward"),
                dict(step="all", label="전체"),
            ]
        )
    ),
)
fig.update_yaxes(title="누적수익률", tickformat=".0%")
fig.update_layout(
    height=460,
    margin=dict(t=60, b=10),
    hovermode="closest",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
)
st.plotly_chart(fig, use_container_width=True)
st.caption(
    "▲ 파란색 = 매수 시점, ▼ 빨간색 = 매도 시점 (거래 내역 표 참고) · "
    f"{STRAT_HYBRID_LABEL}의 점선 구간 = 미보유(현금) 기간에 기대수익률을 가정 적용한 부분 · "
    "하단 슬라이더로 구간을 드래그해 확대, 상단 버튼으로 빠른 기간 이동 가능."
)

# ---- 3. 연도별 연환산수익률 막대그래프 ----
st.subheader("연도별 연환산수익률")
STRAT_HELD_LABEL = f"{STRATEGY_LABEL}(보유기간만)"
yearly_mode = st.radio(
    "신호전략 계산 방식",
    options=[STRAT_HYBRID_LABEL, STRAT_HELD_LABEL],
    horizontal=True,
    key="bt_yearly_chart_mode",
    help=f"**{STRAT_HYBRID_LABEL}**(기본): 미보유(현금) 기간에도 위에서 설정한 기대수익률"
    f"(연 {bond_yield_pct:g}%)을 적용해 연환산수익률을 계산합니다 — 요약 지표 ④ 그룹과 "
    f"동일한 방식입니다. **{STRAT_HELD_LABEL}**: 미보유 기간은 0%로 취급하고 실제 보유 "
    "기간의 등락만 반영합니다 — 요약 지표 ③ 그룹과 동일한 방식입니다.",
)
yearly_display = yearly if yearly_mode == STRAT_HYBRID_LABEL else backtest.yearly_returns(equity, bh_equity)
strategy_series_label = yearly_mode
strategy_color = STRATEGY_HYBRID_COLOR if yearly_mode == STRAT_HYBRID_LABEL else STRATEGY_COLOR

yearly_long = yearly_display.melt(
    id_vars=["year", "days_span"],
    value_vars=["strategy_return_annualized", "bh_return_annualized"],
    var_name="series", value_name="return",
)
yearly_long["series"] = yearly_long["series"].map(
    {"strategy_return_annualized": strategy_series_label, "bh_return_annualized": BH_LABEL}
)
raw_map = {}
for _, row in yearly_display.iterrows():
    raw_map[(row["year"], strategy_series_label)] = row["strategy_return"]
    raw_map[(row["year"], BH_LABEL)] = row["bh_return"]
yearly_long["raw_return"] = [raw_map[(y, s)] for y, s in zip(yearly_long["year"], yearly_long["series"])]

bar_fig = go.Figure()
for label, color in ((strategy_series_label, strategy_color), (BH_LABEL, BH_COLOR)):
    sub = yearly_long[yearly_long["series"] == label]
    bar_fig.add_trace(
        go.Bar(
            x=sub["year"].astype(str), y=sub["return"], name=label, marker_color=color,
            customdata=np.stack([sub["raw_return"].to_numpy(), sub["days_span"].to_numpy()], axis=-1),
            hovertemplate=(
                "연도: %{x}<br>전략: " + label + "<br>연환산수익률: %{y:.1%}<br>해당 연도 실제 수익률: "
                "%{customdata[0]:.1%}<br>해당 연도 일수: %{customdata[1]:d}<extra></extra>"
            ),
        )
    )
bar_fig.update_layout(
    height=340, barmode="group",
    yaxis=dict(title="연환산수익률", tickformat=".0%"),
    xaxis=dict(title=None),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    margin=dict(t=40, b=10),
)
st.plotly_chart(bar_fig, use_container_width=True)
yearly_cash_note = (
    f"미보유(현금) 기간에는 기대수익률(연 {bond_yield_pct:g}%)이 적용됩니다."
    if yearly_mode == STRAT_HYBRID_LABEL
    else "신호전략이 그 해 내내 현금(미보유) 상태였다면 0%로 표시됩니다."
)
st.caption(
    f"{int(yearly_display['year'].iloc[0])}년과 {int(yearly_display['year'].iloc[-1])}년은 분석 기간에 걸친 "
    "부분연도이며, 그 부분 기간의 실제 수익률을 연 단위로 환산한 값입니다(마우스오버 시 실제 "
    f"수익률 확인 가능). {yearly_cash_note}"
)

# ---- 4. 거래 내역 표 ----
st.subheader("거래 내역")
if trades:
    trade_rows = [
        {
            "매수일": t["entry_date"].date(),
            "매수가": _format_copper_price(t["entry_price"]),
            "매수 사유": t["entry_reason"] or "-",
            "매도일": t["exit_date"].date() if t["exit_date"] is not None else "미청산(보유 중)",
            "매도가": _format_copper_price(t["exit_price"]),
            "매도 사유": t["exit_reason"] or ("미청산" if t["open"] else "-"),
            "보유일수": t["hold_days"],
            "구간수익률(순, 수수료 반영)": f"{t['net_period_return']:.2%}" + (" (평가)" if t["open"] else ""),
            "구간수익률(총, 수수료 미반영)": f"{t['gross_period_return']:.2%}" + (" (평가)" if t["open"] else ""),
        }
        for t in trades
    ]
    st.dataframe(pd.DataFrame(trade_rows), use_container_width=True, hide_index=True)
    st.caption(
        "매수 사유/매도 사유는 신호가 발생한 날 기준이며, 모든 조건이 체결일 = 신호 발생일"
        "(지연 없음)입니다. 구간수익률(순)은 이 거래의 매수/매도/보관 수수료를 모두 반영한 "
        "실제 손익 기준이며, 위 승률도 이 기준으로 계산됩니다 — 구간수익률(총)은 수수료를 "
        "제외한 순수 가격 변동률입니다(미청산 거래는 매도수수료를 아직 반영하지 않은 값)."
    )
else:
    st.caption("이 기간 동안 매수 신호가 발생하지 않아 거래 내역이 없습니다.")

st.caption(
    "⚠️ 본 백테스트는 과거 데이터에 기반한 시뮬레이션 결과이며 미래 성과를 보장하지 않습니다. "
    "② KODEX 구리선물(H) 기준일 때는 매수·매도 수수료와 총보수가 반영되지만, 세금·슬리피지는 "
    "여전히 반영되어 있지 않고, 표본 기간이 짧아 과최적화(overfitting) 위험이 있습니다. "
    "'④ 신호전략 (미보유기간 기대수익률 포함)' 그룹의 기대수익률은 사용자가 입력한 "
    "단일 연이율을 그대로 연복리 적용한 단순 가정치이며, 실제 채권 등 투자자산의 이자율 변동· "
    "재투자·신용위험은 반영되어 있지 않습니다."
)
