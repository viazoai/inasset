import pyzipper
import pandas as pd
import io
import re
import os
import datetime

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../docs')

def detect_owner_from_filename(filename: str) -> str | None:
    """
    파일명에서 소유자를 추출합니다.

    패턴 예시:
      '조윤희님_2024-02-01~2025-02-01.zip'  → '윤희'  (ZIP: 님_ 패턴)
      '조형준님_2024-02-01~2025-02-01.zip'  → '형준'  (ZIP: 님_ 패턴)
      '2024-06-01~2025-06-01_내사랑.xlsx'   → '윤희'  (Excel: _내사랑 suffix)
      '2024-06-01~2025-06-01_나.xlsx'       → '형준'  (Excel: _나 suffix)
      '2024-06-01~2025-06-01.xlsx'          → '형준'  (Excel: 날짜만, suffix 없음)

    인식 불가 시 None 반환.
    """
    # ZIP: '님_' 패턴 (조윤희님_, 조형준님_)
    if '님_' in filename:
        full_name = filename.split('님_')[0]
        name = full_name[1:3] if len(full_name) > 1 else None
        if name in ('형준', '윤희'):
            return name

    # Excel: 날짜 범위 패턴 이후 suffix로 판별
    stem = re.sub(r'\.(xlsx?|zip)$', '', filename, flags=re.IGNORECASE)
    date_match = re.search(r'\d{4}-\d{2}-\d{2}~\d{4}-\d{2}-\d{2}(.*)', stem)
    if date_match:
        suffix = date_match.group(1).lstrip('_')
        if '내사랑' in suffix:
            return '윤희'
        return '형준'  # '_나', suffix 없음(날짜만) 모두 형준

    return None


def scan_docs_folder() -> list:
    """
    docs/ 폴더의 ZIP/Excel 파일을 스캔하여 처리 메타데이터 목록을 반환합니다.

    Returns:
        list of dict: {filename, owner, snapshot_date, start_date, mtime}
        mtime: 파일 수정시간 (datetime.datetime, UTC naive)
    """
    if not os.path.exists(DOCS_DIR):
        os.makedirs(DOCS_DIR, exist_ok=True)
        return []

    result = []
    for fname in os.listdir(DOCS_DIR):
        if not fname.lower().endswith(('.zip', '.xlsx', '.xls')):
            continue
        start_str, snapshot_str = extract_date_range(fname)
        if start_str is None:
            start_str = str(datetime.date.fromisoformat(snapshot_str) - datetime.timedelta(days=30))
        fpath = os.path.join(DOCS_DIR, fname)
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
        result.append({
            'filename': fname,
            'owner': detect_owner_from_filename(fname),
            'snapshot_date': snapshot_str,
            'start_date': start_str,
            'mtime': mtime,
        })
    return result


def extract_date_range(filename: str) -> tuple:
    """
    파일명에서 (start_date, end_date) 문자열 쌍을 추출합니다.

    패턴 예시:
      '조윤희님_2024-02-01~2025-02-01.zip'  → ('2024-02-01', '2025-02-01')
      '2024-02-01~2025-02-01_나.xlsx'       → ('2024-02-01', '2025-02-01')

    패턴이 없으면 (None, 오늘 날짜) 반환.
    """
    match = re.search(r'(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})', filename)
    if match:
        return match.group(1), match.group(2)
    today = datetime.date.today().strftime('%Y-%m-%d')
    return None, today


def extract_snapshot_date(filename: str) -> str:
    """파일명에서 기준 날짜(end_date)를 추출합니다. 패턴 없으면 오늘 날짜 반환."""
    _, end_date = extract_date_range(filename)
    return end_date


def process_uploaded_excel(uploaded_file, start_date=None, end_date=None):
    """
    Excel 파일 직접 업로드 처리 (ZIP 없이).
    뱅크샐러드 Excel 구조(Sheet 0: 자산, Sheet 1: 거래내역)를 파싱합니다.

    Returns:
        tx_df, asset_df, error
    """
    try:
        file_content = uploaded_file.read()
        excel_data = pd.ExcelFile(io.BytesIO(file_content))
        return _parse_excel_sheets(excel_data, start_date, end_date)
    except Exception as e:
        return None, None, f"파일 처리 중 오류 발생: {str(e)}"


