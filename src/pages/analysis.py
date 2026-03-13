import calendar
import hashlib
import json
import os
from datetime import date

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from openai import OpenAI

from utils.ai_agent import (
    STANDARD_CATEGORIES, generate_analysis_summary,
    compute_anomaly_metrics, compute_burnrate_metrics,
)
from utils.db_handler import (
    get_analyzed_transactions, get_asset_history, get_budgets,
    fill_combined_trend,
)


def render():
    st.markdown("""
        <style>
        .page-header {
            text-align: center; padding: 1rem 0 0.5rem;
            color: #000000; font-size: 2.5rem; font-weight: 700;
        }
        .page-subtitle {
            text-align: center; color: var(--text-color);
            opacity: 0.7; font-size: 1rem; margin-bottom: 2rem;
        }
        </style>
    """, unsafe_allow_html=True)
    st.markdown('<div class="page-header">분석 리포트</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">과거 패턴을 분석하여 소비 현황과 자산 흐름을 파악합니다.</div>', unsafe_allow_html=True)

    df_all = get_analyzed_transactions()
    if df_all.empty:
        st.info("데이터가 없습니다. 먼저 데이터를 업로드해주세요.")
        return

    # 메트릭 계산 → GPT 요약 카드
    anomaly_metrics = compute_anomaly_metrics(df_all)
    burnrate_metrics = compute_burnrate_metrics(df_all)
    _render_summary_card(anomaly_metrics, burnrate_metrics)

    st.subheader("🚨 이상 지출")
    _render_anomaly(df_all)

    st.divider()

    st.subheader("📊 목표 예산 현황")
    _render_burnrate_by_category(df_all)

    st.divider()

    st.subheader("💸 지출 분석")
    _render_burnrate(df_all)

    st.divider()

    st.subheader("📈 자산 트렌드")
    _render_asset_trend(owner="전체")


# ──────────────────────────────────────────────
# GPT 요약 카드
# ──────────────────────────────────────────────

