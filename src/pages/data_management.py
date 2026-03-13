import streamlit as st
import pandas as pd
import datetime
import calendar
import os
import time

from openai import OpenAI

from utils.db_handler import (
    save_transactions, save_asset_snapshot, clear_all_data,
    sync_categories_from_transactions,
    has_transactions_in_range, get_few_shot_examples,
    get_transactions_for_reclassification, update_refined_categories,
    get_existing_refined_mappings,
)
from utils.file_handler import (
    process_uploaded_zip, process_uploaded_excel,
    extract_snapshot_date, extract_date_range, detect_owner_from_filename,
)
from utils.ai_agent import map_categories, STANDARD_CATEGORIES, INCOME_CATEGORIES

_OWNER_PASSWORDS = {
    '형준': os.getenv('ZIP_PASSWORD_HYEONGJUN', ''),
    '윤희': os.getenv('ZIP_PASSWORD_YUNHEE', ''),
}


# GPT-4o 가격 기준 (2025)
_INPUT_PRICE_PER_TOKEN  = 2.50  / 1_000_000   # USD
_OUTPUT_PRICE_PER_TOKEN = 10.00 / 1_000_000   # USD
_KRW_RATE = 1_350                              # 1 USD = 1,350 KRW


def _add_usage(a: dict, b: dict) -> dict:
    """두 usage dict를 합산합니다."""
    return {
        'model': b.get('model') or a.get('model', 'gpt-4o'),
        'input_tokens':  a.get('input_tokens', 0)  + b.get('input_tokens', 0),
        'output_tokens': a.get('output_tokens', 0) + b.get('output_tokens', 0),
    }


def _show_usage(usage: dict):
    """GPT 사용 모델·토큰·추정 비용을 한 줄로 표시합니다."""
    if not usage or usage.get('input_tokens', 0) + usage.get('output_tokens', 0) == 0:
        return
    inp  = usage.get('input_tokens', 0)
    out  = usage.get('output_tokens', 0)
    usd  = inp * _INPUT_PRICE_PER_TOKEN + out * _OUTPUT_PRICE_PER_TOKEN
    krw  = usd * _KRW_RATE
    st.caption(
        f"🤖 모델: **{usage.get('model', 'gpt-4o')}** | "
        f"입력 {inp:,} + 출력 {out:,} = {inp + out:,} 토큰 | "
        f"추정 비용 **${usd:.4f}** (약 ₩{krw:.1f})"
    )


def _two_months_before(d: datetime.date) -> datetime.date:
    """end_date 기준 2개월 전 같은 날을 반환합니다. (월말 초과 시 해당 월 말일로 보정)"""
    month = d.month - 2
    year = d.year
    if month <= 0:
        month += 12
        year -= 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _resolve_date_range(
    owner: str, file_start: datetime.date, file_end: datetime.date
) -> tuple:
    """
    DB 데이터 유무에 따라 실제 적용할 처리 기간을 결정합니다.
      - 해당 기간에 데이터 없음 → 파일 전체 기간 (file_start ~ file_end)
      - 겹치는 데이터 있음     → 최근 2개월 (file_end - 2개월 ~ file_end)
    """
    if has_transactions_in_range(owner, str(file_start), str(file_end)):
        return _two_months_before(file_end), file_end
    return file_start, file_end


def _build_item(filename: str, file_obj=None) -> dict:
    """파일명에서 처리 메타데이터 추출"""
    start_str, snapshot_str = extract_date_range(filename)
    if start_str is None:
        start_str = str(
            datetime.date.fromisoformat(snapshot_str) - datetime.timedelta(days=30)
        )
    return {
        'file': file_obj,
        'filename': filename,
        'owner': detect_owner_from_filename(filename),
        'snapshot_date': snapshot_str,
        'start_date': start_str,
    }


def _process_single(file_obj, filename: str, owner: str, start_date, end_date):
    """단일 파일 파싱 및 저장. (tx_count, asset_count, error) 반환"""
    password = _OWNER_PASSWORDS.get(owner, '')
    if filename.lower().endswith('.zip'):
        tx_df, asset_df, error = process_uploaded_zip(
            file_obj, password, start_date=start_date, end_date=end_date
        )
    else:
        tx_df, asset_df, error = process_uploaded_excel(
            file_obj, start_date=start_date, end_date=end_date
        )

    if error:
        return 0, 0, error

    tx_count = 0
    if tx_df is not None and not tx_df.empty:
        tx_count = save_transactions(tx_df, owner=owner, filename=filename)

    asset_count = 0
    if asset_df is not None and not asset_df.empty:
        asset_count = save_asset_snapshot(
            asset_df, owner=owner, snapshot_date=extract_snapshot_date(filename)
        )

    return tx_count, asset_count, None


