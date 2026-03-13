import streamlit as st

from utils.db_handler import get_budgets, save_budgets, get_category_avg_monthly


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
    st.markdown('<div class="page-header">목표 예산</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">카테고리별 월 목표 예산을 설정합니다. 월 예산 수정 후 [업데이트] 버튼을 눌러주세요.</div>', unsafe_allow_html=True)

    if st.session_state.pop('budget_saved', False):
        st.success("목표 예산이 업데이트 되었습니다.")

    df = get_budgets()

    if df.empty:
        st.warning("카테고리 규칙 데이터가 없습니다. 먼저 데이터를 업로드해주세요.")
        return

    # 최근 1년 월평균 JOIN (양수 보장)
    avg_df = get_category_avg_monthly(months=12)
    avg_map = avg_df.set_index('category_1')['avg_monthly'].to_dict() if not avg_df.empty else {}
    df['avg_monthly'] = df['category'].map(avg_map).fillna(0).abs().astype(int)

    # UI용 컬럼 구성 (순서 중요)
    display_df = df[['sort_order', 'category', 'monthly_amount', 'avg_monthly', 'is_fixed_cost']].rename(columns={
        'sort_order': 'No.',
        'category': '카테고리',
        'monthly_amount': '월 예산 (원)',
        'avg_monthly': '최근 1년 월평균 (원)',
        'is_fixed_cost': '고정 지출',
    })

    # 총 예산 요약 (저장된 값 기준, 테이블 위에 표시)
    total     = int(display_df['월 예산 (원)'].sum())
    fixed     = int(display_df.loc[display_df['고정 지출'] == 1, '월 예산 (원)'].sum())
    variable  = total - fixed

    avg_total    = int(display_df['최근 1년 월평균 (원)'].sum())
    avg_fixed    = int(display_df.loc[display_df['고정 지출'] == 1, '최근 1년 월평균 (원)'].sum())
    avg_variable = avg_total - avg_fixed

    has_avg = avg_total > 0

    col1, col2, col3 = st.columns(3)
    col1.metric(
        "월 총 예산",
        f"{total:,}원",
        delta=f"{total - avg_total:+,}원" if has_avg else None,
        delta_color="inverse",
        help=f"최근 1년 월평균: {avg_total:,}원" if has_avg else "데이터 없음",
    )
    col2.metric(
        "고정 지출 합계",
        f"{fixed:,}원",
        delta=f"{fixed - avg_fixed:+,}원" if has_avg else None,
        delta_color="inverse",
        help=f"최근 1년 월평균: {avg_fixed:,}원" if has_avg else "데이터 없음",
    )
    col3.metric(
        "변동 지출 합계",
        f"{variable:,}원",
        delta=f"{variable - avg_variable:+,}원" if has_avg else None,
        delta_color="inverse",
        help=f"최근 1년 월평균: {avg_variable:,}원" if has_avg else "데이터 없음",
    )

    st.markdown("---")

    st.markdown("#### 카테고리별 월 예산")

    edited = st.data_editor(
        display_df,
        column_config={
            'No.': st.column_config.NumberColumn(
                min_value=1,
                step=1,
                format="%d",
            ),
            '카테고리': st.column_config.TextColumn(disabled=True),
            '월 예산 (원)': st.column_config.NumberColumn(
                min_value=0,
                step=10000,
                format="%d",
            ),
            '최근 1년 월평균 (원)': st.column_config.NumberColumn(
                disabled=True,
                format="%d",
            ),
            '고정 지출': st.column_config.CheckboxColumn(),
        },
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        key="budget_editor",
    )

    if st.button("업데이트", use_container_width=True):
        save_df = edited.rename(columns={
            'No.': 'sort_order',
            '카테고리': 'category',
            '월 예산 (원)': 'monthly_amount',
            '고정 지출': 'is_fixed_cost',
        }).drop(columns=['최근 1년 월평균 (원)'])
        # No. 기준으로 재정렬하여 저장
        save_df = save_df.sort_values('sort_order').reset_index(drop=True)
        try:
            save_budgets(save_df)
            st.session_state['budget_saved'] = True
        except Exception as e:
            st.error(f"저장 실패: {e}")

render()
