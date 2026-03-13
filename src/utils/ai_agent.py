import json
from datetime import date
import pandas as pd
from openai import OpenAI

STANDARD_CATEGORIES = [
    '식비', '교통비', '고정비', '주거비', '금융', '보험',
    '생활비', '활동비', '친목비', '꾸밈비', '차량비', '여행비',
    '의료비', '기여비', '양육비', '예비비', '미분류',
]

INCOME_CATEGORIES = ['근로소득', '투자소득', '추가수입', '캐쉬백/포인트', '미분류']

DB_SCHEMA = """
[DB 스키마 — SQLite]

테이블: transactions (거래 내역)
  - date TEXT               : 날짜 (YYYY-MM-DD)
  - time TEXT               : 시간 (HH:MM)
  - tx_type TEXT            : 타입 — 수입 / 지출 / 이체
  - category_1 TEXT         : 원본 대분류 (뱅크샐러드 그대로)
  - refined_category_1 TEXT : 표준화 대분류 (GPT 재분류값, 없으면 NULL 또는 빈 문자열)
  - category_2 TEXT         : 소분류
  - description TEXT        : 내용/상호명
  - amount INTEGER          : 금액 (원 단위). 지출은 음수(-50000), 수입은 양수(+3000000)로 저장됨
  - currency TEXT           : 화폐
  - source TEXT             : 결제수단
  - memo TEXT               : 메모
  - owner TEXT              : 소유자 — 형준 / 윤희 / 공동

[중요] 카테고리 조회 규칙:
  - 카테고리별 집계/필터링 시 항상 아래 표현식을 사용해야 합니다:
      COALESCE(NULLIF(refined_category_1, ''), category_1)
  - 예시: WHERE COALESCE(NULLIF(refined_category_1, ''), category_1) = '식비'
  - 예시: GROUP BY COALESCE(NULLIF(refined_category_1, ''), category_1)

테이블: asset_snapshots (자산 스냅샷)
  - snapshot_date TEXT : 스냅샷 날짜 (YYYY-MM-DD)
  - balance_type TEXT  : 구분 — 자산 / 부채
  - asset_type TEXT    : 항목 (현금 자산, 투자성 자산 등)
  - account_name TEXT  : 상품명
  - amount INTEGER     : 금액 (원 단위, 부채는 양수로 저장됨)
  - owner TEXT         : 소유자 — 형준 / 윤희 / 공동

테이블: budgets (카테고리별 월 예산)
  - category       TEXT : 카테고리명 (transactions의 대분류와 동일)
  - monthly_amount INT  : 월 예산 (원 단위, 0이면 미설정)
  - is_fixed_cost  INT  : 고정비 여부 — 1=고정 지출 / 0=변동 지출
"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "SQLite DB에서 SELECT 쿼리로 데이터를 조회합니다. "
                "분석에 필요한 데이터만 정확히 쿼리하세요. "
                "이체(tx_type='이체')는 항상 제외하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "실행할 SELECT SQL 쿼리 (SELECT 또는 WITH 로 시작해야 함)"
                    }
                },
                "required": ["sql"]
            }
        }
    }
]


def map_categories(
    client: OpenAI,
    pairs_df: pd.DataFrame,
    few_shot_df: pd.DataFrame,
    categories: list = None,
) -> pd.DataFrame:
    """
    거래 내역의 (description, category_1) 조합을 GPT로 refined_category_1에 매핑합니다.

    Args:
        client      : OpenAI 클라이언트
        pairs_df    : 고유 (description, category_1) 쌍 DataFrame
        few_shot_df : few-shot 예시 DataFrame (description, category_1 컬럼)
        categories  : 사용할 표준 카테고리 리스트 (None이면 STANDARD_CATEGORIES 사용)

    Returns:
        pairs_df에 refined_category_1 컬럼이 추가된 DataFrame
    """
    cats = categories if categories is not None else STANDARD_CATEGORIES

    _zero_usage = {'model': 'gpt-4o', 'input_tokens': 0, 'output_tokens': 0}

    result_df = pairs_df.copy()
    result_df['refined_category_1'] = result_df['category_1']  # 기본값: 원본 카테고리

    if pairs_df.empty:
        return result_df, _zero_usage

    few_shot_text = "기존 데이터 없음"
    if not few_shot_df.empty:
        lines = [
            f"  - {row['description']} → {row['category_1']}"
            for _, row in few_shot_df.head(50).iterrows()
        ]
        few_shot_text = "\n".join(lines)

    items_lines = [
        f"{i + 1}. description=\"{row['description']}\", category_1=\"{row['category_1']}\""
        for i, (_, row) in enumerate(pairs_df.iterrows())
    ]
    items_text = "\n".join(items_lines)
    categories_str = ', '.join(cats)

    prompt = f"""가계부 카테고리 분류 전문가로서 아래 거래 내역의 refined_category_1을 결정해줘.

