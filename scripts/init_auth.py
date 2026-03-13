#!/usr/bin/env python3
"""
InAsset 인증 초기화 스크립트

실행 (로컬):
    python scripts/init_auth.py

실행 (Docker 컨테이너 내부):
    docker exec -it <container_name> python scripts/init_auth.py

비밀번호를 입력하면 bcrypt 해시가 생성되고 config.yaml이 저장됩니다.
"""
import sys
import os
import getpass
import secrets

import yaml


def _hash_password(password: str) -> str:
    try:
        import bcrypt
    except ImportError:
        print("❌ bcrypt를 찾을 수 없습니다. pip install streamlit-authenticator 를 먼저 실행하세요.")
        sys.exit(1)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def main():
    print("=== InAsset 인증 설정 초기화 ===\n")

    users = {}

    for username, name, role in [
        ("형준", "형준", "admin"),
        ("윤희", "윤희", "user"),
    ]:
        role_label = "관리자" if role == "admin" else "사용자"
        print(f"[{name} ({role_label}) 계정]")

        while True:
            pw = getpass.getpass("  비밀번호 입력: ")
            if len(pw) < 4:
                print("  ❌ 비밀번호는 4자 이상이어야 합니다.\n")
                continue
            pw_confirm = getpass.getpass("  비밀번호 확인: ")
            if pw == pw_confirm:
                users[username] = {
                    "email": "",
                    "name": name,
                    "password": _hash_password(pw),
                    "role": role,
                }
                print(f"  ✅ {name} 계정 설정 완료\n")
                break
            else:
                print("  ❌ 비밀번호가 일치하지 않습니다. 다시 입력하세요.\n")

    config = {
        "credentials": {
            "usernames": users,
        },
        "cookie": {
            "expiry_days": 30,
            "key": secrets.token_hex(32),  # 랜덤 서명 키 자동 생성
            "name": "inasset_auth",
        },
    }

    # 스크립트 위치(scripts/) 기준으로 프로젝트 루트 찾기
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(project_root, "config.yaml")

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

    print(f"✅ config.yaml 생성 완료: {config_path}")
    print("이제 앱을 재시작하면 로그인 화면이 활성화됩니다.")


if __name__ == "__main__":
    main()
