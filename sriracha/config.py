"""환경변수(.env) 기반 설정 로드."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# .env 를 한 번 로드 (이미 export 된 환경변수가 우선)
load_dotenv(override=False)

# Gmail 라벨 부착을 위해 modify 권한이 필요. Sheets 는 읽기+쓰기.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    # Google OAuth
    credentials_path: Path
    token_path: Path

    # Gmail
    gmail_query: str
    done_label: str
    max_messages: int

    # Sheets
    spreadsheet_id: str
    sheet_tab: str
    first_data_row: int
    columns: list[str]
    rate_cell: str  # 기준환율이 든 시트 셀 (예: H7) — 원화 수식에 절대참조로 사용

    # vLLM
    vllm_base_url: str
    vllm_api_key: str
    model: str  # 빈 문자열이면 /v1/models 에서 자동 선택
    llm_retries: int
    request_timeout: float  # 한 요청 타임아웃(초) — 병렬 큐잉 대비 넉넉히
    concurrency: int  # 동시 LLM 요청 수 (vLLM max-num-seqs 와 맞춤)

    # 로컬 상태 / 추출
    db_path: Path
    image_max_edge: int
    pdf_dpi: int

    @classmethod
    def load(cls) -> "Config":
        return cls(
            credentials_path=Path(os.getenv("SRIRACHA_CREDENTIALS", "credentials.json")),
            token_path=Path(os.getenv("SRIRACHA_TOKEN", "token.json")),
            gmail_query=os.getenv("SRIRACHA_GMAIL_QUERY", "-label:sriracha/done -in:chats"),
            done_label=os.getenv("SRIRACHA_DONE_LABEL", "sriracha/done"),
            max_messages=int(os.getenv("SRIRACHA_MAX_MESSAGES", "20")),
            spreadsheet_id=os.getenv("SRIRACHA_SPREADSHEET_ID", ""),
            sheet_tab=os.getenv("SRIRACHA_SHEET_TAB", "Sheet1"),
            first_data_row=int(os.getenv("SRIRACHA_FIRST_DATA_ROW", "2")),
            columns=_split_csv(
                os.getenv("SRIRACHA_COLUMNS", "date,vendor,currency,amount,amount_krw,receipt_no")
            ),
            rate_cell=os.getenv("SRIRACHA_RATE_CELL", "H7").strip(),
            vllm_base_url=os.getenv("SRIRACHA_VLLM_BASE_URL", "http://localhost:8000/v1"),
            vllm_api_key=os.getenv("SRIRACHA_VLLM_API_KEY", "EMPTY"),
            model=os.getenv("SRIRACHA_MODEL", "").strip(),
            llm_retries=int(os.getenv("SRIRACHA_LLM_RETRIES", "2")),
            request_timeout=float(os.getenv("SRIRACHA_REQUEST_TIMEOUT", "600")),
            concurrency=int(os.getenv("SRIRACHA_CONCURRENCY", "4")),
            db_path=Path(os.getenv("SRIRACHA_DB", "sriracha_store.json")),
            image_max_edge=int(os.getenv("SRIRACHA_IMAGE_MAX_EDGE", "1600")),
            pdf_dpi=int(os.getenv("SRIRACHA_PDF_DPI", "170")),
        )

    def require_spreadsheet(self) -> str:
        if not self.spreadsheet_id:
            raise ValueError(
                "SRIRACHA_SPREADSHEET_ID 가 설정되지 않았습니다. .env 를 확인하세요."
            )
        return self.spreadsheet_id