## 기존 분류 패턴 (few-shot 예시)
{few_shot_text}

## 표준 카테고리
{categories_str}

## 분류할 항목
{items_text}

## 응답 형식
JSON 형식으로만 응답해:
{{"mappings": [
  {{"index": 1, "refined_category_1": "식비"}},
  {{"index": 2, "refined_category_1": "교통비"}}
]}}

분류 규칙:
- category_1이 표준 카테고리와 일치하면 그대로 사용
- 표준 카테고리에 없거나 애매하면 description을 참고해 가장 적합한 카테고리로 분류
- 어느 카테고리도 맞지 않으면 '미분류' 사용"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content)
        for item in data.get("mappings", []):
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(result_df):
                refined = item.get("refined_category_1", "")
                if refined in cats:
                    result_df.at[result_df.index[idx], "refined_category_1"] = refined
        usage = response.usage
        usage_dict = {
            'model': response.model,
            'input_tokens': usage.prompt_tokens,
            'output_tokens': usage.completion_tokens,
        }
    except Exception:
        usage_dict = _zero_usage  # 오류 시 기본값(category_1) 유지

    return result_df, usage_dict


def generate_analysis_summary(
    client: OpenAI,
    anomaly_metrics: dict | None,
    burnrate_metrics: dict | None,
) -> str:
    """
    이상 지출 및 지출 예측 분석 결과를 바탕으로 친근한 한국어 요약을 생성합니다.

    Args:
        client           : OpenAI 클라이언트
        anomaly_metrics  : _compute_anomaly_metrics() 반환값 (None이면 데이터 부족)
        burnrate_metrics : _compute_burnrate_metrics() 반환값 (None이면 데이터 없음)

    Returns:
        str: 2~3문장 한국어 요약. 오류 시 빈 문자열 반환.
    """
    today_str = date.today().strftime('%Y년 %m월 %d일')

    # 이상 지출 요약 텍스트 구성
    if anomaly_metrics is None:
        anomaly_text = "이상 지출 분석 불가 (과거 3개월 이상 데이터 필요)"
    elif not anomaly_metrics.get("anomalies"):
        anomaly_text = "이상 지출 없음 — 모든 카테고리 정상 범위"
    else:
        lines = []
        for a in anomaly_metrics["anomalies"]:
            direction = "초과" if a["direction"] == "over" else "절감"
            lines.append(f"  - {a['category']}: 평소 대비 {abs(a['pct']):.0f}% {direction} ({a['diff']:+,}원)")
        anomaly_text = f"이상 지출 감지 ({anomaly_metrics['past_months']}개월 기준):\n" + "\n".join(lines)

    # 지출 예측 요약 텍스트 구성
    if burnrate_metrics is None:
        burnrate_text = "지출 예측 불가 (이번 달 지출 데이터 없음)"
    else:
        bm = burnrate_metrics
        budget_str = (
            f"예산 {bm['budget_total']:,}원 대비 {bm['budget_pct']:.0f}% 소진"
            if bm["budget_total"] > 0 else "예산 미설정"
        )
        exceed_str = "월말 예산 초과 예상" if bm["will_exceed"] else "월말 예산 내 예상"
        burnrate_text = (
            f"현재 누적 {bm['current_total']:,}원 ({budget_str}), "
            f"월말 예상 {bm['projected_total']:,}원 → {exceed_str}"
        )

    prompt = f"""오늘은 {today_str}입니다.
아래는 가계부 분석 시스템이 계산한 이번 달 현황입니다:

{anomaly_text}

{burnrate_text}

이 데이터를 바탕으로 분석 리포트 상단에 표시할 친근한 안내 메시지를 2~3문장으로 작성해줘.
규칙:
- 친근하고 따뜻한 톤 (딱딱한 보고서 말투 금지)
- 가장 중요한 내용 1~2가지만 강조, 나머지는 아래 차트 참고 유도
- 이상 지출이 없고 예산 내이면 긍정적인 메시지
- 순수 텍스트로만 응답 (마크다운, 이모지 없이)"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return ""


def compute_anomaly_metrics(df_all) -> dict | None:
    """이상 지출 계산. 데이터 부족(3개월 미만) 시 None 반환."""
    from datetime import date
    df = df_all[df_all['tx_type'] == '지출'].copy()
    if df.empty:
        return None

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
        return None

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
        return {"anomalies": [], "past_months": past_months}

    current_monthly = (
        current_df.groupby('category_1')['amount_abs']
        .sum().reset_index()
        .rename(columns={'amount_abs': 'current_amount'})
    )
    merged = current_monthly.merge(past_stats, on='category_1', how='left').dropna(subset=['mean', 'std'])
    anomalies_df = merged[
        (merged['std'] > 0) &
        (abs(merged['current_amount'] - merged['mean']) > 2 * merged['std'])
    ].copy()

    anomalies = []
    for _, row in anomalies_df.iterrows():
        diff = row['current_amount'] - row['mean']
        pct = (diff / row['mean'] * 100) if row['mean'] > 0 else 0
        anomalies.append({
            "category": row['category_1'],
            "current": int(row['current_amount']),
            "mean": int(row['mean']),
            "diff": int(diff),
            "pct": round(pct, 1),
            "direction": "over" if diff > 0 else "under",
        })

    return {"anomalies": anomalies, "past_months": past_months}


def compute_burnrate_metrics(df_all) -> dict | None:
    """Burn-rate 계산. 이번 달 지출 없으면 None 반환."""
    import calendar
    from datetime import date
    from utils.db_handler import get_budgets

    today = date.today()
    first_of_month = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    df = df_all.copy()
    df['date'] = pd.to_datetime(df['date'])
    df['year_month'] = df['date'].dt.to_period('M')
    current_period = pd.Period(today, 'M')

    budgets_df = get_budgets()
    budget_total = int(budgets_df['monthly_amount'].sum()) if not budgets_df.empty else 0

    df_month = df[
        (df['tx_type'] == '지출') &
        (df['date'] >= pd.Timestamp(first_of_month)) &
        (df['date'] <= pd.Timestamp(today))
    ].copy()
    df_month['amount_abs'] = df_month['amount'].abs()

    daily = df_month.groupby('date')['amount_abs'].sum().reset_index()
    date_range = pd.date_range(start=first_of_month, end=today)
    daily = (
        daily.set_index('date').reindex(date_range, fill_value=0).reset_index()
        .rename(columns={'index': 'date', 'amount_abs': 'amount'})
    )
    daily['cumulative'] = daily['amount'].cumsum()
    current_total = int(daily['cumulative'].iloc[-1]) if not daily.empty else 0

    past_12_df = df[
        (df['tx_type'] == '지출') &
        (df['year_month'] < current_period) &
        (df['year_month'] >= current_period - 12)
    ].copy()
    past_12_df['amount_abs'] = past_12_df['amount'].abs()
    past_12_df['day_of_month'] = past_12_df['date'].dt.day

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

    if current_total == 0 and projected_total == 0:
        return None

    budget_pct = (current_total / budget_total * 100) if budget_total > 0 else 0
    will_exceed = (projected_total > budget_total) if budget_total > 0 else False

    return {
        "current_total": current_total,
        "projected_total": projected_total,
        "budget_total": budget_total,
        "budget_pct": round(budget_pct, 1),
        "will_exceed": will_exceed,
    }


def ask_gpt_finance(client: OpenAI, chat_history: list) -> str:
    """
    Function Calling으로 GPT가 필요한 쿼리를 직접 작성·실행하고 답변을 생성합니다.

    Args:
        client      : OpenAI 클라이언트
        chat_history: 대화 이력 (최신 user 메시지 포함)

    Returns:
        str: GPT 최종 답변
    """
    from utils.db_handler import execute_query_safe, get_budgets

    today = date.today().strftime('%Y-%m-%d')

    # 8-2. 예산 데이터 동적 주입
    budgets_df = get_budgets()
    if not budgets_df.empty:
        budget_lines = [
            f"  - {row['category']}: 월 {row['monthly_amount']:,}원 ({'고정' if row['is_fixed_cost'] else '변동'})"
            for _, row in budgets_df[budgets_df['monthly_amount'] > 0].iterrows()
        ]
        budget_context = "\n".join(budget_lines) if budget_lines else "  설정된 예산 없음"
    else:
        budget_context = "  설정된 예산 없음"

    system_prompt = f"""너는 꼼꼼한 가계부 분석 비서야. 부부(형준/윤희)의 가계 데이터를 분석한다.

