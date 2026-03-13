import sqlite3
import pandas as pd
import os
import re

# DB 경로 및 파일명 변경 (InAsset의 아이덴티티 반영)
DB_PATH = "data/inasset_v1.db"

def _init_db():
    directory = os.path.dirname(DB_PATH)
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # 1. 뱅크샐러드 엑셀 구조를 반영한 신규 테이블 스키마
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,          -- 날짜는 필수 (YYYY-MM-DD)
                time TEXT,          -- 시간 (HH:MM)
                tx_type TEXT,       -- 타입 (수입/지출)
                category_1 TEXT,    -- 대분류
                category_2 TEXT,    -- 소분류
                refined_category_1 TEXT, -- 표준화 대분류 (분석용)
                refined_category_2 TEXT, -- 표준화 소분류 (분석용)
                description TEXT,   -- 내용
                amount INTEGER,     -- 금액
                currency TEXT,      -- 화폐
                source TEXT,        -- 결제수단
                memo TEXT,          -- 메모
                owner TEXT,         -- 소유자 (남편/아내/공동)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. 자산 스냅샷 테이블 (Asset Snapshots)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS asset_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT,
                balance_type TEXT,  -- 구분 (자산/부채)
                asset_type TEXT,    -- 항목 (예: 자유입출금 자산, 신탁 자산, 저축성 자산 등)
                account_name TEXT,  -- 상품명 (예: 신한 주거래 우대통장)
                amount INTEGER,     -- 금액
                owner TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 3. 목표 예산 테이블 (Budgets) — 카테고리 마스터 겸 예산 관리
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                category       TEXT PRIMARY KEY,  -- transactions.category_1과 동일
                monthly_amount INTEGER DEFAULT 0, -- 월 예산 (원 단위)
                is_fixed_cost  INTEGER DEFAULT 0, -- 1=고정, 0=변동
                sort_order     INTEGER DEFAULT 0  -- 표시 순서
            )
        """)

        # 마이그레이션: sort_order 컬럼이 없는 기존 DB에 추가
        try:
            cursor.execute("ALTER TABLE budgets ADD COLUMN sort_order INTEGER DEFAULT 0")
        except Exception:
            pass

        # 마이그레이션: category_rules 테이블 제거 (budgets로 통합)
        try:
            cursor.execute("DROP TABLE IF EXISTS category_rules")
        except Exception:
            pass

        # 4. 처리 완료 파일 이력 테이블
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_files (
                filename      TEXT PRIMARY KEY,
                owner         TEXT,
                snapshot_date TEXT,
                processed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status        TEXT DEFAULT 'new'
            )
        """)

        # 마이그레이션: status 컬럼이 없는 기존 DB에 추가 + 기존 행을 'updated'로 표시
        try:
            cursor.execute("ALTER TABLE processed_files ADD COLUMN status TEXT DEFAULT 'new'")
            cursor.execute("UPDATE processed_files SET status = 'updated' WHERE status IS NULL OR status = 'new'")
        except Exception:
            pass

def get_connection():
    """데이터베이스 연결 객체를 반환합니다."""
    # DB 파일이 존재하는지 체크 (선택 사항)
    if not os.path.exists(DB_PATH):
        # 만약 data 폴더가 없다면 생성
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        
    conn = sqlite3.connect(DB_PATH)
    return conn

def save_transactions(df, owner=None, filename="unknown.xlsx"):
    """
    지정된 기간과 소유자에 해당하는 기존 데이터를 삭제한 후, 새로운 데이터를 저장합니다.
    """
    _init_db()
    
    # 1. 한글 컬럼 -> 영문 컬럼 매핑 사전
    mapping = {
        '날짜': 'date',
        '시간': 'time',
        '타입': 'tx_type',
        '대분류': 'category_1',
        '소분류': 'category_2',
        '내용': 'description',
        '금액': 'amount',
        '화폐': 'currency',
        '결제수단': 'source',
        '메모': 'memo'
    }
    
    rename_df = df.rename(columns=mapping).copy()

    rename_df['owner'] = owner
    rename_df['source_file'] = filename
    rename_df['date'] = pd.to_datetime(rename_df['date']).dt.strftime('%Y-%m-%d')
    
    # 시간은 그대로 유지 (이미 HH:mm:ss 형식)
    if 'time' in rename_df.columns:
        rename_df['time'] = rename_df['time'].astype(str).str.strip()
    else:
        rename_df['time'] = '00:00:00'

    # 4. DB에 저장할 최종 컬럼 리스트 정의
    valid_columns = list(mapping.values()) + ['owner', 'refined_category_1']

    # 5. 데이터프레임에 해당 컬럼들이 있는지 확인 후 필터링
    # (혹시라도 매핑되지 않은 컬럼이 있을 경우를 대비해 존재하는 것만 추림)
    final_df = rename_df[[col for col in valid_columns if col in rename_df.columns]]

    # 사용자가 기간을 선택했든 전체를 선택했든, 실제 들어가는 데이터의 양끝을 찾습니다.
    min_date = final_df['date'].min()
    max_date = final_df['date'].max()

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # 2. "감지된 기간" 내의 "해당 소유자" 데이터만 삭제
        delete_query = "DELETE FROM transactions WHERE owner = ? AND date >= ? AND date <= ?"
        cursor.execute(delete_query, (owner, min_date, max_date))
        
        # 3. 새로운 데이터 삽입 (Bulk Insert)
        final_df.to_sql('transactions', conn, if_exists='append', index=False)
        conn.commit()

    return len(final_df)    


