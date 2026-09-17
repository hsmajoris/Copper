"""Streamlit dashboard: copper price correlation table.

For today's date, reads the pre-computed data/latest.json (refreshed daily at
07:00 KST by the GitHub Actions workflow in
.github/workflows/update_dashboard_data.yml) so the page loads instantly.
For any other selected date, computes the table live as of that date
(requires network access; no API key needed — both indicators are Yahoo
Finance tickers, unlike Gold's real_rate which needs FRED_API_KEY).
"""

import json
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from copper_dashboard import config, signals, timeseries
from copper_dashboard.timeutil import today_kst

DATA_PATH = Path(__file__).resolve().parent / "data" / "latest.json"
EARLIEST_DATE = date(2000, 1, 1)

# dataviz reference palette: the indicator gets a sequential blue ramp (darkest =
# its own daily close, progressively lighter for the config.MA_WINDOWS SMAs
# so shorter windows read closer to the raw series), copper price gets
# categorical slot 2 (bronze/copper tone) so it never reads as "one more shade
# of the same family" on its own (right-hand) axis.
CHART_INDICATOR_COLOR = "#256abf"
CHART_SMA_COLORS = {7: "#5598e7", 20: "#86b6ef", 60: "#b7d3f6"}
CHART_COPPER_COLOR = "#b5651d"
CHART_SIGNAL_SHADE_COLOR = "#e34948"
CHART_SIGNAL_SHADE_OPACITY = 0.16
CHART_YEARS = 10


def _boolean_series_to_ranges(flag: pd.Series) -> list[tuple]:
    """Contiguous [start, end] date ranges where `flag` is True (inclusive of
    both ends)."""
    if flag.empty:
        return []
    idx = flag.index
    values = flag.to_numpy()
    ranges = []
    start = None
    for i, is_true in enumerate(values):
        if is_true and start is None:
            start = idx[i]
        elif not is_true and start is not None:
            ranges.append((start, idx[i - 1]))
            start = None
    if start is not None:
        ranges.append((start, idx[-1]))
    return ranges


@st.cache_data(ttl=3600, show_spinner="데이터를 불러오는 중입니다...")
def load_data(selected_date_iso: str, is_today: bool):
    if is_today and DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))

    from copper_dashboard.build_table import build

    as_of = None if is_today else date.fromisoformat(selected_date_iso)
    return build(as_of=as_of)


@st.cache_data(ttl=86400, show_spinner=f"{CHART_YEARS}년치 시계열 데이터를 불러오는 중입니다...")
def load_chart_data(indicator_key: str, as_of_iso: str) -> dict:
    return timeseries.build_indicator_chart_data(
        indicator_key, as_of=date.fromisoformat(as_of_iso), years=CHART_YEARS
    )