오늘 날짜: {today}

{DB_SCHEMA}

[월 예산 현황]
{budget_context}

[규칙]
- 질문에 답하기 위해 반드시 query_database 도구로 필요한 데이터를 먼저 조회해
- 질문 범위에 딱 맞는 쿼리를 작성해 (불필요한 데이터 로딩 금지)
- 이체(tx_type='이체')는 항상 WHERE 조건에서 제외해
- 기간이 명시되지 않으면 이번 달 기준으로 조회해
- 금액은 원 단위 정수야. 지출은 음수(-), 수입은 양수(+)로 저장됨
- 지출 금액 크기 비교·정렬 시 반드시 ABS(amount) 또는 -amount를 사용해
  예) 가장 큰 지출: ORDER BY ABS(amount) DESC  /  지출 합계: SUM(ABS(amount))
- 조회 결과가 없으면 기간·카테고리 조건 등 가능한 원인을 추론해서 알려줘
- 답변의 금액은 반드시 천 단위 구분자와 '원' 단위를 붙여서 표기해 (예: 1,234,000원)
- 답변은 친근하고 명확하게 한국어로 해줘
"""

    # 8-1. 슬라이딩 윈도우: 최근 10턴만 전달 (system 메시지는 항상 포함)
    recent_history = chat_history[-10:] if len(chat_history) > 10 else chat_history

    messages = [
        {"role": "system", "content": system_prompt},
        *recent_history,
    ]

    max_iterations = 5  # 무한 루프 방지
    try:
        for _ in range(max_iterations):
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
            )
            response_message = response.choices[0].message

            # tool_call이 없으면 최종 답변 반환
            if not response_message.tool_calls:
                return response_message.content

            # tool_call 실행: 요청된 쿼리를 모두 처리하고 결과를 messages에 추가
            messages.append(response_message)
            for tool_call in response_message.tool_calls:
                if tool_call.function.name == "query_database":
                    args = json.loads(tool_call.function.arguments)
                    sql = args.get("sql", "")
                    query_result = execute_query_safe(sql)

                    # 8-3. SQL 오류 복구: 오류 시 GPT가 수정 쿼리를 재시도하도록 안내
                    if query_result.startswith("쿼리 실행 오류:"):
                        query_result = (
                            f"[SQL 실행 실패 — 쿼리를 수정해서 다시 시도해줘]\n"
                            f"실패한 SQL:\n{sql}\n"
                            f"오류 내용: {query_result}"
                        )

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": query_result,
                    })

        return "죄송해요, 데이터 조회가 너무 복잡해서 답변을 완성하지 못했어요. 질문을 조금 더 구체적으로 해주시겠어요?"

    except Exception as e:
        return f"AI 응답 중 오류가 발생했습니다: {str(e)}"
