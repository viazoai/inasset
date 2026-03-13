# PLAN.md — InAsset 개발 계획

## 구현 현황

| 단계 | 내용 | 상태 |
|------|------|------|
| Step 0 | 기술 부채 정리 | ✅ |
| Step 1 | 로그인/인증 (streamlit-authenticator, admin/user 역할) | ✅ |
| Step 2 | 목표 예산 설정 (budgets 테이블, 카테고리 자동 동기화) | ✅ |
| Step 3 | 멀티 포맷 업로더 + 파일명 날짜 자동 감지 | ✅ |
| Step 4 | GPT 카테고리 자동 매핑 + 검수 UI | ✅ |
| Step 5 | 이상 지출 탐지 (2σ 기준, 드릴다운) | ✅ |
| Step 6 | Burn-rate 분석 (과거 패턴 기반 월말 예측) | ✅ |
| Step 7 | 자산 트렌드 MVP (선형 회귀, Prophet은 마이그레이션 후) | ✅ |
| Step 8 | NL-to-SQL 멀티턴 고도화 | ✅ |
| Step 9 | 이메일 기반 자동 업데이트 (FastAPI ingest + Telegram 알림) | ✅ |
| Step 10 | 페이지별 URL 라우팅 (`st.navigation()`) | ✅ |
| Step 11 | 동적 시각화 (챗봇 Plotly 차트) | ❌ |
| Step 12 | 모바일 Web App (PWA) — 갤럭시 홈 화면 설치 | ❌ |

---

## 알려진 이슈

- `data_management.py`에 ZIP 비밀번호 하드코딩 (형준=0979, 윤희=1223)
- `transactions.py`에 owner 보정 로직 하드코딩 (`Mega/페이코` → 윤희)
- `transactions` 스키마에 `refined_category_2` 컬럼 정의되어 있으나 미활용
- Step 7 자산 트렌드: 현재 선형 회귀(numpy) MVP. 데이터 마이그레이션 후 Prophet으로 교체 예정
- `asset_snapshots`의 부채 `amount`는 양수로 저장됨 (net_worth 계산 시 차감 필요)

---

## 다음 단계

### 데이터 마이그레이션 (Step 8 이전 권장)

2022~2025년 4년치 과거 데이터 일괄 업로드. Step 5~7 분석 신뢰도 확보 및 Prophet 교체 조건 충족.

- `data_management.py` 수동 업로드 탭에서 Excel 일괄 업로드
- `save_transactions()`의 날짜 범위 삭제-재삽입 로직이 연도별로 정확히 동작하는지 사전 검증 필요
- `asset_snapshots`는 동일 날짜+소유자 덮어쓰기 — 중복 업로드 시 자동 교체됨

---

### Step 8 — NL-to-SQL 에이전트 고도화

**목표:** 자연어 질문 → SQL → 한국어 답변의 신뢰도와 안정성 향상

#### 현재 상태
- `ask_gpt_finance()` 멀티턴 루프 완료 (최대 5회 Function Calling 반복)
- `execute_query_safe()` SELECT 전용 안전 실행기 완료

#### 잔여 작업

**8-1. 슬라이딩 윈도우** (`ai_agent.py`)
- `chat_history` 전체를 GPT에 전달 중 → 장기 대화 시 토큰 초과 위험
- `chat_history[-10:]` 방식으로 최근 N턴만 전달
- 단, system 메시지는 항상 포함 (슬라이싱 시 첫 항목 보존)

```python
# 적용 위치: ask_gpt_finance() 내 messages 구성 시
system_msg = chat_history[0]
recent = chat_history[-10:] if len(chat_history) > 10 else chat_history[1:]
messages = [system_msg] + recent
```

**8-2. 예산 데이터 동적 주입** (`ai_agent.py`, `chatbot.py`)
- 현재 시스템 프롬프트에 예산 정보 없음 → "이번 달 예산 얼마야?" 질문 응답 불가
- 챗봇 호출 시 `get_budgets()` 결과를 시스템 프롬프트에 주입

```python
# chatbot.py → ask_gpt_finance() 호출 전
budgets_df = get_budgets()
budget_context = budgets_df.to_string(index=False) if not budgets_df.empty else "없음"
# system 메시지에 f"## 월 예산\n{budget_context}" 추가
```

**8-3. SQL 오류 복구** (`ai_agent.py`)
- 현재 SQL 실행 오류 시 오류 메시지를 그대로 GPT에 재전달
- 오류 메시지를 구조화하여 GPT가 수정 쿼리를 생성하도록 유도하는 프롬프트 개선

**8-4. 응답 후처리** (`ai_agent.py`)
- 금액 결과에 단위(원) 자동 포맷팅 유도 (시스템 프롬프트 지시 추가)
- 결과가 0건일 때 "데이터 없음" 대신 원인 추론 유도