def get_analyzed_transactions():
    """
    transactions 테이블과 budgets를 조인하여
    고정비/변동비가 마킹된 데이터를 반환합니다.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path_fixed = os.path.join(base_dir, '../../data/inasset_v1.db')

    if not os.path.exists(db_path_fixed):
        return pd.DataFrame()

    with sqlite3.connect(db_path_fixed) as conn:
        query = '''
        SELECT
            T.date,
            T.time,
            T.tx_type,
            COALESCE(NULLIF(T.refined_category_1, ''), T.category_1) AS category_1,
            T.description,
            T.amount,
            T.memo,
            T.owner,
            T.source,
            CASE
                WHEN T.tx_type != '지출' THEN NULL
                WHEN B.is_fixed_cost = 1 THEN '고정 지출'
                ELSE '변동 지출'
            END AS expense_type
        FROM transactions T
        LEFT JOIN budgets B ON COALESCE(NULLIF(T.refined_category_1, ''), T.category_1) = B.category
        WHERE T.tx_type != '이체'
        ORDER BY T.date DESC, T.time DESC
        '''
        return pd.read_sql_query(query, conn)

def save_asset_snapshot(df, owner=None, snapshot_date=None):
    """
    추출된 자산 데이터를 asset_snapshots 테이블에 저장합니다.
    동일한 (snapshot_date, owner) 조합이 이미 존재하면 덮어씁니다.

    Args:
        df: 자산 데이터프레임 (owner 컬럼 포함 권장)
        owner: 소유자 (df에 owner가 없을 때만 사용)
        snapshot_date: 스냅샷 날짜 (YYYY-MM-DD 형식으로 저장)
    """
    _init_db()

    df = df.copy()

    if 'owner' not in df.columns or df['owner'].isna().all():
        df['owner'] = owner

    # snapshot_date를 YYYY-MM-DD 형식으로 정규화
    if snapshot_date:
        normalized_date = pd.to_datetime(snapshot_date).strftime('%Y-%m-%d')
        df['snapshot_date'] = normalized_date

    target_date = df['snapshot_date'].iloc[0]
    target_owner = df['owner'].iloc[0]

    with sqlite3.connect(DB_PATH) as conn:
        # 동일 날짜 + 소유자 기존 데이터 삭제 후 재삽입
        conn.execute(
            "DELETE FROM asset_snapshots WHERE snapshot_date = ? AND owner = ?",
            (target_date, target_owner)
        )
        df.to_sql('asset_snapshots', conn, if_exists='append', index=False)
        conn.commit()

    return len(df)


def clear_all_data():
    """
    transactions, asset_snapshots, processed_files 테이블의 모든 데이터를 삭제합니다.
    테이블 구조(스키마)는 유지됩니다.
    """
    if not os.path.exists(DB_PATH):
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM asset_snapshots")
        conn.execute("DELETE FROM processed_files")
        conn.commit()


def has_transactions_in_range(owner: str, start_date: str, end_date: str) -> bool:
    """지정 기간에 해당 소유자의 거래내역이 존재하는지 확인합니다."""
    if not os.path.exists(DB_PATH):
        return False
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "SELECT 1 FROM transactions WHERE owner = ? AND date >= ? AND date <= ? LIMIT 1",
            (owner, start_date, end_date),
        )
        return cursor.fetchone() is not None


def get_processed_filenames() -> dict:
    """처리 완료된 파일명 → processed_at(UTC 문자열) 매핑을 반환합니다. {'filename': 'YYYY-MM-DD HH:MM:SS'}"""
    if not os.path.exists(DB_PATH):
        return {}
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT filename, processed_at FROM processed_files")
        return {row[0]: row[1] for row in cursor.fetchall()}


def mark_file_processed(filename: str, owner: str, snapshot_date: str, status: str = 'new'):
    """파일 처리 완료를 기록합니다. status: 'new' | 'updated'"""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO processed_files (filename, owner, snapshot_date, status) VALUES (?, ?, ?, ?)",
            (filename, owner, snapshot_date, status),
        )
        conn.commit()


def get_latest_assets():
    """
    각 소유자별 가장 최근 날짜의 자산 스냅샷 정보를 가져옵니다.
    """
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. asset_snapshots 테이블이 있는지 먼저 확인
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='asset_snapshots'")
    if not cursor.fetchone():
        conn.close()
        return pd.DataFrame() # 테이블이 없으면 빈 DF 반환

    # 2. 소유자별 가장 최근 스냅샷 날짜 찾기
    query_latest = """
    SELECT owner, MAX(snapshot_date) as latest_date
    FROM asset_snapshots
    GROUP BY owner
    """
    latest_dates = pd.read_sql_query(query_latest, conn)
    
    if latest_dates.empty:
        conn.close()
        return pd.DataFrame()

    # 3. 각 소유자의 최신 날짜 데이터 모두 조회
    # owner와 date 쌍으로 조회
    query = """
    SELECT 
        owner, 
        balance_type, 
        asset_type, 
        account_name, 
        amount,
        snapshot_date
    FROM asset_snapshots 
    WHERE (owner, snapshot_date) IN (
        SELECT owner, snapshot_date FROM (
            SELECT owner, snapshot_date,
                   ROW_NUMBER() OVER (PARTITION BY owner ORDER BY snapshot_date DESC) as rn
            FROM asset_snapshots
        )
        WHERE rn = 1
    )
    ORDER BY owner DESC, balance_type DESC, amount DESC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

