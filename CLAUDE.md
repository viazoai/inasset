# CLAUDE.md — InAsset

InAsset은 부부(형준/윤희)의 가계부 앱이다. BankSalad Excel 내보내기를 SQLite에 저장하고 Streamlit으로 시각화하며, GPT-4o 챗봇으로 자연어 질의를 지원한다.

## 앱 실행

```bash
# Docker (권장) — http://localhost:3101
docker-compose up -d

# 로컬 직접 실행
pip install -r requirements.txt
streamlit run src/app.py
```

## 아키텍처

**수동 업로드 (GPT 매핑):**
```
BankSalad ZIP/Excel 업로드 (data_management.py)
  → file_handler.py  (ZIP 해제, Excel 파싱, 파일명 날짜 추출)
  → ai_agent.py      (GPT-4o 카테고리 매핑)
  → db_handler.py    (SQLite DELETE+INSERT)
  → pages/           (Streamlit 화면)
```

**자동 업데이트 (n8n + FastAPI, GPT 없음):**
```
뱅크샐러드 이메일 → n8n (Gmail Trigger)
  → POST /api/ingest (inasset-ingest 컨테이너, 포트 3102)
    → file_handler.py  (파싱)
    → apply_direct_category()  (category_1 → refined_category_1 직통 복사)
    → get_existing_refined_mappings()  (수동 재분류값 보존)
    → db_handler.py    (SQLite DELETE+INSERT)
    → Telegram Bot API (완료/실패 알림)
```

**챗봇:**
```
Streamlit UI → ai_agent.py (GPT-4o Function Calling + NL-to-SQL)
```

## 핵심 파일

| 파일 | 역할 |
|------|------|
| `src/app.py` | 진입점, 사이드바 라우팅, DB 초기화, 인증/승인 |
| `src/pages/budget.py` | 🎯 목표 예산 설정 |
| `src/pages/transactions.py` | 💰 수입/지출 현황 |
| `src/pages/assets.py` | 🏦 자산 현황 |
| `src/pages/chatbot.py` | 🤖 AI 챗봇 |
| `src/pages/data_management.py` | 📂 데이터 관리 — 카테고리 정규화(상단) / 수동 업로드(GPT) / DB 초기화 |
| `src/pages/analysis.py` | 📊 분석 리포트 — AI 요약 / 이상 지출 / Burn-rate / 자산 트렌드 |
| `src/api/ingest.py` | ⚡ FastAPI 자동 업데이트 엔드포인트 — n8n이 POST로 파일 전송, GPT 없이 저장, Telegram 알림 |
| `src/utils/db_handler.py` | 모든 SQLite 작업 |
| `src/utils/file_handler.py` | ZIP/Excel 파싱, 파일명 메타데이터 추출, 카테고리 직통 복사 |
| `src/utils/ai_agent.py` | OpenAI API 래퍼 (카테고리 매핑 / 분석 요약 / 챗봇) |

## 데이터베이스 스키마

**DB 경로:** `data/inasset_v1.db` (gitignore 됨)