def _show_file_table(items: list):
    """파일 목록 요약 테이블 렌더링 (snapshot_date 오름차순)"""
    rows = []
    for it in sorted(items, key=lambda x: x['snapshot_date']):
        row = {
            '파일명': it['filename'],
            '소유자': it['owner'] or '⚠️ 미감지',
            '기준일': it['snapshot_date'],
            '처리 기간': f"{it.get('resolved_start', it['start_date'])} ~ {it['snapshot_date']}",
            '상태': '🔄 업데이트됨' if it.get('is_updated') else '🆕 신규',
        }
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)




def _show_results(results: list):
    """배치 처리 결과를 렌더링합니다."""
    success_count = sum(1 for r in results if '✅' in r['처리결과'])
    st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
    st.success(f"✅ {success_count} / {len(results)}개 파일 처리 완료")


def _parse_batch_only(items: list) -> list:
    """파일을 파싱만 하고 저장은 하지 않습니다. parsed_data 리스트를 반환합니다."""
    parsed_data = []
    for item in sorted(items, key=lambda x: x['snapshot_date']):
        filename = item['filename']
        owner = item['owner']
        file_start = datetime.date.fromisoformat(item['start_date'])
        file_end = datetime.date.fromisoformat(item['snapshot_date'])
        actual_start, actual_end = _resolve_date_range(owner, file_start, file_end)

        password = _OWNER_PASSWORDS.get(owner, '')
        file_obj = item['file']

        if filename.lower().endswith('.zip'):
            tx_df, asset_df, error = process_uploaded_zip(
                file_obj, password, start_date=actual_start, end_date=actual_end
            )
        else:
            tx_df, asset_df, error = process_uploaded_excel(
                file_obj, start_date=actual_start, end_date=actual_end
            )

        parsed_data.append({
            'tx_df': tx_df,
            'asset_df': asset_df,
            'item': {**item, 'start_date': str(actual_start), 'snapshot_date': str(actual_end)},
            'error': error,
        })
    return parsed_data