def process_uploaded_zip(uploaded_file, password, start_date=None, end_date=None):
    """
    업로드된 뱅샐 ZIP 파일을 분석하여 '가계부 내역'과 '자산 현황' DataFrame을 반환합니다.

    Returns:
        tx_df (pd.DataFrame): 가계부 지출/수입 내역
        asset_df (pd.DataFrame): 자산 잔액 현황
        error (str): 에러 메시지 (없으면 None)
    """
    try:
        with pyzipper.AESZipFile(uploaded_file) as zf:
            zf.setpassword(password.encode('utf-8'))

            target_files = [f for f in zf.namelist() if f.endswith(('.csv', '.xlsx'))]

            if not target_files:
                return None, None, "ZIP 파일 내에 엑셀/CSV 파일이 없습니다."

            with zf.open(target_files[0]) as f:
                excel_data = pd.ExcelFile(io.BytesIO(f.read()))
                return _parse_excel_sheets(excel_data, start_date, end_date)

    except RuntimeError:
        return None, None, "비밀번호가 틀렸거나 파일 형식이 잘못되었습니다."
    except Exception as e:
        return None, None, f"파일 처리 중 오류 발생: {str(e)}"


def _parse_excel_sheets(excel_data, start_date=None, end_date=None):
    """
    pd.ExcelFile 객체에서 자산(Sheet 0)과 거래내역(Sheet 1)을 파싱합니다.

    Returns:
        tx_df, asset_df, error
    """
    tx_df = None
    asset_df = None

    # Sheet 0: 자산
    try:
        raw_asset_df = pd.read_excel(excel_data, sheet_name=0)
        asset_df = _parse_asset_sheet(raw_asset_df)
    except Exception:
        pass

    # Sheet 1: 거래내역
    try:
        if len(excel_data.sheet_names) > 1:
            tx_df = pd.read_excel(excel_data, sheet_name=1)

            if '날짜' not in tx_df.columns:
                return None, asset_df, f"거래내역 시트에 '날짜' 컬럼이 없습니다. (컬럼: {list(tx_df.columns)})"

            tx_df['날짜'] = pd.to_datetime(tx_df['날짜'])

            if start_date and end_date:
                mask = (tx_df['날짜'].dt.date >= start_date) & (tx_df['날짜'].dt.date <= end_date)
                tx_df = tx_df.loc[mask].copy()
        else:
            return None, None, "엑셀 파일에 가계부 내역 시트(Sheet2)가 없습니다."
    except Exception as e:
        return None, None, f"가계부 내역 시트 처리 중 오류: {str(e)}"

    return tx_df, asset_df, None


