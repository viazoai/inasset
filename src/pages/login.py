import bcrypt
import re

import streamlit as st
import yaml


def render(authenticator, config, config_path):
    """미인증 상태의 로그인/회원가입 UI를 렌더링한다. 인증 전까지 이후 실행을 차단한다."""

    # 사이드바 및 토글 버튼 숨김 + 로그인 폼 스타일
    st.markdown("""
        <style>
        [data-testid="stSidebar"],
        [data-testid="collapsedControl"] { display: none !important; }

        /* 폼 타이틀 가운데 정렬 + 링크 아이콘 숨김 */
        [data-testid="stHeadingWithActionElements"] { text-align: center; }
        [data-testid="stHeaderActionElements"] { display: none; }

        /* 로그인 버튼 가로 꽉 채우기 */
        [data-testid="stElementContainer"]:has([data-testid="stFormSubmitButton"]),
        [data-testid="stFormSubmitButton"],
        [data-testid="stBaseButton-secondaryFormSubmit"] { width: 100% !important; }
        </style>
    """, unsafe_allow_html=True)

    _, col, _ = st.columns([1.25, 1, 1.25])
    with col:
        st.markdown("""
            <div class="login-header">
                <div style="font-size:3.5rem;">🏛️</div>
                <p style="color:#2575fc; font-size:2rem; font-weight:700; margin:0.25rem 0;">InAsset</p>
                <p style="opacity:0.65; margin:0;">우리 부부의 스마트 자산 관리자</p>
            </div>
        """, unsafe_allow_html=True)

        tab_login, tab_register = st.tabs(["로그인", "회원가입"])

        with tab_login:
            authenticator.login(
                location='main',
                captcha=False,
                fields={
                    'Username': '이메일',
                    'Password': '비밀번호',
                    'Login': '로그인',
                },
            )

            # 폼 아래에 메시지 표시
            if st.session_state.get('authentication_status') is True:
                st.rerun()
            elif st.session_state.get('authentication_status') is False:
                st.error("이메일 또는 비밀번호가 올바르지 않습니다.")

            if st.session_state.pop('_approval_pending', False):
                st.warning("⏳ 관리자 승인 대기 중입니다.")

        with tab_register:
            _render_register_form(config, config_path)

    with col:
        st.markdown(
            "<p style='font-size:0.8rem; opacity:0.4; text-align:center; margin-top:1.5rem;'>v1.0.0 · © 2025 zoai</p>",
            unsafe_allow_html=True,
        )

    if st.session_state.get('authentication_status') is not True:
        st.stop()


def _render_register_form(config, config_path):
    """회원가입 커스텀 폼: 이름·이메일·비밀번호만 입력받고, 신규 계정은 승인 대기 상태로 저장한다."""
    with st.form("custom_register_form"):
        r_name  = st.text_input("이름")
        r_email = st.text_input("이메일")
        r_pw    = st.text_input("비밀번호", type="password")
        r_pw2   = st.text_input("비밀번호 확인", type="password")
        submitted = st.form_submit_button("가입하기", use_container_width=True)

    if not submitted:
        return

    users = config['credentials']['usernames']
    if not r_name.strip():
        st.error("이름을 입력하세요.")
    elif not re.match(r'^[^@]+@[^@]+\.[^@]+$', r_email):
        st.error("올바른 이메일 형식이 아닙니다.")
    elif r_email in users:
        st.error("이미 등록된 이메일입니다.")
    elif len(r_pw) < 4:
        st.error("비밀번호는 4자 이상이어야 합니다.")
    elif r_pw != r_pw2:
        st.error("비밀번호가 일치하지 않습니다.")
    else:
        users[r_email] = {
            'name': r_name.strip(),
            'email': r_email,
            'password': bcrypt.hashpw(r_pw.encode(), bcrypt.gensalt()).decode(),
            'role': 'user',
            'approved': False,  # 관리자 승인 후 로그인 가능
        }
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        st.success(f"✅ '{r_name.strip()}' 계정이 생성되었습니다. 관리자 승인 후 로그인이 가능합니다.")
