"""Streamlit dashboard: copper price correlation table + weighted
copper_friendly score.

For today's date, reads the pre-computed data/latest.json (refreshed daily
at 07:00 KST by the GitHub Actions workflow in
.github/workflows/update_dashboard_data.yml) for the daily-frequency
indicators (DXY/WTI/gold-copper ratio), so the page loads instantly. PMI and
COMEX are read live from their own local stores on every load (they're
button-refreshed independently, not part of the daily batch job — see
copper_dashboard/pmi_store.py / comex_store.py).
"""

import json
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from copper_dashboard import build_table, comex_store, config, data_sources, pmi_store, signals, timeseries
from copper_dashboard.timeutil import today_kst

DATA_PATH = Path(__file__).resolve().parent / "data" / "latest.json"
EARLIEST_DATE = date(2000, 1, 1)

# dataviz reference palette: the indicator gets a sequential blue ramp (darkest =
# its own daily close, progressively lighter for the 5/30/60-day SMAs), copper
# price gets a distinct copper/bronze tone on its own (right-hand) axis so it
# never reads as "one more shade of the indicator family", and reference
# threshold lines use a neutral gray.
CHART_INDICATOR_COLOR = "#256abf"
CHART_SMA_COLORS = {5: "#5598e7", 30: "#86b6ef", 60: "#b7d3f6"}
CHART_COPPER_COLOR = "#b5651d"
CHART_THRESHOLD_COLOR = "#8a8a86"
CHART_SIGNAL_SHADE_COLOR = "#e34948"
CHART_SIGNAL_SHADE_OPACITY = 0.16
# Fixed, not user-configurable here — deliberately independent of the 유효성
# 검증 (backtest) page's own adjustable analysis period.
CHART_YEARS = 10


def _boolean_series_to_ranges(flag: pd.Series) -> list[tuple]:
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
def load_daily_data(selected_date_iso: str, is_today: bool):
    if is_today and DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))

    as_of = None if is_today else date.fromisoformat(selected_date_iso)
    return build_table.build(as_of=as_of)


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
    if chart_data["kind"] == "ma":
        window_labels = {5: "5일 이동평균", 30: "30일 이동평균", 60: "60일 이동평균"}
        for window in (5, 30, 60):
            sma_series = chart_data["smas"][window]
            left_rows.extend(
                {"date": d, "value": v, "series": window_labels[window]}
                for d, v in sma_series.items()
            )
        left_domain = [indicator_series_name, "5일 이동평균", "30일 이동평균", "60일 이동평균"]
        left_range = [CHART_INDICATOR_COLOR, CHART_SMA_COLORS[5], CHART_SMA_COLORS[30], CHART_SMA_COLORS[60]]
    else:
        left_domain = [indicator_series_name]
        left_range = [CHART_INDICATOR_COLOR]

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

    if chart_data["kind"] == "ma":
        signal_flag = signals.all_windows_copper_friendly_for(
            indicator_key, chart_data["indicator"], chart_data["smas"]
        )
    else:
        buy_flag = signals.ratio_threshold_active(
            chart_data["indicator"], config.DEFAULT_GC_RATIO_BUY_THRESHOLD, "le"
        )
        signal_flag = buy_flag

    shade_ranges = _boolean_series_to_ranges(signal_flag)
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
    if chart_data["kind"] == "ratio":
        threshold_df = pd.DataFrame(
            {
                "y": [config.DEFAULT_GC_RATIO_BUY_THRESHOLD, config.DEFAULT_GC_RATIO_SELL_THRESHOLD],
                "label": [
                    f"매수 우호적 임계값 {config.DEFAULT_GC_RATIO_BUY_THRESHOLD:g}",
                    f"매수 비우호적 임계값 {config.DEFAULT_GC_RATIO_SELL_THRESHOLD:g}",
                ],
            }
        )
        layers.append(
            alt.Chart(threshold_df)
            .mark_rule(strokeDash=[4, 4], strokeWidth=1.5, color=CHART_THRESHOLD_COLOR)
            .encode(y="y:Q", tooltip=[alt.Tooltip("label:N", title="기준선")])
        )

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

    if chart_data["kind"] == "ratio":
        st.caption(
            f"🔵 {label}(왼쪽 축) · 🟤 구리 가격(오른쪽 축, $) · 회색 점선 = 매수 우호적/비우호적 임계값 "
            f"({config.DEFAULT_GC_RATIO_BUY_THRESHOLD:g} / {config.DEFAULT_GC_RATIO_SELL_THRESHOLD:g})"
        )
        st.caption(
            f"🟥 음영 구간 = 비율 ≤ {config.DEFAULT_GC_RATIO_BUY_THRESHOLD:g} (구리에 우호적인 국면)"
        )
    else:
        st.caption(
            f"🔵 진한 파랑 = {label} 종가, 옅어질수록 5→30→60일 이동평균(왼쪽 축) · "
            "🟤 구리 가격(오른쪽 축, $)"
        )
        st.caption(
            "🟥 음영 구간 = 5·30·60일 이평선 3개 모두 동시에 구리에 우호적인 방향을 가리키는 날"
        )