def render_indicator_chart(indicator_key: str, label: str, as_of_iso: str) -> None:
    try:
        chart_data = load_chart_data(indicator_key, as_of_iso)
    except Exception as exc:
        st.error(f"{label} 시계열을 불러오지 못했습니다: {exc}")
        return

    meta = config.INDICATOR_META[indicator_key]
    unit_suffix = f" ({meta['unit']})" if meta["unit"] else ""
    indicator_series_name = f"{label} 종가{unit_suffix}"
    copper_series_name = "구리(HG=F) 가격 ($)"

    left_rows = [
        {"date": d, "value": v, "series": indicator_series_name}
        for d, v in chart_data["indicator"].items()
    ]
    ma_windows = sorted(config.MA_WINDOWS)
    window_labels = {window: f"{window}일 이동평균" for window in ma_windows}
    for window in ma_windows:
        sma_series = chart_data["smas"][window]
        left_rows.extend(
            {"date": d, "value": v, "series": window_labels[window]}
            for d, v in sma_series.items()
        )
    left_domain = [indicator_series_name] + [window_labels[window] for window in ma_windows]
    left_range = [CHART_INDICATOR_COLOR] + [CHART_SMA_COLORS[window] for window in ma_windows]

    combined_domain = left_domain + [copper_series_name]
    combined_range = left_range + [CHART_COPPER_COLOR]
    color_scale = alt.Scale(domain=combined_domain, range=combined_range)

    left_df = pd.DataFrame(left_rows)
    left_chart = (
        alt.Chart(left_df)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format="%Y", tickCount="year")),
            y=alt.Y(
                "value:Q",
                title=indicator_series_name,
                axis=alt.Axis(titleColor=CHART_INDICATOR_COLOR),
            ),
            color=alt.Color("series:N", scale=color_scale, legend=alt.Legend(title=None)),
            strokeDash=alt.StrokeDash(
                "series:N",
                scale=alt.Scale(domain=left_domain, range=[[]] + [[4, 2]] * (len(left_domain) - 1)),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("date:T", title="날짜"),
                alt.Tooltip("series:N", title="시리즈"),
                alt.Tooltip("value:Q", title="값", format=".2f"),
            ],
        )
    )

    # Buy-signal-active shading: both dxy and fxi feed green_count (unlike
    # Gold, this project has no reference-only indicator), so both always
    # get shading here.
    signal_flag = (
        signals.all_windows_copper_friendly_for(
            indicator_key, chart_data["indicator"], chart_data["smas"]
        )
        if indicator_key in config.GREEN_COUNT_SIGNAL_INDICATORS
        else None
    )

    shade_ranges = _boolean_series_to_ranges(signal_flag) if signal_flag is not None else []
    layers = []
    if shade_ranges:
        shade_df = pd.DataFrame(
            {
                "start": [r[0] for r in shade_ranges],
                "end": [r[1] + timedelta(days=1) for r in shade_ranges],
            }
        )
        layers.append(
            alt.Chart(shade_df)
            .mark_rect(color=CHART_SIGNAL_SHADE_COLOR, opacity=CHART_SIGNAL_SHADE_OPACITY)
            .encode(x="start:T", x2="end:T")
        )

    layers.append(left_chart)

    copper_df = pd.DataFrame(
        {"date": d, "value": v, "series": copper_series_name} for d, v in chart_data["copper"].items()
    )
    copper_chart = (
        alt.Chart(copper_df)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format="%Y", tickCount="year")),
            y=alt.Y(
                "value:Q",
                title=copper_series_name,
                axis=alt.Axis(orient="right", titleColor=CHART_COPPER_COLOR),
            ),
            color=alt.Color("series:N", scale=color_scale, legend=alt.Legend(title=None)),
            tooltip=[
                alt.Tooltip("date:T", title="날짜"),
                alt.Tooltip("value:Q", title="구리 가격", format="$.2f"),
            ],
        )
    )

    combined_chart = (
        alt.layer(alt.layer(*layers), copper_chart)
        .resolve_scale(y="independent")
        .properties(height=650, title=f"{label} vs 구리 가격 — 최근 {CHART_YEARS}년")
    )
    st.altair_chart(combined_chart, use_container_width=True)

    windows_arrow = "→".join(str(w) for w in ma_windows)
    windows_dot = "·".join(str(w) for w in ma_windows)
    st.caption(
        f"🔵 진한 파랑 = {label} 종가, 옅어질수록 {windows_arrow}일 이동평균(왼쪽 축) · "
        "🟤 구리 가격(오른쪽 축, $)"
    )
    st.caption(
        f"🟥 음영 구간 = 해당 지표 기준 매수신호 활성 구간 ({windows_dot}일 이평선 {len(ma_windows)}개 모두 "
        "동시에 만족하는 날). 실제 매매 신호의 green_count는 이 조건을 달러인덱스·FXI 두 지표에서 "
        f"합산하므로, 이 지표 하나만으로 {len(ma_windows)}개를 모두 만족하지 못해도 다른 지표 쪽에서 "
        "채워져 매수가 발생할 수 있습니다."
    )