def _build_mapping_df(client, parsed_data: list) -> tuple:
    """
    파싱된 거래 데이터에서 개별 거래 행을 추출하고 GPT 매핑을 실행합니다.
    GPT 호출은 고유 (description, category_1) 쌍으로만 수행(비용 절감), 결과는 개별 행으로 확장합니다.
    DB에 기존 refined_category_1이 있는 항목은 GPT 없이 기존값을 신규분류 기본값으로 사용합니다.

    Returns:
        (mapping_df, usage_dict)
        mapping_df columns: date, description, amount, memo, category_1, tx_type, current_refined, refined_category_1
    """
    _empty_usage = {'model': 'gpt-4o', 'input_tokens': 0, 'output_tokens': 0}
    _KEEP = {'날짜': 'date', '내용': 'description', '금액': 'amount', '메모': 'memo', '대분류': 'category_1'}

    all_rows = []
    for pd_item in parsed_data:
        tx_df = pd_item.get('tx_df')
        if tx_df is None or tx_df.empty:
            continue
        if '내용' not in tx_df.columns or '대분류' not in tx_df.columns:
            continue
        available = {k: v for k, v in _KEEP.items() if k in tx_df.columns}
        rows = tx_df[list(available.keys())].rename(columns=available).copy()
        rows['tx_type'] = tx_df['타입'].values if '타입' in tx_df.columns else '지출'
        all_rows.append(rows)

    if not all_rows:
        return pd.DataFrame(columns=['date', 'description', 'amount', 'memo', 'category_1', 'tx_type', 'current_refined', 'refined_category_1']), _empty_usage

    all_df = pd.concat(all_rows, ignore_index=True)
    all_df['date'] = pd.to_datetime(all_df['date']).dt.strftime('%Y-%m-%d')
    all_df = all_df.dropna(subset=['description', 'category_1']).reset_index(drop=True)

    # 고유 (description, category_1) 쌍으로 GPT 비용 절감
    unique_pairs = all_df[['description', 'category_1', 'tx_type']].drop_duplicates().reset_index(drop=True)

    # DB 기존 분류 조회 — (date, description, category_1) 기준으로 기존 수동 분류값 복원
    all_pair_tuples = list(zip(all_df['date'], all_df['description'], all_df['category_1']))
    existing_map = get_existing_refined_mappings(all_pair_tuples)
    # GPT 스킵 판단용: (description, category_1)에 기존 분류가 하나라도 있으면 재사용
    existing_by_desc_cat = {}
    for (date, desc, cat), refined in existing_map.items():
        if (desc, cat) not in existing_by_desc_cat:
            existing_by_desc_cat[(desc, cat)] = refined
    unique_pairs['_existing'] = unique_pairs.apply(
        lambda r: existing_by_desc_cat.get((r['description'], r['category_1']), ''), axis=1
    )
    already_pairs = unique_pairs[unique_pairs['_existing'] != ''].copy()
    to_classify   = unique_pairs[unique_pairs['_existing'] == ''].copy()

    expense = to_classify[to_classify['tx_type'] == '지출'][['description', 'category_1']].copy()
    income  = to_classify[to_classify['tx_type'] == '수입'][['description', 'category_1']].copy()
    total_usage = _empty_usage.copy()

    # 지출 GPT 매핑 (STANDARD_CATEGORIES)
    if client is not None and not expense.empty:
        try:
            expense_mapped, u = map_categories(client, expense, get_few_shot_examples(tx_type='지출'), STANDARD_CATEGORIES)
            total_usage = _add_usage(total_usage, u)
        except Exception:
            expense_mapped = expense.copy()
            expense_mapped['refined_category_1'] = expense_mapped['category_1']
    else:
        expense_mapped = expense.copy()
        expense_mapped['refined_category_1'] = expense_mapped['category_1']
    expense_mapped['refined_category_1'] = expense_mapped.apply(
        lambda r: r['category_1'] if r['refined_category_1'] == '미분류' else r['refined_category_1'], axis=1
    )

    # 수입 GPT 매핑 (INCOME_CATEGORIES)
    if client is not None and not income.empty:
        try:
            income_mapped, u = map_categories(client, income, get_few_shot_examples(tx_type='수입'), INCOME_CATEGORIES)
            total_usage = _add_usage(total_usage, u)
        except Exception:
            income_mapped = income.copy()
            income_mapped['refined_category_1'] = income_mapped['category_1']
    else:
        income_mapped = income.copy()
        income_mapped['refined_category_1'] = income_mapped['category_1']
    income_mapped['refined_category_1'] = income_mapped.apply(
        lambda r: r['category_1'] if r['refined_category_1'] == '미분류' else r['refined_category_1'], axis=1
    )

    # (description, category_1) → refined_category_1 매핑 딕셔너리 구성
    refined_map = {}
    for _, row in already_pairs.iterrows():
        refined_map[(row['description'], row['category_1'])] = row['_existing']
    for _, row in pd.concat([expense_mapped, income_mapped], ignore_index=True).iterrows():
        refined_map[(row['description'], row['category_1'])] = row['refined_category_1']

    # 개별 행으로 확장
    all_df['current_refined'] = all_df.apply(
        lambda r: existing_map.get((r['date'], r['description'], r['category_1']), ''), axis=1
    )
    all_df['refined_category_1'] = all_df.apply(
        lambda r: refined_map.get((r['description'], r['category_1']), r['category_1']), axis=1
    )

    return all_df, total_usage


def _apply_mapping_and_save(parsed_data: list, mapping_df: pd.DataFrame) -> list:
    """카테고리 매핑을 적용하고 DB에 저장합니다."""
    mapping_dict = (
        {(row['description'], row['category_1']): row['refined_category_1']
         for _, row in mapping_df.iterrows()}
        if not mapping_df.empty else {}
    )

    results = []
    for pd_item in parsed_data:
        item = pd_item['item']
        tx_df = pd_item['tx_df']
        asset_df = pd_item['asset_df']
        error = pd_item['error']
        filename = item['filename']
        owner = item['owner']
        period_str = f"{item['start_date']} ~ {item['snapshot_date']}"

        if error:
            results.append({'파일명': filename, '소유자': owner, '처리기간': period_str, '처리결과': f'❌ {error}'})
            continue

        tx_count = 0
        if tx_df is not None and not tx_df.empty:
            tx_df = tx_df.copy()
            if '내용' in tx_df.columns and '대분류' in tx_df.columns:
                tx_df['refined_category_1'] = tx_df.apply(
                    lambda r: mapping_dict.get((r['내용'], r['대분류']), r['대분류']), axis=1
                )
            tx_count = save_transactions(tx_df, owner=owner, filename=filename)

        asset_count = 0
        if asset_df is not None and not asset_df.empty:
            asset_count = save_asset_snapshot(
                asset_df, owner=owner, snapshot_date=extract_snapshot_date(filename)
            )

        results.append({
            '파일명': filename, '소유자': owner, '처리기간': period_str,
            '처리결과': f'✅ 거래 {tx_count}건  자산 {asset_count}건',
        })

    sync_categories_from_transactions()
    return results