# utils/db_handler.py 에 추가

def get_previous_assets(target_date, owner):
    """
    특정 소유자의 데이터 중 target_date와 가장 가까운 snapshot_date의 데이터를 가져옵니다.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        # 1. 해당 소유자의 snapshot_date들 중 target_date와 차이(절대값)가 가장 작은 날짜 1개를 찾습니다.
        # strftime('%s', ...)는 날짜를 초 단위 타임스탬프로 변환하여 계산 가능하게 합니다.
        find_date_query = """
            SELECT snapshot_date
            FROM asset_snapshots
            WHERE owner = ?
            ORDER BY ABS(strftime('%s', snapshot_date) - strftime('%s', ?)) ASC
            LIMIT 1
        """
        closest_date_df = pd.read_sql(find_date_query, conn, params=(owner, target_date))

        if closest_date_df.empty:
            return pd.DataFrame()

        closest_date = closest_date_df.iloc[0]['snapshot_date']

        # 2. 찾은 '가장 근사한 날짜'에 해당하는 그 소유자의 모든 자산 내역을 가져옵니다.
        query = """
            SELECT * FROM asset_snapshots
            WHERE owner = ?
              AND snapshot_date = ?
        """
        df = pd.read_sql(query, conn, params=(owner, closest_date))
        return df
        
    finally:
        conn.close()

def get_latest_transaction_date() -> str | None:
    """transactions 테이블에서 가장 최근 날짜를 반환합니다. 데이터 없으면 None."""
    if not os.path.exists(DB_PATH):
        return None
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT MAX(date) FROM transactions")
        row = cursor.fetchone()
        return row[0] if row and row[0] else None


def get_available_asset_months() -> pd.DataFrame:
    """
    asset_snapshots에 실제 데이터가 존재하는 연/월 목록을 반환합니다.
    Returns: DataFrame with [year(int), month(int)] — 최신순 정렬
    """
    if not os.path.exists(DB_PATH):
        return pd.DataFrame(columns=['year', 'month'])

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='asset_snapshots'")
        if not cursor.fetchone():
            return pd.DataFrame(columns=['year', 'month'])

        query = """
            SELECT DISTINCT
                CAST(strftime('%Y', snapshot_date) AS INTEGER) AS year,
                CAST(strftime('%m', snapshot_date) AS INTEGER) AS month
            FROM asset_snapshots
            ORDER BY year DESC, month DESC
        """
        return pd.read_sql_query(query, conn)


def get_assets_for_month(year: int, month: int) -> pd.DataFrame:
    """
    특정 연/월 내에서 소유자별 가장 마지막 snapshot_date의 자산 데이터를 반환합니다.
    """
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()

    ym = f"{year:04d}-{month:02d}"

    with sqlite3.connect(DB_PATH) as conn:
        query = """
        SELECT
            owner,
            balance_type,
            asset_type,
            account_name,
            amount,
            snapshot_date
        FROM asset_snapshots
        WHERE (owner, snapshot_date) IN (
            SELECT owner, snapshot_date FROM (
                SELECT owner, snapshot_date,
                       ROW_NUMBER() OVER (
                           PARTITION BY owner
                           ORDER BY snapshot_date DESC
                       ) AS rn
                FROM asset_snapshots
                WHERE strftime('%Y-%m', snapshot_date) = ?
            )
            WHERE rn = 1
        )
        ORDER BY owner DESC, balance_type DESC, amount DESC
        """
        return pd.read_sql_query(query, conn, params=(ym,))


def init_budgets():
    """
    budgets 테이블이 비어 있을 때 transactions(owner='형준')의 카테고리로 초기화합니다.
    이미 데이터가 있으면 아무것도 하지 않습니다.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path_fixed = os.path.join(base_dir, '../../data/inasset_v1.db')

    with sqlite3.connect(db_path_fixed) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM budgets")
        if cursor.fetchone()[0] > 0:
            return

        cursor.execute("""
            INSERT OR IGNORE INTO budgets (category, monthly_amount, is_fixed_cost, sort_order)
            SELECT
                category_1,
                0,
                0,
                ROW_NUMBER() OVER (ORDER BY category_1)
            FROM (SELECT DISTINCT category_1 FROM transactions
                  WHERE owner = '형준' AND tx_type = '지출' AND category_1 IS NOT NULL)
        """)
        conn.commit()