# 구리 vs DXY / 구리 vs FXI 상관계수 — 2005-01~2026-09, HG=F/DXY/FXI 일간수익률
# 기준, 2년 단위 11개 구간. COPPER_TRADING_LOGIC.md 11장 참고. 원자료
# (copper_chii_dxy_daily.csv)에서 직접 계산한 부호 있는 상관계수(r)를 제곱해
# R²(0~1)로 표시 — 부호(방향)는 표 대신 본문·캡션에 별도 텍스트로 명시한다
# (DXY는 전 구간 역상관, FXI는 전 구간 정상관으로 한 번도 바뀌지 않았으므로
# 구간마다 반복 표기할 필요가 없음).
_KEY_TAKEAWAYS_PERIODS = [
    ("2005~2006", 0.0219, 0.0515),
    ("2007~2008", 0.1436, 0.0350),
    ("2009~2010", 0.1310, 0.2938),
    ("2011~2012", 0.2304, 0.3552),
    ("2013~2014", 0.0237, 0.0801),
    ("2015~2016", 0.0210, 0.0955),
    ("2017~2018", 0.0484, 0.1129),
    ("2019~2020", 0.0586, 0.1884),
    ("2021~2022", 0.1722, 0.0692),
    ("2023~2024", 0.1505, 0.1772),
    ("2025~2026", 0.0506, 0.1347),
]