def render_score(score_info: dict) -> None:
    score = score_info["score"]
    st.subheader("종합 점수 (copper_friendly)")
    if score is None:
        st.warning("모든 지표가 제외되어 점수를 계산할 수 없습니다.")
        return

    if score >= config.SCORE_BUY_FRIENDLY_CUTOFF:
        badge, color = "매수 우호적", "🟢"
    elif score <= config.SCORE_SELL_UNFRIENDLY_CUTOFF:
        badge, color = "매수 비우호적", "🔴"
    else:
        badge, color = "중립", "🟡"

    col1, col2 = st.columns([1, 2])
    with col1:
        st.metric("종합 점수 (0~100)", f"{score:.1f}", delta=f"{color} {badge}", delta_color="off")
    with col2:
        st.progress(min(max(score / 100.0, 0.0), 1.0))
        st.caption(
            f"기준: {config.SCORE_BUY_FRIENDLY_CUTOFF:g} 이상 매수 우호적 · "
            f"{config.SCORE_SELL_UNFRIENDLY_CUTOFF:g} 이하 매수 비우호적 (초기 설정값 — "
            "유효성 검증 페이지에서 과거 분포를 보고 조정 가능)"
        )
    if score_info["excluded"]:
        excluded_labels = ", ".join(
            config.INDICATOR_META.get(k, {}).get("label", k) for k in score_info["excluded"]
        )
        st.caption(f"⚠️ 데이터 오래됨/누락으로 점수 계산에서 제외된 지표: {excluded_labels}")