#### 수정 대상 파일
- `src/utils/ai_agent.py` — 8-1, 8-2, 8-3, 8-4
- `src/pages/chatbot.py` — 8-2 (budgets 조회 후 전달)

---

### Step 9 — 이메일 기반 자동 업데이트 ✅ 완료

#### 구현 내용

**아키텍처:**
```
뱅크샐러드 발송 이메일 (Gmail)
  → n8n 워크플로우 (Gmail Trigger)
    → HTTP Request 노드 → POST /api/ingest (포트 3102)
      → file_handler.py (ZIP/Excel 파싱)
        → apply_direct_category() (GPT 없이 직통 저장)
          → get_existing_refined_mappings() (수동 재분류값 보존)
            → save_transactions() (DELETE+INSERT)
              → Telegram Bot API (완료 알림)
```

**카테고리 처리 규칙:**

| 원본 `category_1` | `refined_category_1` 저장값 |
|---|---|
| `개회` | `예비비` (CATEGORY_REMAP 강제 재분류) |
| 그 외 모든 값 | 원본 `category_1` 그대로 |
| DB에 수동 재분류 이력 있는 값 | 기존 `refined_category_1` 복원 |

**처리 범위 결정 로직:**
- DB에 해당 소유자 데이터가 이미 있으면 → 파일 종료일 기준 최근 2개월만 갱신
- DB가 비어있으면 → 파일 전체 기간 저장

**수동 재분류값 보존 방법:**
- `get_existing_refined_mappings(pairs)` 함수: `(date, description, category_1)` 3-tuple 키로 DB 조회
- DELETE 전에 조회 → INSERT 시 기존값 복원 → 사용자의 수동 분류가 덮어씌워지지 않음

**텔레그램 알림 메시지:**
```
✅ InAsset 자동 업데이트 완료

👤 소유자: 형준
📅 기간: 2026-01-14 ~ 2026-03-14
📥 거래 저장: 150건 (신규 45건)
🏦 자산 저장: 3건
⚠️ 표준 카테고리 미해당 5건 — 앱에서 카테고리 정규화를 진행해주세요  ← 있을 때만
```
- `신규 N건`: 기존 재분류 이력 없는 새 거래
- `표준 카테고리 미해당`: STANDARD_CATEGORIES에 없는 항목 → 앱에서 카테고리 정규화 필요

