"""Google OAuth 인증 — Gmail + Sheets 공용 자격증명.

최초 1회 브라우저 동의로 token.json 을 생성하고, 이후에는 자동 refresh 한다.

    python -m sriracha.auth      # 토큰 발급/갱신
"""

from __future__ import annotations

import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import SCOPES, Config

log = logging.getLogger(__name__)


def get_credentials(cfg: Config) -> Credentials:
    """유효한 OAuth 자격증명 반환. 필요 시 refresh 하거나 새로 발급한다."""
    creds: Credentials | None = None

    if cfg.token_path.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        log.info("토큰 갱신 중...")
        creds.refresh(Request())
        _save(cfg, creds)
        return creds

    # 새로 발급 (브라우저 동의 필요)
    if not cfg.credentials_path.exists():
        raise FileNotFoundError(
            f"OAuth 클라이언트 파일이 없습니다: {cfg.credentials_path}\n"
            "Google Cloud Console에서 데스크톱 앱 OAuth 클라이언트를 만들고 "
            "credentials.json 으로 저장하세요."
        )

    log.info("브라우저 동의를 진행합니다...")
    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    _save(cfg, creds)
    return creds


def _save(cfg: Config, creds: Credentials) -> None:
    cfg.token_path.write_text(creds.to_json())
    log.info("토큰 저장: %s", cfg.token_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = Config.load()
    creds = get_credentials(cfg)
    print(f"✅ 인증 완료. 토큰: {cfg.token_path} (유효: {creds.valid})")


if __name__ == "__main__":
    main()