def _render_key_takeaways() -> None:
    """"핵심 요약" callout pinned above the correlation table. 금 프로젝트와 달리
    국면별 참여율 문구는 없음 — regime.py를 이번 1차 범위에서 제외했기 때문(요청에
    따름). 대신 11개 구간 상관계수 검증(11장) 결과를 고정 텍스트로 보여준다."""
    st.markdown(
        """
<div style="background-color:#fff3e0;border-left:6px solid #b5651d;
border-radius:8px;padding:16px 20px;margin-bottom:4px">
<div style="font-size:17px;font-weight:600;margin-bottom:8px">💡 핵심 요약</div>
<p style="margin:0 0 4px 0;font-weight:600">[분석 개요]</p>
<p style="margin:0 0 10px 0;line-height:1.6">
구리는 금과 달리 안전자산이 아닌 경기민감 산업금속으로, 실질금리(무이자자산 보유의 기회비용)
같은 금 특유의 통화적 요인은 설명력이 없다고 판단해 이번 모델에서 제외했다. 대신 달러인덱스
(원자재 표시통화 부담)와 FXI(세계 최대 구리 소비국인 중국의 산업활동에 대한 시장의 실시간
평가를 담은 가격 기반 대리지표)를 채택했다. PMI(중국 제조업 지수)도 별도로 검증했다 — 이번 달
발표치가 1개월 전·3개월 전 대비 개선되는지를 신호로 삼아 20년간 확인한 결과, 전체기간 평균으로는
유의미해 보였으나 5년 단위로 구간을 쪼개보니 2005~2009년 한 시기에서만 유의미했고 나머지 네
구간(2010~2026)은 유의성이 없었다. 매수 신호와 매도 신호를 직접 비교해도 방향을 구분해주지
못했다. 반면 FXI는 1년 단위 22개 구간 중 21개에서 견고하게 유의미했다 — 실물 통계인 PMI보다,
시장가격 기반이지만 매일 갱신되고 선반영 효과가 있는 FXI가 더 신뢰할 수 있는 지표로 확인되어
FXI를 최종 채택했다.
</p>
<p style="margin:0 0 4px 0;font-weight:600">[분석 결과]</p>
<p style="margin:0 0 10px 0;line-height:1.6">
2005년 이후 11개 구간(2년 단위)으로 나눠 검토한 결과, 구리-달러인덱스는 전 구간에서 부호가 한
번도 바뀌지 않고 R²(설명력) 0.021~0.230 사이에서 일관되게 역상관, 구리-FXI 역시 전 구간에서
부호가 바뀌지 않고 R² 0.035~0.355 사이에서 일관되게 정상관이었다. 두 지표 상호간 상관관계는
R²=0.032로 약해 서로 대체 관계가 아닌 독립적인 정보를 담고 있다고 판단했다(OR 결합 채택 근거).
</p>
<div style="background-color:#fdecea;border-left:4px solid #d32f2f;
border-radius:6px;padding:10px 14px;line-height:1.6">
<span style="font-weight:600">[유의사항]</span><br>
리드-래그 분석 결과 두 지표 모두 상관관계가 거의 전부 lag=0(당일)에 몰려 있어, 이 지표들은
"선행지표"가 아니라 "당일 동시 신호"로 해석해야 한다 — 다만 이 전략은 애초에 신호 당일 종가에
즉시 체결하는 구조라 이 한계가 설계 자체와 모순되지는 않는다. FXI는 공식 통계(PMI 등)보다
갱신이 빠른 대신, 중국 경기 자체가 아니라 "시장이 평가한" 값이라는 한 단계 간접적인 대리지표라는
한계가 있다(COPPER_TRADING_LOGIC.md 10장 참고). 아래 표의 R²가 "유의미한가"는 고정된 숫자
하나로 판단할 수 없고 표본 크기(구간 길이)에 따라 문턱 자체가 달라진다 — t-검정 기준 대략
R² ≈ 3.84/(n-2)로, 일간 관측치가 약 250개(1년)면 R²≈0.016, 약 500개(2년)면 R²≈0.008 정도가
p<0.05 유의성 문턱이다. 아래 11개 구간은 최솟값(DXY 0.021, FXI 0.035)조차 이 문턱을 넉넉히
넘는다.
</div>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.expander("📊 구간별 상관성 근거 보기", expanded=False):

        def header_cell(text: str) -> str:
            return (
                f'<th style="padding:8px 12px;border:1px solid #ddd;background:#f5f5f5;'
                f'text-align:left;white-space:nowrap">{text}</th>'
            )

        def r_cell(value: float) -> str:
            text = f"{value:.3f}"
            if value >= 0.16:
                text = f"<strong>{text}</strong>"
            return f'<td style="padding:8px 12px;border:1px solid #ddd">{text}</td>'

        header_row = header_cell("구간") + header_cell("구리-달러인덱스 R²") + header_cell("구리-FXI R²")
        rows_html = [f"<tr>{header_row}</tr>"]
        for period, dxy_r, fxi_r in _KEY_TAKEAWAYS_PERIODS:
            rows_html.append(f"<tr>{header_cell(period)}{r_cell(dxy_r)}{r_cell(fxi_r)}</tr>")
        table_html = (
            '<table style="border-collapse:collapse;width:100%;font-size:14px">' + "".join(rows_html) + "</table>"
        )
        st.markdown(table_html, unsafe_allow_html=True)
        st.caption(
            "2년 단위 기준(2005-01~2026-09) · 원자료: yfinance HG=F/DX-Y.NYB/FXI 일간종가 · "
            "일간수익률(pct_change) 기준 피어슨 상관계수(r)를 제곱한 R²(설명력, 0~1) · "
            "방향(역상관/정상관)은 전 구간 고정이라 본문에 별도 서술 · lag=-5..+5 리드-래그 분석에서 "
            "두 지표 모두 |r| 최댓값이 lag=0에서 나타남을 확인."
        )


def render_dashboard() -> None:
    st.title("구리(Copper) 상관관계 대시보드")
    _render_key_takeaways()

    # Shared with the 유효성 검증 (backtest) page via config.COPPER_PRICE_BASIS_STATE_KEY
    # — but NOT via that key's own widget binding: st.navigation resets a
    # widget's session_state entry back to its default the moment that exact
    # widget isn't instantiated in a run, so a `key=` shared across two
    # different pages' widgets does NOT survive navigation between them. The
    # fix is to keep the shared choice in that plain session_state entry
    # (which does survive navigation) and seed each page's own, page-local
    # widget from it via `index=`, writing the widget's result straight back
    # after every rerun.
    _copper_basis_options = [config.COPPER_PRICE_BASIS_INTL, config.COPPER_PRICE_BASIS_KRX]
    st.session_state.setdefault(config.COPPER_PRICE_BASIS_STATE_KEY, config.COPPER_PRICE_BASIS_DEFAULT)
    # The backtest page's own 3rd radio option (COPX) is page-local and never
    # written into this shared key (see that page's module docstring) — this
    # fallback just protects against a value this page doesn't recognize ever
    # landing here regardless.
    _shared_basis = st.session_state[config.COPPER_PRICE_BASIS_STATE_KEY]
    _basis_index = (
        _copper_basis_options.index(_shared_basis)
        if _shared_basis in _copper_basis_options
        else _copper_basis_options.index(config.COPPER_PRICE_BASIS_DEFAULT)
    )
    copper_price_basis = st.radio(
        "구리 가격 기준",
        options=_copper_basis_options,
        format_func=lambda v: config.COPPER_PRICE_BASIS_LABELS[v],
        index=_basis_index,
        key="_copper_price_basis_widget_dashboard",
        horizontal=True,
        help="유효성 검증(백테스트) 페이지 전체가 이 기준으로 계산됩니다. 이 대시보드 페이지의 "
        "표·그래프 자체는 이 설정과 무관하게 항상 국제 시세 기준입니다.",
    )
    st.session_state[config.COPPER_PRICE_BASIS_STATE_KEY] = copper_price_basis
    if copper_price_basis == config.COPPER_PRICE_BASIS_KRX:
        st.caption(
            "ℹ️ 아래 표의 달러인덱스·FXI는 국제 시세 기준 참고 지표이며 KODEX 구리선물(H)과 직접 "
            "대응되지 않습니다. 이 설정은 유효성 검증 페이지의 백테스트에만 적용됩니다."
        )

    today = today_kst()
    date_col, refresh_col = st.columns([4, 1])
    with date_col:
        selected_date = st.date_input(
            "기준일 선택",
            value=today,
            min_value=EARLIEST_DATE,
            max_value=today,
            help="이 날짜(또는 그 이전 최근 거래일)의 종가를 기준으로 표를 계산합니다.",
        )
    with refresh_col:
        st.write("")
        st.write("")
        force_live = st.button("새로고침", use_container_width=True)

    is_today = selected_date == today
    if force_live:
        st.cache_data.clear()

    try:
        data = load_data(selected_date.isoformat(), is_today)
    except Exception as exc:
        st.error(f"데이터를 불러오지 못했습니다: {exc}")
        st.stop()

    close_row_label = "전일종가" if is_today else "종가"

    if is_today:
        st.caption(
            f"기준일(전일 미국장 마감 종가): **{data['as_of']}**  ·  생성시각(KST): {data['generated_at']}"
        )
        st.caption(
            "⚠️ 실시간 시세가 아닙니다. 이 표는 매일 아침 7시(KST)에 자동 갱신됩니다."
        )
    else:
        st.caption(
            f"선택한 기준일: **{selected_date}** → 실제 반영된 거래일: **{data['as_of']}** "
            "(주말·휴장일이면 직전 거래일 종가가 표시됩니다)"
        )
        st.caption("ℹ️ 과거 기준일은 매일 자동 갱신되는 캐시가 아니라 그때그때 실시간으로 계산됩니다.")
    st.caption(
        "🟢 옅은 녹색 배경 = 그 신호가 현재 구리값에 우호적인 방향인 셀입니다. 역방향 지표"
        "(달러인덱스)는 종가가 이평선 아래일 때, 정방향 지표(FXI)는 종가가 이평선 위일 때 "
        "초록색으로 표시됩니다(green_count에 사용). 셀에 보이는 '상향 돌파/이평선 아래' 문구는 "
        "하이라이트 색과 무관한, 종가와 이평선의 기술적 위치입니다. 각 셀 하단의 작은 글씨는 그 "
        "상향 돌파가 며칠째 지속 중인지를 나타내는 보조 정보입니다."
    )

    indicator_order = data["indicator_order"]
    indicators = data["indicators"]

    def header_cell(text: str, tooltip: str | None = None) -> str:
        title_attr = f' title="{tooltip}"' if tooltip else ""
        return (
            f'<th style="padding:8px 12px;border:1px solid #ddd;background:#f5f5f5;'
            f'text-align:left;white-space:nowrap"{title_attr}>{text}</th>'
        )

    def data_cell(text: str, highlight: bool = False) -> str:
        bg = "background-color: rgba(76,175,80,0.28);" if highlight else ""
        return f'<td style="padding:8px 12px;border:1px solid #ddd;{bg}">{text}</td>'

    def ma_cell(sma: dict) -> str:
        bg = "background-color: rgba(76,175,80,0.28);" if sma["copper_friendly"] else ""
        badge = (
            f'<div style="font-size:11px;color:#5a5a5a;margin-top:2px">{sma["streak_display"]}</div>'
            if sma["streak_display"]
            else ""
        )
        return (
            f'<td style="padding:8px 12px;border:1px solid #ddd;{bg}">'
            f'{sma["display"]}{badge}</td>'
        )

    rows_html = []

    header_row = header_cell("구성") + "".join(
        header_cell(indicators[k]["label"], tooltip=indicators[k]["source"]) for k in indicator_order
    )
    rows_html.append(f"<tr>{header_row}</tr>")

    for row_name in data["row_order"]:
        cells = "".join(data_cell(data["static_rows"][row_name][k]) for k in indicator_order)
        rows_html.append(f"<tr>{header_cell(row_name)}{cells}</tr>")

    ma_windows = data["ma_windows"]
    for window in ma_windows:
        cells = "".join(ma_cell(indicators[k]["sma"][str(window)]) for k in indicator_order)
        rows_html.append(f"<tr>{header_cell(f'{window}일선')}{cells}</tr>")

    close_cells = "".join(data_cell(indicators[k]["prev_close"]["display"]) for k in indicator_order)
    rows_html.append(f"<tr>{header_cell(close_row_label)}{close_cells}</tr>")

    table_html = (
        '<table style="border-collapse:collapse;width:100%;font-size:14px">'
        + "".join(rows_html)
        + "</table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)

    st.markdown("#### 지표별 시계열 그래프")
    st.caption(
        f"버튼을 누른 지표만 그 시점에 최근 {CHART_YEARS}년치 데이터를 받아와 그립니다 — 누르기 "
        "전에는 어떤 지표도 미리 계산하지 않습니다."
    )

    chart_cols = st.columns(len(indicator_order))
    for col, key in zip(chart_cols, indicator_order):
        state_key = f"show_chart_{key}"
        st.session_state.setdefault(state_key, False)
        with col:
            button_label = (
                f"📉 {indicators[key]['label']} 그래프 숨기기"
                if st.session_state[state_key]
                else f"📈 {indicators[key]['label']} 그래프 보기"
            )
            if st.button(button_label, key=f"btn_{state_key}", use_container_width=True):
                st.session_state[state_key] = not st.session_state[state_key]
                st.rerun()

    for key in indicator_order:
        if st.session_state.get(f"show_chart_{key}", False):
            render_indicator_chart(key, indicators[key]["label"], today.isoformat())

    st.markdown("#### 지표별 참고 출처")
    for k in indicator_order:
        st.caption(f"**{indicators[k]['label']}** — {data['footnotes'][k]}")


def main() -> None:
    st.set_page_config(page_title="구리(Copper) 상관관계 대시보드", layout="wide")

    pages = st.navigation(
        [
            st.Page(render_dashboard, title="대시보드", url_path="", default=True),
            st.Page("pages/1_백테스트.py", title="유효성 검증", url_path="백테스트"),
        ]
    )
    pages.run()


main()