def _parse_asset_sheet(df):
    """
    뱅크샐러드 자산 시트(좌:자산, 우:부채, 셀병합)를 표준 포맷으로 변환 (Sheet 0 전용)
    """
    try:
        # 1. 헤더 위치 찾기 ('3.재무현황' 섹션 탐색)
        start_row_idx = -1
        for i, row in df.iterrows():
            if str(row[1]).strip() == '3.재무현황':
                start_row_idx = i
                break

        if start_row_idx == -1:
            return None

        # 실제 데이터 헤더('항목', '상품명') 위치 찾기 (시작 행 이후 10줄 이내 검색)
        header_row_idx = -1
        for i in range(start_row_idx, start_row_idx + 10):
            row_values = [str(x).strip() for x in df.iloc[i].values]
            if '항목' in row_values and '상품명' in row_values:
                header_row_idx = i
                break

        if header_row_idx == -1:
            return None

        # 종료 행 찾기 ('총자산' 텍스트 위치 탐색)
        end_row_idx = len(df)
        for i in range(header_row_idx + 1, len(df)):
            row_str = " ".join([str(x) for x in df.iloc[i] if pd.notna(x)])
            if '총자산' in row_str:
                end_row_idx = i
                break

        # 2. 데이터 영역 슬라이싱 (헤더 다음 행부터 끝까지)
        data_df = df.iloc[header_row_idx + 1 : end_row_idx].copy()

        if data_df.empty:
            return None

        # 헤더 행에서 컬럼명 감지
        header_row = df.iloc[header_row_idx]

        # 자산과 부채를 구분하기 위해 중간점 찾기
        item_positions = []
        for i, val in enumerate(header_row):
            if str(val).strip() == '항목':
                item_positions.append(i)

        if len(item_positions) >= 2:
            left_end = item_positions[1]
            right_start = item_positions[1]
        elif len(item_positions) == 1:
            left_end = len(header_row) // 2
            right_start = left_end
        else:
            left_end = min(4, len(data_df.columns))
            right_start = max(5, len(data_df.columns) // 2)

        # [좌측] 자산 데이터 추출
        assets = data_df.iloc[:, :left_end].copy()
        assets.columns = [str(header_row.iloc[i]).strip() if i < len(header_row) else f'col_{i}'
                         for i in range(len(assets.columns))]

        column_mapping = {}
        for col in assets.columns:
            col_lower = col.lower()
            if '항목' in col_lower:
                column_mapping[col] = 'asset_type'
            elif '상품명' in col_lower or '계좌' in col_lower:
                column_mapping[col] = 'account_name'
            elif '금액' in col_lower or '잔액' in col_lower:
                column_mapping[col] = 'amount'

        assets = assets.rename(columns=column_mapping)

        required_cols = ['asset_type', 'account_name', 'amount']
        existing_cols = [col for col in required_cols if col in assets.columns]

        if not existing_cols:
            return None

        assets = assets[existing_cols].copy()
        assets['balance_type'] = '자산'

        if 'asset_type' in assets.columns:
            assets['asset_type'] = assets['asset_type'].ffill()

        if 'amount' in assets.columns:
            assets['amount'] = pd.to_numeric(assets['amount'], errors='coerce').fillna(0).astype(int)

        if 'account_name' in assets.columns and 'amount' in assets.columns:
            assets = assets[
                (assets['account_name'].notna() & (assets['account_name'].astype(str).str.strip() != '')) |
                (assets['amount'] != 0)
            ].copy()

        if 'account_name' in assets.columns and 'asset_type' in assets.columns:
            assets['account_name'] = assets['account_name'].fillna(assets['asset_type'])

        # [우측] 부채 데이터 추출
        if right_start < len(data_df.columns):
            liabilities = data_df.iloc[:, right_start:].copy()
            liabilities.columns = [str(header_row.iloc[i]).strip() if i < len(header_row) else f'col_{i}'
                                  for i in range(right_start, min(right_start + len(liabilities.columns), len(header_row)))]

            column_mapping = {}
            for col in liabilities.columns:
                col_lower = col.lower()
                if '항목' in col_lower:
                    column_mapping[col] = 'asset_type'
                elif '상품명' in col_lower or '계좌' in col_lower:
                    column_mapping[col] = 'account_name'
                elif '금액' in col_lower or '잔액' in col_lower:
                    column_mapping[col] = 'amount'

            liabilities = liabilities.rename(columns=column_mapping)

            existing_cols = [col for col in ['asset_type', 'account_name', 'amount']
                           if col in liabilities.columns]

            if existing_cols:
                liabilities = liabilities[existing_cols].copy()
                liabilities['balance_type'] = '부채'

                if 'asset_type' in liabilities.columns:
                    liabilities['asset_type'] = liabilities['asset_type'].ffill()

                if 'amount' in liabilities.columns:
                    liabilities['amount'] = pd.to_numeric(liabilities['amount'], errors='coerce').fillna(0).astype(int)

                if 'account_name' in liabilities.columns and 'amount' in liabilities.columns:
                    liabilities = liabilities.dropna(subset=['account_name'])
                    liabilities = liabilities[
                        (liabilities['account_name'].astype(str).str.strip() != '') &
                        (liabilities['amount'] != 0)
                    ]
            else:
                liabilities = pd.DataFrame()
        else:
            liabilities = pd.DataFrame()

        # 합치기
        combined_df = pd.concat([assets, liabilities], ignore_index=True) if not liabilities.empty else assets.copy()

        if combined_df.empty:
            return None

        return combined_df[['balance_type', 'asset_type', 'account_name', 'amount']]

    except Exception:
        return None


CATEGORY_REMAP = {"개회": "예비비"}

def apply_direct_category(df):
    """
    GPT 매핑 없이 category_1을 refined_category_1에 직접 복사.
    CATEGORY_REMAP에 해당하는 항목만 재분류.
    뱅크샐러드 원본 분류가 이미 충분히 정제된 경우(이메일 자동 수신)에 사용.
    """
    df = df.copy()
    # 한글 컬럼명 → 영문 컬럼명으로 먼저 변환된 경우 대비하여 두 가지 대응
    if '대분류' in df.columns:
        df['refined_category_1'] = df['대분류'].replace(CATEGORY_REMAP)
    elif 'category_1' in df.columns:
        df['refined_category_1'] = df['category_1'].replace(CATEGORY_REMAP)
    return df


def format_df_for_display(df):
    display_df = df.copy()

    if '날짜' in display_df.columns:
        display_df['날짜'] = pd.to_datetime(display_df['날짜']).dt.strftime('%Y-%m-%d')

    if '시간' in display_df.columns:
        display_df['시간'] = pd.to_datetime(display_df['시간'], format='%H:%M:%S', errors='coerce').dt.strftime('%H:%M:%S')
        display_df['시간'] = display_df['시간'].fillna('-')

    return display_df