def render_daily_table(data: dict) -> None:
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
        return f'<td style="padding:8px 12px;border:1px solid #ddd;{bg}">{sma["display"]}{badge}</td>'

    rows_html = []
    weight_row = "".join(
        header_cell(f"가중치 {indicators[k]['weight']:g}") for k in indicator_order
    )
    rows_html.append(f"<tr>{header_cell('구성')}{weight_row}</tr>")

    header_row = "".join(
        header_cell(indicators[k]["label"], tooltip=indicators[k]["source"]) for k in indicator_order
    )
    rows_html.append(f"<tr>{header_cell('지표')}{header_row}</tr>")

    for row_name in data["row_order"]:
        cells = "".join(data_cell(data["static_rows"][row_name][k]) for k in indicator_order)
        rows_html.append(f"<tr>{header_cell(row_name)}{cells}</tr>")

    for window in data["ma_windows"]:
        cells = "".join(ma_cell(indicators[k]["sma"][str(window)]) for k in indicator_order)
        rows_html.append(f"<tr>{header_cell(f'{window}일선')}{cells}</tr>")

    close_cells = "".join(data_cell(indicators[k]["prev_close"]["display"]) for k in indicator_order)
    rows_html.append(f"<tr>{header_cell('전일종가')}{close_cells}</tr>")

    table_html = (
        '<table style="border-collapse:collapse;width:100%;font-size:14px">'
        + "".join(rows_html)
        + "</table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _pmi_refresh_and_save(indicator_key: str, label: str) -> None:
    scrape_fn = data_sources.scrape_china_pmi if indicator_key == "china_pmi" else data_sources.scrape_us_pmi
    period_key = f"pmi_period_{indicator_key}"
    value_key = f"pmi_value_{indicator_key}"
    scraped_key = f"pmi_scraped_{indicator_key}"

    today = today_kst()
    default_period = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    st.session_state.setdefault(period_key, default_period)
    st.session_state.setdefault(value_key, 50.0)
    st.session_state.setdefault(scraped_key, False)

    if st.button(f"🔄 {label} 새로고침 (월초)", key=f"btn_refresh_{indicator_key}"):
        result = scrape_fn()
        if result is not None:
            # Set BEFORE the number_input widget below is instantiated this
            # run, so it picks the new value up via its `key=` binding — do
            # NOT also pass `value=` to the widget (Streamlit warns that
            # combination is a conflicting anti-pattern), and never write
            # None here: a failed scrape should leave whatever value was
            # already in the box, not blank the widget out entirely.
            st.session_state[value_key] = result["value"]
            st.session_state[scraped_key] = True
            st.success(f"자동 스크래핑 성공: {result['value']:.1f} — 아래에서 확인 후 저장하세요.")
        else:
            st.session_state[scraped_key] = False
            st.info("자동 스크래핑에 실패했습니다. 값을 직접 입력한 뒤 저장해 주세요.")

    period = st.text_input(
        "해당 월 (YYYY-MM)", key=period_key, help="이 PMI 수치가 어느 달의 값인지 입력하세요."
    )
    value = st.number_input(
        "값",
        min_value=0.0,
        max_value=100.0,
        step=0.1,
        format="%.1f",
        key=value_key,
    )
    if st.button(f"💾 {label} 저장", key=f"btn_save_{indicator_key}"):
        source = "scraped" if st.session_state.get(f"pmi_scraped_{indicator_key}") else "manual"
        pmi_store.save_pmi_value(indicator_key, period, float(value), source=source)
        st.cache_data.clear()
        st.success(f"{label} {period} = {value:.1f} 저장 완료")
        st.rerun()


def render_pmi_card(indicator_key: str, monthly_data: dict) -> None:
    label = monthly_data["label"]
    with st.container(border=True):
        st.markdown(f"#### {label}")
        st.caption(f"가중치 {monthly_data['weight']:g} · {config.INDICATOR_META[indicator_key]['source']}")
        if monthly_data.get("has_data"):
            badge = "🟢 우호적" if monthly_data["favorable"] else "🔴 비우호적"
            stale_badge = " · ⚠️ 데이터 오래됨(점수 제외)" if monthly_data["stale"] else ""
            st.metric(f"{label} ({monthly_data['period']})", monthly_data["value_display"], delta=badge, delta_color="off")
            st.caption(
                f"최종 갱신일: {monthly_data['recorded_at']} · {monthly_data['days_since_recorded']}일 전{stale_badge}"
            )
            if monthly_data["streak_display"]:
                st.caption(f"📈 {monthly_data['streak_display']}")
        else:
            st.info("아직 저장된 값이 없습니다. 아래에서 새로고침하거나 직접 입력해 저장하세요.")
        _pmi_refresh_and_save(indicator_key, label)


def render_comex_card(comex: dict) -> None:
    with st.container(border=True):
        st.markdown(f"#### {comex['label']} (실시간 카드 — 점수/백테스트 미반영)")
        if comex["has_data"]:
            st.metric(f"최근 보고일: {comex['date']}", comex["tons_display"])
            st.caption(
                f"자동 수집 시작일: {comex['first_collection_date']} "
                f"({comex['days_of_history']}일치 축적됨 — 1년 이상 쌓이면 백테스트 포함 검토)"
            )
        else:
            st.info("아직 저장된 값이 없습니다.")
        st.caption(config.FOOTNOTES["comex_copper_stock"])

        stock_date = st.date_input("보고일", value=today_kst(), key="comex_date_input")
        tons = st.number_input("재고량 (톤)", min_value=0.0, step=1.0, key="comex_tons_input")
        if st.button("💾 COMEX 재고 저장", key="btn_save_comex"):
            comex_store.save_comex_value(stock_date, float(tons))
            st.cache_data.clear()
            st.success(f"{stock_date} = {tons:,.0f}톤 저장 완료")
            st.rerun()
        st.caption(
            "ℹ️ CME의 delivery_reports 엔드포인트는 자동 스크래핑이 이용약관상 명시적으로 금지되어 "
            "있어(요청 시 403과 함께 금지 문구 반환), 자동 수집 대신 수기 입력으로 운영합니다."
        )


def render_dashboard() -> None:
    st.title("구리(Copper) 상관관계 대시보드")

    today = today_kst()
    date_col, refresh_col = st.columns([4, 1])
    with date_col:
        selected_date = st.date_input(
            "기준일 선택 (일별 지표: 달러인덱스·WTI·금/구리비율)",
            value=today,
            min_value=EARLIEST_DATE,
            max_value=today,
            help="이 날짜(또는 그 이전 최근 거래일)의 종가를 기준으로 일별 지표를 계산합니다. "
            "PMI·COMEX는 이 날짜와 무관하게 항상 최근 저장값을 표시합니다.",
        )
    with refresh_col:
        st.write("")
        st.write("")
        force_live = st.button("새로고침", use_container_width=True)

    is_today = selected_date == today
    if force_live:
        st.cache_data.clear()

    try:
        data = load_daily_data(selected_date.isoformat(), is_today)
    except Exception as exc:
        st.error(f"데이터를 불러오지 못했습니다: {exc}")
        st.stop()

    # PMI/COMEX are recomputed fresh on every render (cheap local CSV reads,
    # no network) rather than read out of `data` — `data` is the cached
    # daily-batch JSON snapshot for "today" (data/latest.json, refreshed only
    # once a day), so a PMI value the user just saved through the UI below
    # would otherwise stay invisible until tomorrow's 07:00 KST batch run.
    # Always evaluated as of real "today", independent of the SMA table's
    # own as-of date selector above (PMI/COMEX are a separate, always-latest
    # view — see the date_input's help text).
    monthly_indicators = {
        key: build_table.build_monthly_indicator(key) for key in config.MONTHLY_INDICATOR_ORDER
    }
    comex = build_table.build_comex_card()
    score_directions = {key: data["indicators"][key]["score_direction"] for key in config.INDICATOR_ORDER}
    score_directions.update(
        {key: monthly_indicators[key].get("score_direction") for key in config.MONTHLY_INDICATOR_ORDER}
    )
    score = signals.compute_copper_friendly_score(score_directions)

    if is_today:
        st.caption(
            f"기준일(전일 마감 종가): **{data['as_of']}**  ·  생성시각(KST): {data['generated_at']}"
        )
    else:
        st.caption(
            f"선택한 기준일: **{selected_date}** → 실제 반영된 거래일: **{data['as_of']}** "
            "(주말·휴장일이면 직전 거래일 종가가 표시됩니다)"
        )
    st.caption(
        "⚠️ 실시간 시세가 아닙니다. 일별 지표는 매일 아침 7시(KST)에 자동 갱신되며, PMI·COMEX는 "
        "버튼을 눌러 수동으로 갱신합니다. 구리는 안전자산이 아닌 경기민감 산업금속이므로, 아래 "
        "지표 구성은 금(Gold) 대시보드의 VIX·실질금리 같은 안전자산 심리지표 대신 PMI 중심의 "
        "경기지표로 구성되어 있습니다."
    )

    render_score(score)

    st.markdown("#### 일별 지표 (자동 갱신)")
    st.caption(
        "🟢 옅은 녹색 배경 = 그 신호가 현재 구리값에 우호적인 방향인 셀입니다. 정방향 지표(WTI)는 "
        "종가가 이평선 위일 때, 역방향 지표(달러인덱스·금/구리비율)는 종가가 이평선 아래일 때 "
        "초록색으로 표시됩니다."
    )
    render_daily_table(data)

    st.markdown("#### PMI 지표 (반자동 갱신 — 월초 버튼 클릭 권장)")
    pmi_col1, pmi_col2 = st.columns(2)
    with pmi_col1:
        render_pmi_card("china_pmi", monthly_indicators["china_pmi"])
    with pmi_col2:
        render_pmi_card("us_pmi", monthly_indicators["us_pmi"])

    st.markdown("#### COMEX 재고 (실시간, 점수 미반영)")
    render_comex_card(comex)

    st.markdown("#### 지표별 시계열 그래프")
    st.caption(
        f"버튼을 누른 지표만 그 시점에 최근 {CHART_YEARS}년치 데이터를 받아와 그립니다 — 누르기 "
        "전에는 어떤 지표도 미리 계산하지 않습니다."
    )
    indicator_order = data["indicator_order"]
    indicators = data["indicators"]
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

    st.markdown("#### 지표별 참고 출처 및 한계")
    for k in indicator_order:
        st.caption(f"**{indicators[k]['label']}** — {data['footnotes'][k]}")
    for k in config.MONTHLY_INDICATOR_ORDER:
        st.caption(f"**{config.INDICATOR_META[k]['label']}** — {data['footnotes'][k]}")
    st.caption(f"**종합 한계** — {data['footnotes']['overall_limitation']}")


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