def sync_categories_from_transactions():
    """
    transactions(owner='형준')에서 신규 카테고리를 감지하여 budgets에 자동 추가합니다.
    기존 budgets 데이터(예산액, 고정/변동 설정)는 유지됩니다.
    transactions가 없으면 아무것도 하지 않습니다.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path_fixed = os.path.join(base_dir, '../../data/inasset_v1.db')

    if not os.path.exists(db_path_fixed):
        return

    with sqlite3.connect(db_path_fixed) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO budgets (category, monthly_amount, is_fixed_cost, sort_order)
            SELECT DISTINCT COALESCE(NULLIF(refined_category_1, ''), category_1), 0, 0, 0
            FROM transactions
            WHERE owner = '형준'
              AND tx_type = '지출'
              AND COALESCE(NULLIF(refined_category_1, ''), category_1) IS NOT NULL
              AND COALESCE(NULLIF(refined_category_1, ''), category_1) NOT IN (SELECT category FROM budgets)
        """)
        conn.commit()


def get_budgets() -> pd.DataFrame:
    """
    budgets 테이블 전체를 반환합니다.
    비어 있으면 init_budgets()를 먼저 실행합니다.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path_fixed = os.path.join(base_dir, '../../data/inasset_v1.db')

    if not os.path.exists(db_path_fixed):
        return pd.DataFrame(columns=['category', 'monthly_amount', 'is_fixed_cost'])

    init_budgets()

    with sqlite3.connect(db_path_fixed) as conn:
        df = pd.read_sql_query(
            "SELECT category, monthly_amount, is_fixed_cost, sort_order FROM budgets ORDER BY sort_order, category",
            conn,
        )
    return df


def save_budgets(df: pd.DataFrame):
    """
    예산 데이터프레임을 budgets 테이블에 저장합니다.
    기존 데이터를 모두 교체합니다.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path_fixed = os.path.join(base_dir, '../../data/inasset_v1.db')

    required = {'category', 'monthly_amount', 'is_fixed_cost', 'sort_order'}
    if not required.issubset(df.columns):
        raise ValueError(f"budgets 저장에 필요한 컬럼이 없습니다: {required - set(df.columns)}")

    save_df = df[['category', 'monthly_amount', 'is_fixed_cost', 'sort_order']].copy()
    save_df['monthly_amount'] = save_df['monthly_amount'].fillna(0).astype(int)
    save_df['is_fixed_cost'] = save_df['is_fixed_cost'].astype(int)
    save_df['sort_order'] = save_df['sort_order'].fillna(0).astype(int)

    with sqlite3.connect(db_path_fixed) as conn:
        conn.execute("DELETE FROM budgets")
        save_df.to_sql('budgets', conn, if_exists='append', index=False)
        conn.commit()


