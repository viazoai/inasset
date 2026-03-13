import os

import streamlit as st
import yaml
from dotenv import load_dotenv
from yaml.loader import SafeLoader

import streamlit_authenticator as stauth

from utils.db_handler import _init_db, get_latest_transaction_date
from pages import login

# 1. 페이지 설정 (반드시 첫 번째)
st.set_page_config(page_title="InAsset", layout="wide", page_icon="🏛️")

# 2. DB 및 환경변수 초기화
_init_db()
load_dotenv()

# 3. 전역 CSS 주입
st.markdown("""
    <style>
    /* 페이지 상단 여백 축소 */
    .block-container { padding-top: 1.5rem !important; }

    /* 사이드바 페이지 링크 가운데 정렬 + 간격 */
    [data-testid="stSidebar"] [data-testid="stPageLink"] {
        margin: 0.1rem 0;
    }
    [data-testid="stSidebar"] [data-testid="stPageLink"] a {
        justify-content: center;
        padding: 0.4rem 1rem;
        font-weight: 600;
    }

    /* 사이드바 메뉴 버튼 */
    [data-testid="stSidebar"] .stButton > button {
        width: 100%;
        border-radius: 10px;
        border: 1px solid rgba(128, 128, 128, 0.35);
        background-color: var(--background-color);
        color: var(--text-color);
        padding: 0.5rem 1rem;
        transition: all 0.3s ease;
        text-align: left;
        display: flex;
        align-items: center;
        justify-content: flex-start;
    }
    [data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #6a11cb 0%, #2575fc 100%);
        color: white;
        border: none;
        box-shadow: 0 4px 15px rgba(0,0,0,0.1);
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        border-color: #2575fc;
        color: #2575fc;
        transform: translateY(-2px);
    }

    /* 사이드바 서버 상태 카드 */
    .server-status {
        padding: 10px;
        border-radius: 8px;
        background-color: rgba(128, 128, 128, 0.1);
        border-left: 5px solid #2196f3;
        font-size: 0.8rem;
        color: var(--text-color);
    }

    /* 로그인 화면 헤더 (login.py에서 사용) */
    .login-header {
        text-align: center;
        padding: 3rem 0 1.5rem;
    }
    </style>
    """, unsafe_allow_html=True)

# 4. config.yaml 로드
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.yaml')

if not os.path.exists(_CONFIG_PATH):
    st.error("⚠️ 인증 설정 파일(config.yaml)이 없습니다.")
    st.info("아래 명령으로 초기 비밀번호를 설정한 후 앱을 재시작하세요.")
    st.code("python scripts/init_auth.py", language="bash")
    st.stop()

with open(_CONFIG_PATH, encoding='utf-8') as _f:
    _config = yaml.load(_f, Loader=SafeLoader)

_authenticator = stauth.Authenticate(
    _CONFIG_PATH,
    _config['cookie']['name'],
    _config['cookie']['key'],
    _config['cookie']['expiry_days'],
)

# 5. 페이지 정의 + 라우트 등록 — 인증 전에 먼저 호출해야 로그아웃 후 새로고침 시 "Page not found" 방지
_all_pages = [
    st.Page("pages/analysis.py",       title="📊 분석 리포트",    url_path="analysis", default=True),
    st.Page("pages/chatbot.py",         title="🤖 가계부 AI 비서", url_path="chatbot"),
    st.Page("pages/transactions.py",    title="💰 수입/지출 현황", url_path="transactions"),
    st.Page("pages/assets.py",          title="🏦 자산 현황",      url_path="assets"),
    st.Page("pages/budget.py",          title="🎯 목표 예산",      url_path="budget"),
    st.Page("pages/data_management.py", title="📂 데이터 관리",    url_path="data"),
]
pg = st.navigation(_all_pages, position="hidden")

# 6. 미인증 상태 → 로그인/회원가입 화면 (인증 전까지 이후 실행 차단)
if st.session_state.get('authentication_status') is not True:
    login.render(_authenticator, _config, _CONFIG_PATH)