```sql
-- 거래 내역
transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT,                 -- YYYY-MM-DD
  time TEXT,                 -- HH:MM
  tx_type TEXT,              -- 수입 / 지출 / 이체
  category_1 TEXT,           -- 원본 대분류 (뱅크샐러드 그대로)
  category_2 TEXT,           -- 소분류
  refined_category_1 TEXT,   -- 표준화 대분류 (GPT 매핑값, NULL·빈값이면 category_1 사용)
  refined_category_2 TEXT,   -- 미사용
  description TEXT,          -- 내용/상호명
  amount INTEGER,            -- 금액(원). 지출은 음수(-50000), 수입은 양수(+3000000)
  currency TEXT,
  source TEXT,               -- 결제수단
  memo TEXT,
  owner TEXT,                -- 형준 / 윤희 / 공동
  created_at TIMESTAMP
)

-- 자산 스냅샷 (동일 snapshot_date+owner → DELETE+INSERT)
asset_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_date TEXT,        -- YYYY-MM-DD
  balance_type TEXT,         -- 자산 / 부채
  asset_type TEXT,           -- 현금 자산, 투자성 자산 등
  account_name TEXT,
  amount INTEGER,            -- 원 단위, 부채는 양수로 저장(부호 반전 없음)
  owner TEXT,
  created_at TIMESTAMP
)

-- 카테고리별 월 예산
budgets (
  category       TEXT PRIMARY KEY,  -- transactions의 실효 대분류와 동일
  monthly_amount INTEGER,           -- 월 예산 (원 단위, 0=미설정)
  is_fixed_cost  INTEGER,           -- 1=고정 지출, 0=변동 지출
  sort_order     INTEGER
)

-- docs/ 폴더 자동처리 이력
processed_files (
  filename      TEXT PRIMARY KEY,
  owner         TEXT,
  snapshot_date TEXT,
  processed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

**카테고리 조회 규칙:** `refined_category_1`이 있으면 우선 사용, 없으면 `category_1` fallback.
```sql
COALESCE(NULLIF(refined_category_1, ''), category_1)
```

## 주요 함수

### db_handler.py
- `_init_db()` — 테이블 생성 + migration (category_rules DROP, budgets sort_order ADD)
- `save_transactions(df, owner, filename)` — 데이터 기간 내 해당 소유자 DELETE 후 INSERT
- `save_asset_snapshot(df, owner, snapshot_date)` — 동일 날짜+소유자 DELETE+INSERT
- `get_analyzed_transactions()` — transactions LEFT JOIN budgets, tx_type!='이체', COALESCE 카테고리
- `get_latest_assets()` — 소유자별 최신 스냅샷
- `get_previous_assets(target_date, owner)` — target_date에 가장 근접한 스냅샷 (delta 계산용)
- `init_budgets()` — budgets 비어있을 때 형준 거래내역 카테고리로 seed
- `sync_categories_from_transactions()` — 업로드 후 신규 카테고리를 budgets에 자동 추가
- `get_budgets()` — budgets 전체 반환 (비어있으면 init_budgets 선실행)
- `save_budgets(df)` — budgets 전체 교체 저장
- `get_category_avg_monthly(months)` — 최근 N개월 카테고리별 월평균 지출
- `get_few_shot_examples(months, tx_type)` — GPT 매핑용 few-shot 예시 (형준 최근 N개월)
- `get_transactions_for_reclassification(start_date, end_date)` — 기간별 고유 description 목록 (재분류용)
- `update_refined_categories(mapping, start_date, end_date)` — refined_category_1 일괄 업데이트
- `has_transactions_in_range(owner, start_date, end_date)` — 기간 내 데이터 존재 여부
- `get_existing_refined_mappings(pairs)` — `(date, description, category_1)` 3-tuple 키로 기존 재분류값 조회. DELETE+INSERT 전 수동 분류값 보존용
- `get_processed_filenames()` — `{filename: processed_at}` dict 반환 (set 아님)
- `mark_file_processed(filename, owner, snapshot_date, status)` — 처리 완료 기록
- `clear_all_data()` — transactions / asset_snapshots / processed_files 전체 삭제
- `get_asset_history()` — snapshot_date × owner 기준 집계 (total_asset, total_debt, net_worth)
- `execute_query_safe(sql, max_rows=200)` — 챗봇용 SELECT 전용 안전 실행기

### file_handler.py
- `detect_owner_from_filename(filename)` — ZIP: 님_ 패턴 / Excel: _나·_내사랑 suffix
- `scan_docs_folder()` — docs/ 스캔, `{filename, owner, snapshot_date, start_date, mtime}` 반환
- `extract_date_range(filename)` — `(start_date, end_date)` 추출, 없으면 `(None, 오늘)`
- `extract_snapshot_date(filename)` — end_date만 추출
- `process_uploaded_zip(uploaded_file, password, start_date, end_date)` — ZIP 해제 + Excel 파싱
- `process_uploaded_excel(uploaded_file, start_date, end_date)` — Excel 직접 파싱
- `_parse_excel_sheets(excel_data, start_date, end_date)` — Sheet0(자산) / Sheet1(거래) 파서
- `_parse_asset_sheet(df)` — BankSalad 병합셀 처리
- `apply_direct_category(df)` — GPT 없이 `대분류` → `refined_category_1` 직통 복사. `CATEGORY_REMAP = {"개회": "예비비"}` 적용

### ai_agent.py
- `map_categories(client, pairs_df, few_shot_df, categories)` — GPT few-shot 카테고리 매핑, `(result_df, usage_dict)` 반환
- `generate_analysis_summary(client, anomaly_metrics, burnrate_metrics)` — 이상지출·Burn-rate 메트릭을 받아 친근한 한국어 요약 2~3문장 생성
- `ask_gpt_finance(client, chat_history)` — Function Calling 멀티턴 루프 (최대 5회 반복). GPT가 tool_call 없을 때 최종 답변 반환

### api/ingest.py (FastAPI)
- `POST /api/ingest` — n8n이 뱅크샐러드 파일을 전송하는 엔드포인트
  - `X-Ingest-Secret` 헤더로 인증 (`INGEST_SECRET` 환경변수)
  - Form: `file` (ZIP/Excel), `owner` (형준/윤희/공동), `password` (ZIP용, 선택)
  - 처리 범위: 기존 데이터 있으면 종료일 기준 최근 2개월, 없으면 전체
  - 카테고리: `apply_direct_category()` → `get_existing_refined_mappings()` 순서로 적용
  - 처리 후 Telegram 알림 발송 (`거래 저장: N건 (신규 M건)`)
- `GET /health` — 헬스체크
- `uvicorn src.api.ingest:app --host 0.0.0.0 --port 3102` 로 실행

## 환경변수

`.env` 파일 (gitignore됨):
```
OPENAI_API_KEY=sk-...
INGEST_SECRET=<랜덤 토큰>          # POST /api/ingest 인증용
TELEGRAM_BOT_TOKEN=<BotFather 발급 토큰>
TELEGRAM_CHAT_ID=<알림 받을 채팅 ID>
```

## 코드 컨벤션

- **언어**: UI·주석 모두 한국어
- **금액**: INTEGER (원 단위). 지출 = 음수, 수입 = 양수. 집계 시 `ABS(amount)` 또는 `-amount` 사용
- **카테고리**: `COALESCE(NULLIF(refined_category_1, ''), category_1)` 패턴으로 항상 실효값 사용
- **날짜**: TEXT `YYYY-MM-DD`, 시간 TEXT `HH:MM`
- **소유자**: 형준 / 윤희 / 공동
- **페이지 구조**: 각 페이지 파일에 `render()` 함수 하나
- **DB 연결**: `with sqlite3.connect(DB_PATH) as conn:` 컨텍스트 매니저 사용
- **이체 제외**: `get_analyzed_transactions()`에서 `tx_type != '이체'` 필터링됨

## Docker

컨테이너 2개 운영:

| 서비스 | 포트 | 역할 |
|--------|------|------|
| `inasset` | 3101 | Streamlit 앱 (`streamlit run src/app.py`) |
| `inasset-ingest` | 3102 | FastAPI 자동 업데이트 API (`uvicorn src.api.ingest:app`) |

```yaml
# 공통 설정
image: python:3.11-slim
volumes: .:/app
env_file: .env
TZ: Asia/Seoul
restart: always
```

볼륨 마운트(`.:/app`) — 코드 수정 후 컨테이너 재시작 필요.
```bash
docker compose up -d --force-recreate   # .env 변경 포함 재시작
docker compose up -d --force-recreate inasset-ingest  # ingest만 재시작
```

## 개발 계획

→ [PLAN.md](./PLAN.md) 참조 (구현 현황, 알려진 이슈, 다음 단계)