def _render_summary_card(anomaly_metrics: dict | None, burnrate_metrics: dict | None):
    """GPT 기반 분석 요약 안내글 카드."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return

    # 데이터 해시 기반 세션 캐시 (같은 데이터 → 재호출 없음)
    metrics_hash = hashlib.md5(
        json.dumps([anomaly_metrics, burnrate_metrics], sort_keys=True, default=str).encode()
    ).hexdigest()[:10]
    cache_key = f"analysis_summary_{date.today().isoformat()}_{metrics_hash}"

    if cache_key not in st.session_state:
        with st.spinner("AI 요약 생성 중..."):
            try:
                client = OpenAI(api_key=api_key)
                summary = generate_analysis_summary(client, anomaly_metrics, burnrate_metrics)
            except Exception:
                summary = ""
        st.session_state[cache_key] = summary
    else:
        summary = st.session_state[cache_key]

    if summary:
        st.markdown(f"""
            <div style="background:var(--background-color);
                        border-left:4px solid #667eea;
                        padding:0.9rem 1.2rem;
                        border-radius:4px;
                        margin-bottom:1.5rem;
                        font-size:0.95rem;
                        line-height:1.75;">
                🤖 {summary}
            </div>
        """, unsafe_allow_html=True)


# ──────────────────────────────────────────────
# 이상 지출 탐지
# ──────────────────────────────────────────────

def _render_anomaly(df_all):
    df = df_all[df_all['tx_type'] == '지출'].copy()
    if df.empty:
        st.info("지출 데이터가 없습니다.")
        return

    df['amount_abs'] = df['amount'].abs()
    df['date'] = pd.to_datetime(df['date'])
    df['year_month'] = df['date'].dt.to_period('M')

    today = date.today()
    current_period = pd.Period(today, 'M')
    today_day = today.day

    past_df = df[
        (df['year_month'] < current_period) &
        (df['year_month'] >= current_period - 12)
    ].copy()
    current_df = df[df['year_month'] == current_period].copy()

    past_months = past_df['year_month'].nunique()
    if past_months < 3:
        st.info(f"최소 3개월 이상 과거 데이터가 필요합니다. (현재 {past_months}개월 보유)")
        return

    # 과거 데이터도 동일 구간(1일~당일)으로 한정하여 공정 비교
    past_same_period = past_df[past_df['date'].dt.day <= today_day].copy()

    past_monthly = (
        past_same_period.groupby(['year_month', 'category_1'])['amount_abs']
        .sum().reset_index()
    )
    past_stats = (
        past_monthly.groupby('category_1')['amount_abs']
        .agg(['mean', 'std']).reset_index()
    )
    past_stats.columns = ['category_1', 'mean', 'std']

    if current_df.empty:
        st.info("이번 달 지출 데이터가 없습니다.")
        return

    current_monthly = (
        current_df.groupby('category_1')['amount_abs']
        .sum().reset_index()
        .rename(columns={'amount_abs': 'current_amount'})
    )

    merged = current_monthly.merge(past_stats, on='category_1', how='left').dropna(subset=['mean', 'std'])
    anomalies = merged[
        (merged['std'] > 0) &
        (abs(merged['current_amount'] - merged['mean']) > 2 * merged['std'])
    ].copy()

    st.caption(f"비교 기준: 매월 1일~{today_day}일 누적 지출 / 과거 {past_months}개월 평균")

    if anomalies.empty:
        st.success("이번 달 이상 지출이 감지되지 않았습니다.")
        return

    for _, row in anomalies.iterrows():
        diff = row['current_amount'] - row['mean']
        pct = (diff / row['mean'] * 100) if row['mean'] > 0 else 0
        sign = "+" if diff > 0 else ""
        st.warning(
            f"🚨 **{row['category_1']}** — 이번 달 {today_day}일까지 {int(row['current_amount']):,}원 "
            f"(평균 대비 {sign}{pct:.0f}%, {sign}{int(diff):,}원)"
        )
        with st.expander("상세 내역 보기"):
            detail = (
                current_df[current_df['category_1'] == row['category_1']]
                [['date', 'description', 'amount_abs', 'memo']].copy()
                .rename(columns={'date': '일자', 'description': '내용',
                                 'amount_abs': '금액', 'memo': '메모'})
            )
            detail['금액'] = detail['금액'].apply(lambda x: f"{int(x):,}원")
            detail['일자'] = detail['일자'].dt.strftime('%Y-%m-%d')
            st.dataframe(detail, use_container_width=True, hide_index=True)


# ──────────────────────────────────────────────
# 지출 예측 (Burn-rate)
# ──────────────────────────────────────────────

def _render_burnrate(df_all):
    today = date.today()
    first_of_month = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    end_of_month = date(today.year, today.month, days_in_month)

    df = df_all.copy()
    df['date'] = pd.to_datetime(df['date'])
    df['year_month'] = df['date'].dt.to_period('M')
    current_period = pd.Period(today, 'M')

    # 카테고리 목록: STANDARD_CATEGORIES 기준 + 예산에 있는 추가 카테고리
    budgets_df = get_budgets()
    variable_cats = (
        set(budgets_df.loc[budgets_df['is_fixed_cost'] == 0, 'category'].tolist())
        if not budgets_df.empty else set()
    )
    known_cats = set(budgets_df['category'].tolist()) if not budgets_df.empty else set()
    std_cats_available = [c for c in STANDARD_CATEGORIES if c in known_cats]
    extra_cats = sorted(known_cats - set(STANDARD_CATEGORIES))
    all_categories = ["전체"] + std_cats_available + extra_cats

    col_cat, _col2, _col3 = st.columns(3)
    with col_cat:
        selected_cat = st.selectbox("카테고리 선택", all_categories)

    exclude_variable = False
    if selected_cat == "전체":
        exclude_variable = st.checkbox("변동지출 제외", value=False)

    # 이번 달 지출 (1일~오늘)
    df_month = df[
        (df['tx_type'] == '지출') &
        (df['date'] >= pd.Timestamp(first_of_month)) &
        (df['date'] <= pd.Timestamp(today))
    ].copy()
    df_month['amount_abs'] = df_month['amount'].abs()
    if selected_cat != "전체":
        df_month = df_month[df_month['category_1'] == selected_cat]
    elif exclude_variable and variable_cats:
        df_month = df_month[~df_month['category_1'].isin(variable_cats)]

    # 일별 합산 → 누적합
    daily = df_month.groupby('date')['amount_abs'].sum().reset_index()
    date_range = pd.date_range(start=first_of_month, end=today)
    daily = (
        daily.set_index('date').reindex(date_range, fill_value=0).reset_index()
        .rename(columns={'index': 'date', 'amount_abs': 'amount'})
    )
    daily['cumulative'] = daily['amount'].cumsum()
    current_total = int(daily['cumulative'].iloc[-1]) if not daily.empty else 0

    # 과거 12개월 일별 평균 패턴으로 비선형 예측
    past_12_df = df[
        (df['tx_type'] == '지출') &
        (df['year_month'] < current_period) &
        (df['year_month'] >= current_period - 12)
    ].copy()
    past_12_df['amount_abs'] = past_12_df['amount'].abs()
    past_12_df['day_of_month'] = past_12_df['date'].dt.day
    if selected_cat != "전체":
        past_12_df = past_12_df[past_12_df['category_1'] == selected_cat]
    elif exclude_variable and variable_cats:
        past_12_df = past_12_df[~past_12_df['category_1'].isin(variable_cats)]

    past_daily_pattern = pd.Series(dtype=float)
    if not past_12_df.empty:
        n_months = past_12_df['year_month'].nunique()
        past_daily_pattern = (
            past_12_df.groupby(['year_month', 'day_of_month'])['amount_abs']
            .sum().reset_index()
            .groupby('day_of_month')['amount_abs']
            .sum()
            .div(n_months)
        )

    remaining_days = range(today.day + 1, days_in_month + 1)
    projected_total = current_total + int(sum(past_daily_pattern.get(d, 0) for d in remaining_days))

    # 지난달 실제 지출
    last_period = current_period - 1
    last_month_df = df[
        (df['tx_type'] == '지출') &
        (df['year_month'] == last_period)
    ].copy()
    last_month_df['amount_abs'] = last_month_df['amount'].abs()
    last_month_df['day_of_month'] = last_month_df['date'].dt.day
    if selected_cat != "전체":
        last_month_df = last_month_df[last_month_df['category_1'] == selected_cat]
    elif exclude_variable and variable_cats:
        last_month_df = last_month_df[~last_month_df['category_1'].isin(variable_cats)]

    last_month_total = int(last_month_df[last_month_df['day_of_month'] <= today.day]['amount_abs'].sum())

    last_month_dates = []
    last_month_values = []
    if not last_month_df.empty:
        last_daily = last_month_df.groupby('day_of_month')['amount_abs'].sum()
        running_last = 0.0
        for d in range(1, days_in_month + 1):
            running_last += float(last_daily.get(d, 0))
            last_month_dates.append(pd.Timestamp(date(today.year, today.month, d)))
            last_month_values.append(round(running_last))

    # 예산 (변동지출 제외 시 고정지출 예산만 합산)
    if selected_cat == "전체":
        if not budgets_df.empty:
            bdf = budgets_df[~budgets_df['category'].isin(variable_cats)] if exclude_variable else budgets_df
            budget_total = int(bdf['monthly_amount'].sum())
        else:
            budget_total = 0
    else:
        row = budgets_df[budgets_df['category'] == selected_cat]
        budget_total = int(row['monthly_amount'].iloc[0]) if not row.empty else 0

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("현재 누적 지출", f"{current_total:,}원")
    with col2:
        last_diff = current_total - last_month_total if last_month_total > 0 else None
        st.metric(
            "지난달 누적 지출 (현재 기준)",
            f"{last_month_total:,}원" if last_month_total > 0 else "데이터 없음",
            delta=f"{last_diff:+,}원" if last_diff is not None else None,
            delta_color="inverse",
        )
    with col3:
        if budget_total > 0:
            budget_diff = projected_total - budget_total
            sign = "+" if budget_diff > 0 else ""
            st.metric(
                "예상 월말 지출",
                f"{projected_total:,}원",
                delta=f"{sign}{budget_diff:,}원 (예산 대비)",
                delta_color="inverse",
                help=f"설정 예산: {budget_total:,}원",
            )
        else:
            st.metric("예상 월말 지출", f"{projected_total:,}원", help="설정된 예산이 없습니다.")

    if current_total == 0 and projected_total == 0:
        st.info("이번 달 지출 데이터가 없습니다.")
        return

    # 예측 곡선 구성 (오늘 지점부터 연결)
    pred_dates = [pd.Timestamp(today)]
    pred_values = [current_total]
    running = float(current_total)
    for d in remaining_days:
        running += float(past_daily_pattern.get(d, 0))
        pred_dates.append(pd.Timestamp(date(today.year, today.month, d)))
        pred_values.append(round(running))

    past_months_count = past_12_df['year_month'].nunique() if not past_12_df.empty else 0
    subtitle = f"과거 {past_months_count}개월 패턴 기반" if past_months_count > 0 else "과거 데이터 없음"

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=daily['date'], y=daily['cumulative'],
        mode='lines', name='현재 누적 지출',
        line=dict(color='#667eea', width=2),
    ))
    if last_month_dates:
        fig.add_trace(go.Scatter(
            x=last_month_dates, y=last_month_values,
            mode='lines', name='지난달 지출',
            line=dict(color='#cccccc', width=1.5),
        ))
    fig.add_trace(go.Scatter(
        x=pred_dates, y=pred_values,
        mode='lines', name='지출 예측 (과거 12개월 기준)',
        line=dict(color='#999999', width=2, dash='dot'),
    ))
    if budget_total > 0:
        all_dates = pd.date_range(start=first_of_month, end=end_of_month)
        fig.add_trace(go.Scatter(
            x=all_dates, y=[budget_total] * len(all_dates),
            mode='lines', name='예산',
            line=dict(color='#e74c3c', width=1.5, dash='dash'),
        ))

    fig.update_layout(
        title=f'{today.year}년 {today.month}월 지출 예측 ({selected_cat}) — {subtitle}',
        xaxis_title='날짜', yaxis_title='금액 (원)',
        yaxis_tickformat=',', hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)




def _render_category_card(card: dict, df_month: pd.DataFrame | None = None, avg_1y: int = 0):
    """카테고리별 진행 카드 (HTML progress bar)를 렌더링한다."""
    cat = card['cat']
    cur = card['cur']
    budget = card['budget']
    prorated = card['prorated']
    pct = card['pct']
    exceeded = card['exceeded']
    is_fixed = card.get('is_fixed_cost')

    if is_fixed == 1:
        cost_badge = '<span style="font-size:10px;font-weight:500;color:#5f6b7a;background:#e8ecf0;border-radius:4px;padding:1px 5px;margin-left:5px;">고정</span>'
    elif is_fixed == 0:
        cost_badge = '<span style="font-size:10px;font-weight:500;color:#5f6b7a;background:#e8ecf0;border-radius:4px;padding:1px 5px;margin-left:5px;">변동</span>'
    else:
        cost_badge = ''

    has_budget = budget > 0
    # 예산 없는 카드: cur/avg 기준 상대 스케일로 바 표시
    if not has_budget and (cur > 0 or avg_1y > 0):
        max_ref = max(cur, avg_1y)
        bar_width = cur / max_ref * 100 if max_ref > 0 else 0
        avg_pct: float | None = avg_1y / max_ref * 100 if (avg_1y > 0 and max_ref > 0) else None
    else:
        bar_width = min(pct, 100) if pct is not None else 0
        avg_pct = min(avg_1y / budget * 100, 120) if (has_budget and avg_1y > 0) else None
    marker_pct = (prorated / budget * 100) if has_budget else None
    budget_text = f'{budget:,}원' if has_budget else '미설정'
    bar_bg = '#dfe6e9' if not has_budget else '#ecf0f1'

    # diff: 잔여 관점 (양수=여유, 음수=초과)
    diff = prorated - cur if has_budget else None
    if diff is not None:
        over_pct = (-diff / budget * 100) if budget > 0 else 0  # 전체 예산 대비 초과율(양수)
        diff_pct = -over_pct
        diff_text = f'{diff:+,}원 ({diff_pct:+.1f}%)'
    else:
        over_pct = 0
        diff_pct = None
        diff_text = '예산 없음'

    # 3단계 상태: 정상 / 주의(0~10% 초과) / 초과(10% 초과)
    if not has_budget or diff_pct is None or diff_pct >= 0:
        status_icon, status_color = ('✅ 정상', '#27ae60') if has_budget else ('', '#95a5a6')
    elif over_pct <= 20:
        status_icon, status_color = '⚠️ 주의', '#f39c12'
    else:
        status_icon, status_color = '🚨 경고', '#e74c3c'

    bar_color = status_color if exceeded else '#667eea'
    diff_color = status_color if diff is not None and diff < 0 else ('#27ae60' if diff is not None and diff > 0 else '#95a5a6')

    # 오늘 기준 예산선: position:absolute 세로선
    marker_div = (
        f'<div style="position:absolute;left:{marker_pct:.1f}%;top:-2px;width:2px;height:calc(100% + 4px);background:#555555;border-radius:1px;"></div>'
        if marker_pct is not None else ''
    )
    # 1년 평균선: 점선 세로선
    avg_div = (
        f'<div style="position:absolute;left:{min(avg_pct, 100):.1f}%;top:-2px;width:0;height:calc(100% + 4px);border-left:2px dashed #888888;"></div>'
        if avg_pct is not None else ''
    )
    prorated_label = f'{cur:,}원 / {prorated:,}원' if has_budget else f'{cur:,}원'
    budget_footer = f'월 예산: {budget_text}' if has_budget else ''
    avg_footer = f'1년 평균 {avg_1y:,}원' if avg_1y > 0 else ''

    html = (
        f'<div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;'
        f'padding:14px 16px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.06);">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
        f'<span style="font-weight:600;font-size:14px;color:#2c3e50;">{cat}{cost_badge}</span>'
        f'<span style="font-size:12px;color:{status_color};font-weight:500;">{"" if not has_budget else status_icon}</span>'
        f'</div>'
        f'<div style="font-size:12px;color:#7f8c8d;margin-bottom:6px;">'
        f'{prorated_label}'
        f'<span style="float:right;font-weight:600;color:{diff_color};">{diff_text}</span>'
        f'</div>'
        f'<div style="position:relative;background:{bar_bg};border-radius:4px;height:10px;">'
        f'<div style="width:{bar_width:.1f}%;background:{bar_color};height:10px;border-radius:4px;"></div>'
        f'{marker_div}'
        f'{avg_div}'
        f'</div>'
        f'<div style="font-size:10px;margin-top:4px;display:flex;justify-content:space-between;">'
        f'<span style="color:#b2bec3;">{budget_footer}</span>'
        f'<span style="color:#b2bec3;">{avg_footer}</span>'
        f'</div>'
        f'</div>'
    )
    st.markdown(html, unsafe_allow_html=True)

    if df_month is not None and not df_month.empty:
        src = df_month if cat == '전체' else df_month[df_month['category_1'] == cat]
        if not src.empty:
            with st.expander("상세 내역 보기"):
                detail = (
                    src[['date', 'description', 'amount_abs', 'memo']].copy()
                    .rename(columns={'date': '일자', 'description': '내용',
                                     'amount_abs': '금액', 'memo': '메모'})
                    .sort_values('일자', ascending=False)
                )
                detail['금액'] = detail['금액'].apply(lambda x: f"{int(x):,}원")
                detail['일자'] = detail['일자'].dt.strftime('%Y-%m-%d')
                st.dataframe(detail, use_container_width=True, hide_index=True)


def _render_burnrate_by_category(df_all):
    """카테고리별 지출 현황을 전체 요약 카드 + 3열 카드 그리드로 표시한다."""
    budgets_df = get_budgets()
    today = date.today()
    first_of_month = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    df = df_all.copy()
    df['date'] = pd.to_datetime(df['date'])

    df_month = df[
        (df['tx_type'] == '지출') &
        (df['date'] >= pd.Timestamp(first_of_month)) &
        (df['date'] <= pd.Timestamp(today))
    ].copy()
    df_month['amount_abs'] = df_month['amount'].abs()

    variable_cats = (
        set(budgets_df.loc[budgets_df['is_fixed_cost'] == 0, 'category'].tolist())
        if not budgets_df.empty else set()
    )
    exclude_variable = st.checkbox("변동지출 제외", value=False, key="burnrate_cat_exclude_variable")
    if exclude_variable and variable_cats:
        df_month = df_month[~df_month['category_1'].isin(variable_cats)]

    current_by_cat = df_month.groupby('category_1')['amount_abs'].sum()
    if current_by_cat.empty:
        return

    budget_map = dict(zip(budgets_df['category'], budgets_df['monthly_amount'])) if not budgets_df.empty else {}
    sort_order_map = dict(zip(budgets_df['category'], budgets_df['sort_order'])) if not budgets_df.empty else {}
    is_fixed_map = dict(zip(budgets_df['category'], budgets_df['is_fixed_cost'])) if not budgets_df.empty else {}

    # 지난 1년 카테고리별 동일 일자 구간(1일~today.day) 월평균 계산
    year_ago = today - pd.DateOffset(months=12)
    df_year = df[
        (df['tx_type'] == '지출') &
        (df['date'] >= pd.Timestamp(year_ago)) &
        (df['date'] < pd.Timestamp(first_of_month))
    ].copy()
    df_year['amount_abs'] = df_year['amount'].abs()
    df_year['year_month'] = df_year['date'].dt.to_period('M')
    df_year_same = df_year[df_year['date'].dt.day <= today.day]
    if exclude_variable and variable_cats:
        df_year_same = df_year_same[~df_year_same['category_1'].isin(variable_cats)]
    year_avg_map: dict[str, int] = {}
    n_year_months = df_year_same['year_month'].nunique()
    if n_year_months > 0:
        year_avg_map = (
            df_year_same.groupby(['year_month', 'category_1'])['amount_abs']
            .sum().reset_index()
            .groupby('category_1')['amount_abs']
            .sum()
            .div(n_year_months)
            .round().astype(int)
            .to_dict()
        )

    # 카드 데이터 구성
    cards = []
    for cat, cur in current_by_cat.items():
        budget = budget_map.get(cat, 0)
        prorated = round(budget * today.day / days_in_month) if budget > 0 else 0
        pct = (cur / budget * 100) if budget > 0 else None
        exceeded = (prorated > 0 and cur > prorated)
        cards.append(dict(cat=cat, cur=int(cur), budget=int(budget),
                          prorated=prorated, pct=pct, exceeded=exceeded,
                          sort_order=sort_order_map.get(cat, 999),
                          is_fixed_cost=is_fixed_map.get(cat),
                          avg_1y=year_avg_map.get(cat, 0)))

    # sort_order 오름차순 정렬 (예산 없는 항목은 맨 뒤)
    cards.sort(key=lambda c: (c['sort_order'], c['cat']))

    # 전체 요약 카드 — 예산은 budgets_df 전체 합산 (지출 없는 카테고리도 포함)
    total_cur = sum(c['cur'] for c in cards)
    budgets_base = budgets_df[~budgets_df['category'].isin(variable_cats)] if (exclude_variable and variable_cats) else budgets_df
    total_budget = int(budgets_base['monthly_amount'].sum()) if not budgets_base.empty else 0
    total_prorated = round(total_budget * today.day / days_in_month) if total_budget > 0 else 0
    total_exceeded = (total_prorated > 0 and total_cur > total_prorated)
    total_avg_1y = sum(c['avg_1y'] for c in cards)
    total_card = dict(cat='전체', cur=total_cur, budget=total_budget,
                      prorated=total_prorated, pct=(total_cur / total_budget * 100) if total_budget > 0 else None,
                      exceeded=total_exceeded)

    if total_budget > 0:
        total_diff = total_prorated - total_cur
        st.metric(
            "현재 일자 기준 예산 잔여액",
            f"{total_diff:+,}원",
            help=f"누적 지출: {total_cur:,}원 / 현재 일자 기준 예산: {total_prorated:,}원",
        )

    _render_category_card(total_card, df_month, avg_1y=total_avg_1y)
    st.markdown('<div style="margin-top:8px;"></div>', unsafe_allow_html=True)

    N_COLS = 3
    for row_start in range(0, len(cards), N_COLS):
        cols = st.columns(N_COLS)
        for col_idx, card in enumerate(cards[row_start:row_start + N_COLS]):
            with cols[col_idx]:
                _render_category_card(card, df_month, avg_1y=card.get('avg_1y', 0))


# ──────────────────────────────────────────────
# 자산 트렌드
# ──────────────────────────────────────────────

def _render_asset_trend(owner: str):
    df = get_asset_history()
    if df.empty:
        st.info("자산 스냅샷 데이터가 없습니다. 먼저 자산 데이터를 업로드해주세요.")
        return

    df['snapshot_date'] = pd.to_datetime(df['snapshot_date'])

    if owner == "전체":
        trend = fill_combined_trend(df)
        trend['snapshot_date'] = pd.to_datetime(trend['snapshot_date'])
    else:
        trend = df[df['owner'] == owner].copy()

    if trend.empty or len(trend) < 2:
        st.info("자산 트렌드를 표시하려면 2개 이상의 스냅샷이 필요합니다.")
        return

    trend = trend.sort_values('snapshot_date')
    trend['ma3'] = trend['net_worth'].rolling(3, min_periods=1).mean().round().astype('Int64')

    # 2년 예측 (스냅샷 3개 이상일 때만)
    # - 근 2년치 데이터만 피팅하여 최근 트렌드 반영
    # - 오늘(today_ts)부터 24개월 앞까지 예측
    forecast_dates = []
    forecast_values = []
    if len(trend) >= 3:
        today_ts = pd.Timestamp(date.today())
        cutoff = today_ts - pd.DateOffset(years=2)
        trend_fit = trend[trend['snapshot_date'] >= cutoff]
        if len(trend_fit) < 3:
            trend_fit = trend  # 2년치 3개 미만이면 전체 사용

        origin = trend_fit['snapshot_date'].min()
        x = np.array([(d - origin).days for d in trend_fit['snapshot_date']])
        y = trend_fit['net_worth'].values
        slope_per_day = np.polyfit(x, y, 1)[0]  # 기울기(원/일)만 사용, 절편 버림

        # 시작점을 실제 최신 순자산으로 고정 후 기울기 적용
        base_value = float(trend.iloc[-1]['net_worth'])
        forecast_dates = [today_ts + pd.DateOffset(months=i) for i in range(0, 25)]
        days_from_today = np.array([(d - today_ts).days for d in forecast_dates])
        forecast_values = np.round(base_value + slope_per_day * days_from_today).astype(int)

    latest = trend.iloc[-1]
    prev = trend.iloc[-2]
    delta = latest['net_worth'] - prev['net_worth']

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("총 자산", f"{int(latest['total_asset']):,}원")
    with col2:
        st.metric("총 부채", f"{int(latest['total_debt']):,}원")
    with col3:
        st.metric("순 자산", f"{int(latest['net_worth']):,}원", delta=f"{int(delta):,}원")

    owner_note = "전체 (보정 합산)" if owner == "전체" else owner
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=trend['snapshot_date'], y=trend['net_worth'],
        mode='lines+markers', name='순자산',
        opacity=0.6, line=dict(color='#667eea', width=1.5), marker=dict(size=4),
    ))
    fig.add_trace(go.Scatter(
        x=trend['snapshot_date'], y=trend['ma3'],
        mode='lines', name='3개월 이동평균',
        line=dict(color='#764ba2', width=3),
    ))
    if forecast_dates:
        fig.add_trace(go.Scatter(
            x=forecast_dates, y=forecast_values,
            mode='lines', name='2년 예측 (선형 추세)',
            line=dict(color='#999999', width=2, dash='dot'),
        ))
    fig.update_layout(
        title=f'자산 트렌드 — {owner_note}',
        xaxis_title='날짜', yaxis_title='금액 (원)',
        yaxis_tickformat=',', hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)

render()