def get_category_avg_monthly(months: int = 12) -> pd.DataFrame:
    """
    최근 N개월간 카테고리별 월평균 지출 금액을 반환합니다.
    Returns: DataFrame with columns [category_1, avg_monthly]
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path_fixed = os.path.join(base_dir, '../../data/inasset_v1.db')

    if not os.path.exists(db_path_fixed):
        return pd.DataFrame(columns=['category_1', 'avg_monthly'])

    query = """
        SELECT
            COALESCE(NULLIF(refined_category_1, ''), category_1) AS category_1,
            ROUND(
                SUM(amount) * 1.0 / COUNT(DISTINCT strftime('%Y-%m', date))
            ) AS avg_monthly
        FROM transactions
        WHERE tx_type = '지출'
          AND date >= date('now', ?)
        GROUP BY COALESCE(NULLIF(refined_category_1, ''), category_1)
    """
    param = f'-{months} months'

    with sqlite3.connect(db_path_fixed) as conn:
        df = pd.read_sql_query(query, conn, params=(param,))

    return df


def get_few_shot_examples(months: int = 3, tx_type: str = '지출') -> pd.DataFrame:
    """형준의 최근 N개월 (description, category_1) 패턴을 few-shot 예시로 반환합니다."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame(columns=['description', 'category_1'])
    param = f'-{months} months'
    query = """
        SELECT DISTINCT description, category_1
        FROM transactions
        WHERE owner = '형준'
          AND tx_type = ?
          AND date >= date('now', ?)
          AND description IS NOT NULL
          AND category_1 IS NOT NULL
        ORDER BY description
    """
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(query, conn, params=(tx_type, param))


def get_transactions_for_reclassification(start_date: str, end_date: str) -> pd.DataFrame:
    """
    지정 기간 내 개별 거래 행 (id, date, description, amount, memo, category_1, tx_type, current_refined) 반환.
    카테고리 재분류 UI의 입력 데이터로 사용됩니다.
    """
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    query = """
        SELECT
            id,
            date,
            description,
            amount,
            COALESCE(memo, '')                          AS memo,
            category_1,
            tx_type,
            COALESCE(refined_category_1, '')            AS current_refined
        FROM transactions
        WHERE date >= ? AND date <= ?
          AND tx_type != '이체'
          AND description IS NOT NULL
        ORDER BY date, id
    """
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(query, conn, params=(start_date, end_date))


def get_existing_refined_mappings(pairs: list) -> dict:
    """
    (date, description, category_1) 트리플 목록 중 DB에 이미 refined_category_1이 저장된 항목을 반환합니다.
    파일 업로드/자동업데이트 시 DELETE+INSERT 전후로 기존 수동 분류값을 복원하기 위해 사용합니다.

    Args:
        pairs: [(date, description, category_1), ...] 리스트  (date는 'YYYY-MM-DD' 문자열)

    Returns:
        {(date, description, category_1): refined_category_1} - 기존 분류가 있는 항목만 포함
    """
    if not os.path.exists(DB_PATH) or not pairs:
        return {}
    query = """
        SELECT date, description, category_1, refined_category_1
        FROM transactions
        WHERE description IS NOT NULL
          AND refined_category_1 IS NOT NULL
          AND refined_category_1 != ''
    """
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(query, conn)
    pair_set = set(pairs)
    return {
        (row['date'], row['description'], row['category_1']): row['refined_category_1']
        for _, row in df.iterrows()
        if (row['date'], row['description'], row['category_1']) in pair_set
    }


def update_refined_categories(mapping: dict, start_date: str, end_date: str) -> int:
    """
    지정 기간 내 transactions.refined_category_1을 (description, category_1) 기준으로 일괄 업데이트합니다.

    Args:
        mapping    : {(description, category_1): refined_category_1} 딕셔너리
        start_date : 업데이트 대상 시작일 (YYYY-MM-DD)
        end_date   : 업데이트 대상 종료일 (YYYY-MM-DD)

    Returns:
        업데이트된 총 행 수
    """
    if not os.path.exists(DB_PATH) or not mapping:
        return 0
    total = 0
    with sqlite3.connect(DB_PATH) as conn:
        for (description, category_1), refined_cat in mapping.items():
            cursor = conn.execute(
                """UPDATE transactions
                   SET refined_category_1 = ?
                   WHERE description = ? AND category_1 = ? AND date >= ? AND date <= ?""",
                (refined_cat, description, category_1, start_date, end_date),
            )
            total += cursor.rowcount
        conn.commit()
    return total