# 6. 승인 여부 확인 — 인증 방식(폼/쿠키)과 무관하게 항상 통과해야 함
#    login.render() 내부에서 authenticator가 st.rerun()을 호출하더라도
#    다음 rerun에서 이 체크가 실행되어 미승인 계정을 차단한다.
_username = st.session_state.get('username', '')
_user_data = _config['credentials']['usernames'].get(_username, {})

if not _user_data.get('approved', True):
    try:
        _authenticator.cookie_controller.delete_cookie()
    except Exception:
        pass
    for _k in ['authentication_status', 'username', 'name', 'email', 'roles']:
        st.session_state.pop(_k, None)
    st.session_state['_approval_pending'] = True
    st.rerun()

# ─────────────────────────────────────────────────────────────
# 이하: 인증 + 승인된 사용자만 접근 가능
# ─────────────────────────────────────────────────────────────
_role = _user_data.get('role', 'user')
st.session_state['role'] = _role

# 9. 사이드바 표시용 페이지 목록 (admin만 데이터 관리 노출)
_pages = _all_pages if _role == 'admin' else _all_pages[:-1]

# 사이드바 구성
with st.sidebar:
    st.markdown("<h1 style='text-align: center; color: #2575fc; font-size: 2rem;'>🏛️ InAsset</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; font-size: 1rem; opacity: 0.7;'>우리 부부의 스마트 자산 관리자</p>", unsafe_allow_html=True)
    _latest_date = get_latest_transaction_date()
    if _latest_date:
        st.markdown(
            f"<p style='text-align: center; font-size: 0.8rem; opacity: 0.5; margin: 0;'>📅 Updated: {_latest_date}</p>",
            unsafe_allow_html=True,
        )
    st.markdown("---")

    # 네비게이션 메뉴 (타이틀 ~ 사용자 정보 사이)
    for _p in _pages:
        st.page_link(_p, use_container_width=True)

    st.markdown("---")

    # 하단: 사용자명 위 / 로그아웃 버튼 아래
    _name = st.session_state.get('name', _username)
    _role_label = '관리자' if _role == 'admin' else '사용자'
    st.markdown(
        f"<p style='font-size:0.85rem; opacity:0.8; margin:0.4rem 0 0.2rem;'>"
        f"👤 {_name} ({_role_label})</p>",
        unsafe_allow_html=True,
    )

    if st.button("로그아웃", use_container_width=True):
        try:
            _authenticator.cookie_controller.delete_cookie()
        except Exception:
            pass
        st.session_state.clear()
        st.rerun()

    # 관리자 전용: 승인 대기 계정 관리 (하단 아래)
    if _role == 'admin':
        _pending = {
            email: data
            for email, data in _config['credentials']['usernames'].items()
            if not data.get('approved', True)
        }
        if _pending:
            with st.expander(f"⚠️ 승인 대기 {len(_pending)}명", expanded=True):
                for _pu_email, _pu_data in _pending.items():
                    st.caption(f"{_pu_data['name']} · {_pu_email}")
                    _col_approve, _col_reject = st.columns(2)
                    if _col_approve.button("승인", key=f"approve_{_pu_email}", use_container_width=True):
                        _config['credentials']['usernames'][_pu_email]['approved'] = True
                        with open(_CONFIG_PATH, 'w', encoding='utf-8') as _wf:
                            yaml.dump(_config, _wf, allow_unicode=True, default_flow_style=False)
                        st.rerun()
                    if _col_reject.button("거절", key=f"reject_{_pu_email}", use_container_width=True):
                        del _config['credentials']['usernames'][_pu_email]
                        with open(_CONFIG_PATH, 'w', encoding='utf-8') as _wf:
                            yaml.dump(_config, _wf, allow_unicode=True, default_flow_style=False)
                        st.rerun()

# 10. 페이지 실행
pg.run()