def _build_recat_mapping_df(client, tx_df: pd.DataFrame) -> tuple:
    """
    DB에서 조회한 개별 거래 행을 GPT로 재분류합니다.
    GPT 호출은 고유 (description, category_1) 쌍으로만 수행(비용 절감), 결과는 개별 행으로 확장합니다.

    Returns:
        (result_df, usage_dict)
        result_df columns: id, date, description, amount, memo, category_1, tx_type, current_refined, refined_category_1
    """
    _empty_usage = {'model': 'gpt-4o', 'input_tokens': 0, 'output_tokens': 0}

    if tx_df.empty:
        return tx_df.copy(), _empty_usage

    unique_pairs = tx_df[['description', 'category_1', 'tx_type']].drop_duplicates().reset_index(drop=True)

    expense_pairs = unique_pairs[unique_pairs['tx_type'] == '지출'][['description', 'category_1']].copy()
    income_pairs  = unique_pairs[unique_pairs['tx_type'] == '수입'][['description', 'category_1']].copy()
    total_usage = _empty_usage.copy()

    # 지출 GPT 매핑 (STANDARD_CATEGORIES)
    if client is not None and not expense_pairs.empty:
        try:
            expense_mapped, u = map_categories(client, expense_pairs, get_few_shot_examples(tx_type='지출'), STANDARD_CATEGORIES)
            total_usage = _add_usage(total_usage, u)
        except Exception:
            expense_mapped = expense_pairs.copy()
            expense_mapped['refined_category_1'] = expense_mapped['category_1']
    else:
        expense_mapped = expense_pairs.copy()
        expense_mapped['refined_category_1'] = expense_mapped['category_1']
    expense_mapped['refined_category_1'] = expense_mapped.apply(
        lambda r: r['category_1'] if r['refined_category_1'] == '미분류' else r['refined_category_1'], axis=1
    )

    # 수입 GPT 매핑 (INCOME_CATEGORIES)
    if client is not None and not income_pairs.empty:
        try:
            income_mapped, u = map_categories(client, income_pairs, get_few_shot_examples(tx_type='수입'), INCOME_CATEGORIES)
            total_usage = _add_usage(total_usage, u)
        except Exception:
            income_mapped = income_pairs.copy()
            income_mapped['refined_category_1'] = income_mapped['category_1']
    else:
        income_mapped = income_pairs.copy()
        income_mapped['refined_category_1'] = income_mapped['category_1']
    income_mapped['refined_category_1'] = income_mapped.apply(
        lambda r: r['category_1'] if r['refined_category_1'] == '미분류' else r['refined_category_1'], axis=1
    )

    # (description, category_1) → refined_category_1 매핑 딕셔너리 구성
    refined_map = {}
    for _, row in pd.concat([expense_mapped, income_mapped], ignore_index=True).iterrows():
        refined_map[(row['description'], row['category_1'])] = row['refined_category_1']

    # 개별 행으로 확장
    result = tx_df.copy()
    result['refined_category_1'] = result.apply(
        lambda r: refined_map.get((r['description'], r['category_1']), r['category_1']), axis=1
    )

    return result, total_usage


