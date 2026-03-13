"""
InAsset Ingest API (Step 9-2)
뱅크샐러드 Excel/ZIP 파일을 HTTP POST로 수신하여 DB에 저장합니다.
실행: uvicorn src.api.ingest:app --host 0.0.0.0 --port 3102
"""
import calendar
import datetime
import io
import os
import shutil

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from src.utils.db_handler import (
    get_existing_refined_mappings,
    has_transactions_in_range,
    mark_file_processed,
    save_asset_snapshot,
    save_transactions,
    sync_categories_from_transactions,
)
from src.utils.file_handler import (
    DOCS_DIR,
    apply_direct_category,
    extract_date_range,
    extract_snapshot_date,
    process_uploaded_excel,
    process_uploaded_zip,
)

UPDATED_DIR = os.path.join(DOCS_DIR, "updated")
from src.utils.ai_agent import STANDARD_CATEGORIES

app = FastAPI(title="InAsset Ingest API")

INGEST_SECRET = os.environ.get("INGEST_SECRET", "")


# ---------------------------------------------------------------------------
# 텔레그램 알림 (9-6)
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    """Telegram Bot API로 알림 발송. 환경변수 미설정 시 무시."""
    import httpx

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 인증 헬퍼
# ---------------------------------------------------------------------------

def _resolve_actual_range(
    owner: str, start_date: datetime.date, end_date: datetime.date
) -> tuple[datetime.date, datetime.date]:
    """기존 데이터가 있으면 최근 2개월만, 없으면 전체 기간 처리."""
    if has_transactions_in_range(owner, str(start_date), str(end_date)):
        month = end_date.month - 2
        year = end_date.year
        if month <= 0:
            month += 12
            year -= 1
        day = min(end_date.day, calendar.monthrange(year, month)[1])
        return datetime.date(year, month, day), end_date
    return start_date, end_date


def _verify_secret(x_ingest_secret: str | None):
    """INGEST_SECRET 환경변수가 설정된 경우 헤더 값과 비교."""
    if not INGEST_SECRET:
        return  # 미설정 시 인증 생략 (개발 환경)
    if x_ingest_secret != INGEST_SECRET:
        raise HTTPException(status_code=401, detail="인증 실패: X-Ingest-Secret 불일치")


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------

