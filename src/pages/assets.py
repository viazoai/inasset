import streamlit as st
import pandas as pd
from datetime import timedelta
from utils.db_handler import get_previous_assets, get_available_asset_months, get_assets_for_month

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
    st.markdown('<div class="page-header">자산 현황</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">현재 자산 분포와 지난달 말일 대비 흐름을 확인합니다.</div>', unsafe_allow_html=True)

    # 1. 연/월 선택기
    months_df = get_available_asset_months()
    if months_df.empty:
        st.info("기록된 자산 데이터가 없습니다. 먼저 데이터를 업로드해주세요.")
        return

    available_years = months_df['year'].unique().tolist()  # 이미 내림차순 정렬됨

    _, sel_col1, sel_col2, _ = st.columns([2, 1, 1, 2])
    with sel_col1:
        sel_year = st.selectbox("연도", available_years, index=0, key="asset_year")

    available_months = months_df[months_df['year'] == sel_year]['month'].tolist()
    with sel_col2:
        sel_month = st.selectbox("월", available_months, index=0, key="asset_month",
                                 format_func=lambda m: f"{m}월")

    # 2. 선택 연월 데이터 로드
    df_assets = get_assets_for_month(sel_year, sel_month)
    if df_assets.empty:
        st.info(f"{sel_year}년 {sel_month}월 자산 데이터가 없습니다.")
        return

    # 전처리: 부채를 음수로 변환
    df_assets.loc[df_assets['balance_type'] == '부채', 'amount'] *= -1

    # 2. 소유자별 데이터 처리 및 Delta 계산을 위한 사전 준비
    owners = sorted(df_assets['owner'].unique())
    summary_data = {} # 각 소유자 및 '전체'의 계산 결과 저장

    # 전체 합계를 위한 변수 초기화
    total_metrics = {
        'cur_asset': 0, 'prev_asset': 0,
        'cur_net': 0, 'prev_net': 0,
        'cur_cash': 0, 'prev_cash': 0,
        'cur_stock': 0, 'prev_stock': 0,
        'prev_date_info': [] # 여러 날짜가 섞일 수 있으므로 리스트로 관리
    }

    for owner in owners:
        owner_data = df_assets[df_assets['owner'] == owner]
        current_date_str = owner_data['snapshot_date'].iloc[0]
        current_date = pd.to_datetime(current_date_str)
        
        # 지난달 말일 계산
        target_prev_date = current_date.replace(day=1) - timedelta(days=1)
        
        # DB에서 해당 소유자의 30일 전과 가장 가까운 데이터 가져오기 (db_handler 기능 활용)
        # get_previous_assets 함수가 owner와 target_date를 받는다고 가정하고 로직 구성
        df_prev_owner = get_previous_assets(target_date=target_prev_date.strftime('%Y-%m-%d'), owner=owner)
        
        if not df_prev_owner.empty:
            df_prev_owner.loc[df_prev_owner['balance_type'] == '부채', 'amount'] *= -1
            prev_date_val = df_prev_owner['snapshot_date'].iloc[0].split()[0]
        else:
            prev_date_val = "데이터 없음"

        # 지표 계산 함수 (현금, 주식 등 로직 유지)
        def get_metrics(df):
            if df.empty: return 0, 0, 0, 0, 0
            asset = df[df['amount'] > 0]['amount'].sum()
            net = df['amount'].sum()
            cash = df[df['asset_type'].isin(['현금 자산', '자유입출금 자산'])]['amount'].sum() + \
                   df[df['account_name'] == '예비 계좌 (네이버)']['amount'].sum()
            stock = df[df['asset_type'] == '투자성 자산']['amount'].sum() - \
                    df[df['account_name'] == '예비 계좌 (네이버)']['amount'].sum()
            debt = df[df['amount'] < 0]['amount'].sum()
            return asset, net, cash, stock, debt

        cur_a, cur_n, cur_c, cur_s, cur_d = get_metrics(owner_data)
        pre_a, pre_n, pre_c, pre_s, _ = get_metrics(df_prev_owner)

        # 소유자별 결과 저장
        summary_data[owner] = {
            'cur': (cur_a, cur_n, cur_c, cur_s, cur_d),
            'prev': (pre_a, pre_n, pre_c, pre_s),
            'prev_date': prev_date_val,
            'help_texts': {
                'asset': f"{prev_date_val}일자 총 자산: {pre_a:,.0f}원",
                'net': f"{prev_date_val}일자 순 자산: {pre_n:,.0f}원",
                'cash': f"{prev_date_val}일자 현금: {pre_c:,.0f}원",
                'stock': f"{prev_date_val}일자 주식: {pre_s:,.0f}원",
            },
            'display_df': owner_data
        }

        # '전체' 합산
        total_metrics['cur_asset'] += cur_a
        total_metrics['prev_asset'] += pre_a
        total_metrics['cur_net'] += cur_n
        total_metrics['prev_net'] += pre_n
        total_metrics['cur_cash'] += cur_c
        total_metrics['prev_cash'] += pre_c
        total_metrics['cur_stock'] += cur_s
        total_metrics['prev_stock'] += pre_s
        if prev_date_val != "데이터 없음":
            total_metrics['prev_date_info'].append(f"{owner}({prev_date_val})")

    # 3. UI 렌더링 (Tabs)
    tab_names = ['전체'] + [f"{o}님" for o in owners]
    tabs = st.tabs(tab_names)

    for idx, name in enumerate(tab_names):
        with tabs[idx]:
            if name == '전체':
                c_a, c_n, c_c, c_s = total_metrics['cur_asset'], total_metrics['cur_net'], total_metrics['cur_cash'], total_metrics['cur_stock']
                p_a, p_n, p_c, p_s = total_metrics['prev_asset'], total_metrics['prev_net'], total_metrics['prev_cash'], total_metrics['prev_stock']
                date_summary = ", ".join(total_metrics['prev_date_info'])
                helps = {
                    'asset': f"{date_summary}일자 총 자산: {p_a:,.0f}원",
                    'net': f"{date_summary}일자 순 자산: {p_n:,.0f}원",
                    'cash': f"{date_summary}일자 현금: {p_c:,.0f}원",
                    'stock': f"{date_summary}일자 주식: {p_s:,.0f}원",
                }
                display_df = df_assets
                debt_sum = df_assets[df_assets['amount'] < 0]['amount'].sum()
            else:
                owner_name = name.replace('님', '')
                res = summary_data[owner_name]
                c_a, c_n, c_c, c_s, debt_sum = res['cur']
                p_a, p_n, p_c, p_s = res['prev']
            
                helps = res['help_texts']
                display_df = res['display_df']

            # Delta 계산 헬퍼
            def get_delta(cur, prev):
                if prev == 0: return None
                return f"{cur - prev:,.0f}원"

            # 상단 메트릭
            m_col1, m_col2 = st.columns(2)
            with m_col1:
                # 각 metric에 맞는 help 텍스트 할당
                st.metric("총 자산", f"{c_a:,.0f}원", delta=get_delta(c_a, p_a), help=helps['asset'])
                st.metric("순 자산", f"{c_n:,.0f}원", delta=get_delta(c_n, p_n), help=helps['net'])
                if debt_sum != 0: st.caption(f" ㄴ 총 부채: {debt_sum:,.0f}원")
            with m_col2:
                # 현금과 주식에도 help 추가
                st.metric("현금", f"{c_c:,.0f}원", delta=get_delta(c_c, p_c), help=helps['cash'])
                st.metric("주식", f"{c_s:,.0f}원", delta=get_delta(c_s, p_s), help=helps['stock'])

            st.divider()
            
            # 상세 내역 (기존 필터 로직 유지)
            render_detail_table(display_df, name)

def render_detail_table(df, key_suffix):
    st.subheader("상세 내역")
    f_col1, f_col2 = st.columns([1, 2])
    with f_col1:
        selected_cats = st.multiselect("분류", sorted(df['asset_type'].unique()), key=f"cat_{key_suffix}")
    with f_col2:
        search = st.text_input("자산명 검색", key=f"search_{key_suffix}")

    filtered = df.copy()
    if selected_cats: filtered = filtered[filtered['asset_type'].isin(selected_cats)]
    if search: filtered = filtered[filtered['account_name'].str.contains(search, case=False, na=False)]

    st.dataframe(
        filtered.sort_values('amount', ascending=False),
        use_container_width=True,
        hide_index=True,
        column_config={
            "snapshot_date": st.column_config.TextColumn("기준일"),
            "balance_type": st.column_config.TextColumn("구분"),
            "asset_type": st.column_config.TextColumn("분류"),
            "account_name": st.column_config.TextColumn("자산명"),
            "amount": st.column_config.NumberColumn("금액", format="%d원"),
            "owner": st.column_config.TextColumn("소유자"),
        }
    )
render()