def render():
    if st.session_state.get('role') != 'admin':
        st.error("관리자만 접근할 수 있습니다.")
        st.stop()

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
    st.markdown('<div class="page-header">데이터 관리 (ETL/EDA)</div>', unsafe_allow_html=True)
    st.markdown('<div class="page-subtitle">가계부 데이터 업로드 및 카테고리 관리</div>', unsafe_allow_html=True)

    api_key = os.getenv("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key) if api_key else None

    def _make_editor(df, cats, key):
        display = df.copy()
        display['변경여부'] = display.apply(
            lambda r: '✏️' if r['refined_category_1'] != (r['current_refined'] if r.get('current_refined') else r['category_1']) else '',
            axis=1
        )
        display = display.sort_values('변경여부', ascending=False).reset_index(drop=True)
        cols_show = [c for c in ['변경여부', 'date', 'description', 'amount', 'memo', 'category_1', 'current_refined', 'refined_category_1'] if c in display.columns]
        return st.data_editor(
            display[cols_show],
            column_config={
                '변경여부': st.column_config.TextColumn('변경', disabled=True, width='small'),
                'date': st.column_config.TextColumn('일자', disabled=True),
                'description': st.column_config.TextColumn('내용', disabled=True),
                'amount': st.column_config.NumberColumn('내역', disabled=True),
                'memo': st.column_config.TextColumn('메모', disabled=True),
                'category_1': st.column_config.TextColumn('원본분류', disabled=True),
                'current_refined': st.column_config.TextColumn('기존분류', disabled=True),
                'refined_category_1': st.column_config.SelectboxColumn('신규분류', options=list(cats), required=True),
            },
            hide_index=True, use_container_width=True, key=key,
        )

    # ── Section 1: 카테고리 정규화 (기존 데이터 기반) ────────────
    st.subheader("카테고리 정규화 (기존 데이터 기반)")
    st.caption("저장된 거래내역의 카테고리를 GPT로 재분류합니다. 기간을 선택하면 해당 기간 개별 거래를 분석합니다.")

    recat_results = st.session_state.get('recat_results')
    recat_review = st.session_state.get('recat_review')

    if recat_results is not None:
        # State C: 완료
        col_m1, col_m2 = st.columns(2)
        col_m1.metric("업데이트된 거래 건수", f"{recat_results['updated_rows']:,}건")
        col_m2.metric("분류 변경 건수", f"{recat_results['changed_items']}건 / {recat_results['total_items']}건")
        _show_usage(recat_results.get('usage', {}))
        if st.button("↩ 다시 실행", key="recat_reset_btn", use_container_width=True):
            st.session_state.pop('recat_results', None)
            st.rerun()

    elif recat_review is not None:
        # State B: 검수
        mapping_df = recat_review['mapping_df']
        start_date_str = recat_review['start_date']
        end_date_str = recat_review['end_date']

        has_type = 'tx_type' in mapping_df.columns
        exp_df = mapping_df[mapping_df['tx_type'] == '지출'] if has_type else mapping_df
        inc_df = mapping_df[mapping_df['tx_type'] == '수입'] if has_type else pd.DataFrame()

        changed_count = mapping_df.apply(
            lambda r: r['refined_category_1'] != (r['current_refined'] if r.get('current_refined') else r['category_1']), axis=1
        ).sum()
        st.markdown(
            f"**카테고리 재분류 검수** — `{start_date_str}` ~ `{end_date_str}` 기간  \n"
            f"지출 **{len(exp_df)}건** · 수입 **{len(inc_df)}건** / 분류 변경 **{changed_count}건**  \n"
            f"'신규분류' 열을 직접 수정한 후 저장하세요."
        )
        _show_usage(recat_review.get('usage', {}))

        show_tabs = not exp_df.empty and not inc_df.empty
        if show_tabs:
            tab_exp, tab_inc = st.tabs([f"💸 지출 ({len(exp_df)})", f"💰 수입 ({len(inc_df)})"])
            with tab_exp:
                edited_exp = _make_editor(exp_df, STANDARD_CATEGORIES, "recat_rev_exp")
            with tab_inc:
                edited_inc = _make_editor(inc_df, INCOME_CATEGORIES, "recat_rev_inc")
        elif not exp_df.empty:
            edited_exp = _make_editor(exp_df, STANDARD_CATEGORIES, "recat_rev_exp")
            edited_inc = pd.DataFrame()
        else:
            edited_exp = pd.DataFrame()
            edited_inc = _make_editor(inc_df, INCOME_CATEGORIES, "recat_rev_inc")

        col1, col2 = st.columns([3, 1])
        with col1:
            if st.button("검수 완료 & DB 저장", use_container_width=True, key="recat_save_btn"):
                combined_edited = pd.concat([edited_exp, edited_inc], ignore_index=True)
                mapping_dict = {
                    (row['description'], row['category_1']): row['refined_category_1']
                    for _, row in combined_edited.iterrows()
                }
                updated_rows = update_refined_categories(mapping_dict, start_date_str, end_date_str)
                changed_items = int(combined_edited.apply(
                    lambda r: r['refined_category_1'] != (r['current_refined'] if r.get('current_refined') else r['category_1']), axis=1
                ).sum())
                st.session_state['recat_results'] = {
                    'updated_rows': updated_rows,
                    'changed_items': changed_items,
                    'total_items': len(combined_edited),
                    'usage': recat_review.get('usage', {}),
                }
                st.session_state.pop('recat_review', None)
                st.rerun()
        with col2:
            if st.button("취소", use_container_width=True, key="recat_cancel_btn"):
                st.session_state.pop('recat_review', None)
                st.rerun()

    else:
        # State A: 날짜 범위 선택
        with st.container(border=True):
            today = datetime.date.today()
            default_start = _two_months_before(today)

            col1, col2 = st.columns(2)
            with col1:
                start_date = st.date_input("시작일", value=default_start, key="recat_start_date")
            with col2:
                end_date = st.date_input("종료일", value=today, key="recat_end_date")

            if not client:
                st.warning("⚠️ OPENAI_API_KEY가 설정되지 않아 GPT 분류 없이 원본 카테고리가 표시됩니다.")

            if st.button("카테고리 재분류 (GPT 기반)", use_container_width=True, key="recat_start_btn"):
                if start_date > end_date:
                    st.error("시작일이 종료일보다 늦을 수 없습니다.")
                else:
                    with st.spinner("DB에서 거래 내역을 조회하는 중..."):
                        tx_df = get_transactions_for_reclassification(str(start_date), str(end_date))

                    if tx_df.empty:
                        st.warning(f"해당 기간 (`{start_date}` ~ `{end_date}`)에 거래 내역이 없습니다.")
                    else:
                        with st.spinner(f"{len(tx_df)}건 거래를 GPT로 분류 중..."):
                            mapping_df, usage = _build_recat_mapping_df(client, tx_df)
                        st.session_state['recat_review'] = {
                            'mapping_df': mapping_df,
                            'start_date': str(start_date),
                            'end_date': str(end_date),
                            'usage': usage,
                        }
                        st.rerun()

    st.divider()

    # ── Section 2: 수동 업데이트 (파일 기반) ─────────────────────
    st.subheader("수동 업데이트 (파일 기반)")
    st.caption("뱅크샐러드 ZIP·Excel 파일을 업로드하면 GPT가 카테고리를 자동 분류합니다. 검수 후 저장하세요.")
    st.caption("📅 처리 기간: 기존 데이터 없으면 파일 전체 기간 저장 / 있으면 종료일 기준 최근 2개월만 갱신")

    upload_results = st.session_state.get('upload_results')
    upload_review = st.session_state.get('upload_review')

    if upload_results is not None:
        # State 3: 저장 완료 결과 표시
        _show_results(upload_results)
        if st.button("↩ 새 업로드로 이동", key="reset_upload_btn", use_container_width=True):
            st.session_state.pop('upload_results', None)
            st.rerun()

    elif upload_review is not None:
        # State 2: 카테고리 검수
        mapping_df = upload_review['mapping_df']
        has_type = 'tx_type' in mapping_df.columns
        exp_df = mapping_df[mapping_df['tx_type'] == '지출'] if has_type else mapping_df
        inc_df = mapping_df[mapping_df['tx_type'] == '수입'] if has_type else pd.DataFrame()

        items = [pd_item['item'] for pd_item in upload_review['parsed_data']]
        _show_file_table(items)
        changed_count = mapping_df.apply(
            lambda r: r['refined_category_1'] != (r['current_refined'] if r.get('current_refined') else r['category_1']), axis=1
        ).sum()
        st.markdown(
            f"**카테고리 검수** — {len(items)}개 파일  \n"
            f"지출 **{len(exp_df)}건** · 수입 **{len(inc_df)}건** / 분류 변경 **{changed_count}건**  \n"
            f"'신규분류' 열을 직접 수정한 후 저장하세요."
        )
        _show_usage(upload_review.get('usage', {}))

        show_tabs = not exp_df.empty and not inc_df.empty
        if show_tabs:
            tab_exp, tab_inc = st.tabs([f"💸 지출 ({len(exp_df)})", f"💰 수입 ({len(inc_df)})"])
            with tab_exp:
                edited_exp = _make_editor(exp_df, STANDARD_CATEGORIES, "upload_rev_exp")
            with tab_inc:
                edited_inc = _make_editor(inc_df, INCOME_CATEGORIES, "upload_rev_inc")
        elif not exp_df.empty:
            edited_exp = _make_editor(exp_df, STANDARD_CATEGORIES, "upload_rev_exp")
            edited_inc = pd.DataFrame()
        else:
            edited_exp = pd.DataFrame()
            edited_inc = _make_editor(inc_df, INCOME_CATEGORIES, "upload_rev_inc")

        col1, col2 = st.columns([3, 1])
        with col1:
            if st.button("검수 완료 & DB 저장", use_container_width=True):
                combined_edited = pd.concat([edited_exp, edited_inc], ignore_index=True)
                results = _apply_mapping_and_save(upload_review['parsed_data'], combined_edited)
                st.session_state['upload_results'] = results
                st.session_state.pop('upload_review', None)
                st.rerun()
        with col2:
            if st.button("취소", use_container_width=True):
                st.session_state.pop('upload_review', None)
                st.rerun()

    else:
        # State 1: 파일 업로드
        with st.container(border=True):
            uploaded_files = st.file_uploader(
                "뱅크샐러드 ZIP 또는 Excel 파일 (여러 파일 동시 선택 가능)",
                type=["zip", "xlsx", "xls"],
                accept_multiple_files=True,
            )

        if uploaded_files:
            items = [_build_item(f.name, file_obj=f) for f in uploaded_files]
            for item in items:
                if item['owner']:
                    _fs = datetime.date.fromisoformat(item['start_date'])
                    _fe = datetime.date.fromisoformat(item['snapshot_date'])
                    item['resolved_start'] = str(_resolve_date_range(item['owner'], _fs, _fe)[0])
            _show_file_table(items)

            undetected = [it['filename'] for it in items if not it['owner']]
            if undetected:
                st.warning(f"⚠️ 소유자 미감지 파일은 처리에서 제외됩니다: {', '.join(undetected)}")

            processable = [it for it in items if it['owner']]
            if processable:
                if st.button("카테고리 재분류 (GPT 기반)", use_container_width=True):
                    with st.spinner("파일 분석 중..."):
                        parsed_data = _parse_batch_only(processable)
                    with st.spinner("GPT가 카테고리를 분류하고 있습니다..."):
                        mapping_df, usage = _build_mapping_df(client, parsed_data)
                    if mapping_df.empty:
                        results = _apply_mapping_and_save(parsed_data, mapping_df)
                        st.session_state['upload_results'] = results
                    else:
                        st.session_state['upload_review'] = {
                            'parsed_data': parsed_data,
                            'mapping_df': mapping_df,
                            'usage': usage,
                        }
                    st.rerun()

    st.divider()

    # ── Section 4: Admin ─────────────────────────────────
    if st.session_state.get('role') == 'admin':
        st.markdown("""
        <style>
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"] {
            background-color: #dc3545 !important;
            border-color: #dc3545 !important;
            color: white !important;
        }
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"]:hover {
            background-color: #c82333 !important;
            border-color: #bd2130 !important;
        }
        </style>
        """, unsafe_allow_html=True)

        @st.dialog("데이터 초기화 확인")
        def open_delete_modal():
            st.write("이 작업은 되돌릴 수 없으며, 저장된 모든 가계부 내역과 자산 정보가 영구적으로 삭제됩니다. (테이블 구조는 유지)")
            col1, col2 = st.columns([1, 1])

            with col1:
                if st.button("네, 초기화합니다", type="primary", use_container_width=True):
                    try:
                        clear_all_data()
                        for _k in ['upload_results', '_upload_filenames',
                                   'recat_results', 'recat_review']:
                            st.session_state.pop(_k, None)
                        st.success("초기화 완료! 잠시 후 새로고침 됩니다.")
                        time.sleep(1.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"오류: {e}")

            with col2:
                if st.button("아니오, 취소합니다", use_container_width=True):
                    st.rerun()

        if st.button("DB 데이터 초기화", type="primary", use_container_width=True):
            open_delete_modal()

    st.markdown("<div style='margin-bottom: 40px;'></div>", unsafe_allow_html=True)

render()