@app.post("/api/ingest")
async def ingest(
    file: UploadFile = File(...),
    owner: str = Form(...),
    password: str = Form(default=""),
    x_ingest_secret: str | None = Header(default=None),
):
    """
    뱅크샐러드 Excel/ZIP 파일을 수신하여 GPT 없이 직접 DB에 저장합니다.

    Headers:
        X-Ingest-Secret: <INGEST_SECRET>

    Form fields:
        file    : Excel (.xlsx) 또는 AES-ZIP 파일
        owner   : 형준 | 윤희 | 공동
        password: ZIP 비밀번호 (ZIP 파일인 경우, 선택)
    """
    _verify_secret(x_ingest_secret)

    filename = file.filename or "unknown"

    # 소유자 검증
    if owner not in ("형준", "윤희", "공동"):
        raise HTTPException(
            status_code=400,
            detail="owner는 형준 / 윤희 / 공동 중 하나여야 합니다.",
        )

    # 파일명에서 날짜 추출 후 실제 처리 범위 결정 (기존 데이터 있으면 최근 2개월)
    start_date_str, end_date_str = extract_date_range(filename)
    snapshot_date = extract_snapshot_date(filename)

    file_start = datetime.date.fromisoformat(start_date_str) if start_date_str else datetime.date.today().replace(day=1)
    file_end = datetime.date.fromisoformat(end_date_str)
    actual_start, actual_end = _resolve_actual_range(owner, file_start, file_end)

    # 파일 읽기 — 빈 파일이면 n8n이 재시도하도록 400 반환
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=400,
            detail="파일 데이터가 비어있습니다. 잠시 후 재시도해주세요.",
        )
    file_bytes = io.BytesIO(content)

    # 파싱 (실제 처리 범위로 필터링)
    try:
        if filename.lower().endswith(".zip"):
            tx_df, asset_df, error = process_uploaded_zip(
                file_bytes, password, actual_start, actual_end
            )
        else:
            tx_df, asset_df, error = process_uploaded_excel(
                file_bytes, actual_start, actual_end
            )
    except Exception as e:
        send_telegram(
            f"❌ InAsset 자동 업데이트 실패\n"
            f"📎 파일명: {filename}\n"
            f"🔴 오류: 파싱 예외 — {e}"
        )
        raise HTTPException(status_code=500, detail=f"파일 파싱 중 예외: {e}")

    if error:
        send_telegram(
            f"❌ InAsset 자동 업데이트 실패\n"
            f"📎 파일명: {filename}\n"
            f"🔴 오류: {error}"
        )
        raise HTTPException(status_code=422, detail=error)

    inserted_tx = 0
    inserted_asset = 0
    new_tx_count = 0
    unclassified_count = 0

    if tx_df is not None and not tx_df.empty:
        # 1. category_1 → refined_category_1 직통 복사
        tx_df = apply_direct_category(tx_df)

        # 2. 기존 DB 재분류값 조회 후 덮어쓰기 (수동 분류 보존)
        desc_col = '내용' if '내용' in tx_df.columns else None
        cat_col  = '대분류' if '대분류' in tx_df.columns else None
        if desc_col and cat_col:
            date_strs = tx_df['날짜'].dt.strftime('%Y-%m-%d') if hasattr(tx_df['날짜'], 'dt') else tx_df['날짜'].astype(str).str[:10]
            pairs = list(zip(date_strs, tx_df[desc_col], tx_df[cat_col]))
            existing_map = get_existing_refined_mappings(pairs)
            tx_df['_date_str'] = date_strs.values
            tx_df['refined_category_1'] = tx_df.apply(
                lambda r: existing_map.get((r['_date_str'], r[desc_col], r[cat_col]), r['refined_category_1']),
                axis=1,
            )
            has_existing = tx_df.apply(lambda r: (r['_date_str'], r[desc_col], r[cat_col]) in existing_map, axis=1)
            not_standard = ~tx_df['refined_category_1'].isin(STANDARD_CATEGORIES)
            new_tx_count = int((~has_existing).sum())
            unclassified_count = int((~has_existing & not_standard).sum())
            tx_df = tx_df.drop(columns=['_date_str'])
        else:
            new_tx_count = len(tx_df)

        inserted_tx = save_transactions(tx_df, owner=owner, filename=filename)

    if asset_df is not None and not asset_df.empty:
        inserted_asset = save_asset_snapshot(
            asset_df, owner=owner, snapshot_date=snapshot_date
        )

    sync_categories_from_transactions()

    # 처리 완료 기록
    mark_file_processed(filename, owner=owner, snapshot_date=snapshot_date, status="new")

    # docs/ 폴더에 파일이 있으면 updated/로 이동
    src_path = os.path.join(DOCS_DIR, filename)
    if os.path.exists(src_path):
        os.makedirs(UPDATED_DIR, exist_ok=True)
        dst_path = os.path.join(UPDATED_DIR, filename)
        if os.path.exists(dst_path):
            stem, ext = os.path.splitext(filename)
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            dst_path = os.path.join(UPDATED_DIR, f"{stem}_{ts}{ext}")
        shutil.move(src_path, dst_path)

    period_str = f"{actual_start} ~ {actual_end}"
    unclassified_line = (
        f"\n⚠️ 표준 카테고리 미해당 {unclassified_count}건 — 앱에서 카테고리 정규화를 진행해주세요"
        if unclassified_count > 0 else ""
    )
    send_telegram(
        f"✅ InAsset 자동 업데이트 완료\n\n"
        f"👤 소유자: {owner}\n"
        f"📅 기간: {period_str}\n"
        f"📥 거래 저장: {inserted_tx}건 (신규 {new_tx_count}건)\n"
        f"🏦 자산 저장: {inserted_asset}건"
        f"{unclassified_line}"
    )

    return {
        "ok": True,
        "inserted": inserted_tx,
        "assets": inserted_asset,
        "period": period_str,
        "filename": filename,
    }


@app.get("/health")
def health():
    return {"status": "ok"}