def get_asset_history() -> pd.DataFrame:
    """
    전체 자산 스냅샷 이력을 snapshot_date × owner 기준으로 집계합니다.
    부채는 DB에 양수로 저장되므로 net_worth 계산 시 차감합니다.
    Returns: DataFrame with [snapshot_date, owner, total_asset, total_debt, net_worth]
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path_fixed = os.path.join(base_dir, '../../data/inasset_v1.db')

    if not os.path.exists(db_path_fixed):
        return pd.DataFrame(columns=['snapshot_date', 'owner', 'total_asset', 'total_debt', 'net_worth'])

    query = """
        SELECT
            snapshot_date,
            owner,
            SUM(CASE WHEN balance_type = '자산' THEN amount ELSE 0 END) AS total_asset,
            SUM(CASE WHEN balance_type = '부채' THEN amount ELSE 0 END) AS total_debt,
            SUM(CASE WHEN balance_type = '자산' THEN amount
                     WHEN balance_type = '부채' THEN -amount
                     ELSE 0 END) AS net_worth
        FROM asset_snapshots
        GROUP BY snapshot_date, owner
        ORDER BY snapshot_date ASC
    """
    with sqlite3.connect(db_path_fixed) as conn:
        return pd.read_sql_query(query, conn)


def fill_combined_trend(df: pd.DataFrame) -> pd.DataFrame:
    """
    전체 소유자 합산 트렌드를 계산합니다.
    특정 날짜에 데이터가 없는 소유자는 가장 가까운 과거 스냅샷으로 보정 후 합산합니다.
    """
    df = df.sort_values('snapshot_date')
    owners = df['owner'].unique()
    all_dates = sorted(df['snapshot_date'].unique())

    rows = []
    for d in all_dates:
        total_net, total_asset, total_debt = 0.0, 0.0, 0.0
        for owner in owners:
            past = df[(df['owner'] == owner) & (df['snapshot_date'] <= d)]
            if not past.empty:
                latest = past.iloc[-1]
                total_net += float(latest['net_worth'])
                total_asset += float(latest['total_asset'])
                total_debt += float(latest['total_debt'])
        rows.append({'snapshot_date': d, 'net_worth': total_net,
                     'total_asset': total_asset, 'total_debt': total_debt})
    return pd.DataFrame(rows)


def execute_query_safe(sql: str, max_rows: int = 200) -> str:
    """
    챗봇이 생성한 SELECT 쿼리를 안전하게 실행합니다.
    SELECT/WITH 쿼리만 허용하고, 결과를 문자열로 반환합니다.
    """
    sql_stripped = sql.strip()
    sql_upper = sql_stripped.upper()

    if not (sql_upper.startswith('SELECT') or sql_upper.startswith('WITH')):
        return "오류: SELECT 쿼리만 허용됩니다."

    forbidden_keywords = ['DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER', 'CREATE', 'ATTACH', 'PRAGMA']
    for kw in forbidden_keywords:
        if re.search(rf'\b{kw}\b', sql_upper):
            return f"오류: '{kw}' 명령은 허용되지 않습니다."

    if not os.path.exists(DB_PATH):
        return "데이터베이스가 없습니다. 먼저 데이터를 업로드해주세요."

    try:
        with sqlite3.connect(DB_PATH) as conn:
            df = pd.read_sql_query(sql_stripped, conn)
            if df.empty:
                return "조회 결과가 없습니다."

            suffix = ""
            if len(df) > max_rows:
                df = df.head(max_rows)
                suffix = f"\n(전체 결과 중 상위 {max_rows}건만 표시)"

            # 금액 컬럼 포맷팅
            for col in df.columns:
                if col in ('amount', 'total') and pd.api.types.is_numeric_dtype(df[col]):
                    df[col] = df[col].apply(lambda x: f"{int(x):,}원" if pd.notna(x) else "")

            return df.to_string(index=False) + suffix
    except Exception as e:
        return f"쿼리 실행 오류: {str(e)}"