**구현 파일:**
- `src/api/ingest.py` — FastAPI 엔드포인트 (`POST /api/ingest`, `GET /health`)
- `src/utils/file_handler.py` — `apply_direct_category()`, `CATEGORY_REMAP` 추가
- `src/utils/db_handler.py` — `get_existing_refined_mappings()` (date 포함 3-tuple 키로 변경)
- `docker-compose.yml` — `inasset-ingest` 서비스 추가 (포트 3102)
- `requirements.txt` — `fastapi`, `uvicorn[standard]`, `httpx`, `python-multipart` 추가
- `.env` — `INGEST_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 추가

**data_management.py UI 변경:**
- 자동 업데이트 (메일 기반) 섹션 제거 (ingest API로 이관)
- 카테고리 정규화 섹션 → 최상단으로 이동
- 수동 업데이트 섹션 → 카테고리 정규화 아래로 이동

---
### Step 10 — 페이지별 URL 라우팅 ✅ 완료

**방식:** `st.navigation(position="hidden")` + `st.page_link()` 조합
- 자동 생성 네비는 숨기고, 사이드바 타이틀~사용자 정보 사이에 `st.page_link()`로 직접 배치
- 각 `pages/*.py` 하단에 `render()` 호출 추가

**URL 매핑:**

| URL 경로 | 페이지 | 접근 권한 |
|----------|--------|----------|
| `/analysis` | 📊 분석 리포트 (기본) | 전체 |
| `/chatbot` | 🤖 가계부 AI 비서 | 전체 |
| `/transactions` | 💰 수입/지출 현황 | 전체 |
| `/assets` | 🏦 자산 현황 | 전체 |
| `/budget` | 🎯 목표 예산 | 전체 |
| `/data` | 📂 데이터 관리 | admin only |

**구현 파일:**
- `src/app.py` — `st.navigation(position="hidden")`, `st.page_link()` 사이드바 배치, 기존 버튼 라우팅 제거
- `src/pages/*.py` — 각 파일 하단에 `render()` 추가 (6개 파일)

---
### Step 11 — 동적 시각화

**목표:** 챗봇 답변에 Plotly 차트를 함께 렌더링. GPT가 차트 파라미터만 결정하고 Python이 직접 생성 (보안 원칙 유지)

#### 설계 원칙
- GPT는 `render_chart(chart_type, title, labels, values)` Tool 파라미터만 결정
- 허용 chart_type: `pie` / `bar` / `line` (하드코딩, GPT가 임의 코드 실행 불가)
- Python이 파라미터를 검증 후 Plotly Figure 직접 생성

#### 작업 목록

**10-1. Tool 정의 추가** (`ai_agent.py`)

```python
{
    "type": "function",
    "function": {
        "name": "render_chart",
        "description": "차트를 렌더링한다. 데이터를 시각화할 때 사용.",
        "parameters": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["pie", "bar", "line"]},
                "title":      {"type": "string"},
                "labels":     {"type": "array", "items": {"type": "string"}},
                "values":     {"type": "array", "items": {"type": "number"}}
            },
            "required": ["chart_type", "title", "labels", "values"]
        }
    }
}
```

**10-2. 차트 생성 함수** (`ai_agent.py` 또는 `chatbot.py`)

```python
def build_chart(chart_type, title, labels, values) -> go.Figure:
    if chart_type == "pie":
        ...
    elif chart_type == "bar":
        ...
    elif chart_type == "line":
        ...
    else:
        raise ValueError(f"허용되지 않는 chart_type: {chart_type}")
```

**10-3. 챗봇 렌더링 분기** (`chatbot.py`)
- `ask_gpt_finance()` 반환값에 차트 데이터 포함 여부 확인
- `tool_call.function.name == "render_chart"` 이면 `st.plotly_chart()` 렌더링
- 텍스트 답변과 차트를 함께 표시

**10-4. 지원 시나리오 (우선순위순)**
1. 카테고리별 지출 비율 → `pie`
2. 월별 지출 추이 → `bar` 또는 `line`
3. 자산 변동 추이 → `line`

#### 수정 대상 파일
- `src/utils/ai_agent.py` — Tool 정의, `build_chart()` 함수, `ask_gpt_finance()` 반환값 확장
- `src/pages/chatbot.py` — 차트 렌더링 분기 처리

---

### Step 12 — 모바일 Web App (PWA)

**목표:** 갤럭시 등 안드로이드에서 홈 화면에 설치해 Open WebUI처럼 앱 형태로 실행

**전제 조건:** Cloudflare Tunnel로 외부 접속 URL이 이미 확보되어 있어야 함

#### 구조

```
갤럭시 홈 화면 아이콘
  → Cloudflare Tunnel URL (HTTPS)
    → Nginx (PWA 정적 파일 서빙 + 리버스 프록시)
      → Streamlit (localhost:8501)
```

Streamlit은 `manifest.json`과 Service Worker를 직접 서빙할 수 없어 **Nginx를 앞단에 추가**합니다.

#### 작업 목록

**11-1. PWA 정적 파일 준비** (`static/` 폴더 신규)

```
static/
├── manifest.json       # PWA 설치 메타데이터
├── sw.js               # Service Worker (오프라인 대응 최소 구현)
└── icons/
    ├── icon-192.png
    └── icon-512.png
```

`manifest.json` 핵심 항목:
```json
{
  "name": "InAsset",
  "short_name": "InAsset",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#ffffff",
  "theme_color": "#667eea",
  "icons": [...]
}
```

**11-2. Nginx 컨테이너 추가** (`docker-compose.yml`)
- `static/` 폴더의 `manifest.json`, `sw.js`, `icons/`를 직접 서빙
- `/` 이하 나머지 요청은 Streamlit(8501)로 프록시
- WebSocket(`/_stcore/stream`) 프록시 설정 필수 (Streamlit 실시간 통신)

```nginx
location /manifest.json { root /static; }
location /sw.js         { root /static; }
location /icons/        { root /static; }
location /              { proxy_pass http://streamlit:8501; }
```

**11-3. PWA 메타태그 주입** (`src/app.py`)

`st.markdown`으로 `<head>`에 삽입:
```html
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#667eea">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
```

**11-4. 모바일 UI 최적화** (`src/app.py`, 각 페이지)
- 사이드바 기본 접힘 설정: `st.set_page_config(initial_sidebar_state="collapsed")`
- 핵심 지표 카드 폰트·패딩 모바일 대응 CSS 점검

**11-5. 갤럭시 설치 테스트**
- Chrome → 주소창 우측 설치 아이콘 확인
- Samsung Internet → 메뉴 → 홈 화면에 추가
- 설치 후 독립 앱 모드(주소창 없음) 동작 확인

#### 수정 대상 파일
- `docker-compose.yml` — Nginx 서비스 추가, 포트 변경 (3101 → Nginx)
- `nginx/nginx.conf` — 신규 작성
- `static/manifest.json`, `static/sw.js`, `static/icons/` — 신규
- `src/app.py` — PWA 메타태그 주입, 사이드바 초기 상태

---


