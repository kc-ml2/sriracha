"""핵심 데이터 모델."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ImageSource:
    """LLM 에 넣을 이미지 한 장 (base64 data URL 용)."""

    mime: str  # 예: "image/png", "image/jpeg"
    b64: str  # base64 인코딩된 바이트
    origin: str  # 출처 설명 (예: "attachment:receipt.pdf#p1")

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.b64}"


@dataclass
class MailSources:
    """메일 한 건에서 추출한 LLM 입력 소스 묶음."""

    message_id: str
    subject: str
    received_date: str  # YYYY-MM-DD (날짜 fallback 힌트)
    body_text: str
    images: list[ImageSource]

    @property
    def has_content(self) -> bool:
        return bool(self.body_text.strip()) or bool(self.images)


@dataclass
class Receipt:
    """LLM 이 추출한 영수증 정보."""

    is_receipt: bool
    date: str | None  # YYYY-MM-DD
    vendor: str | None
    amount: float | None  # 양수 절대값 (부호는 is_refund 로 표현)
    currency: str | None
    receipt_no: str | None
    is_refund: bool  # 환불이면 True → 시트엔 음수로 기록
    confidence: float

    # 추적용 (시트에는 안 들어감)
    source_msg_id: str | None = None

    @classmethod
    def from_json(cls, data: dict, source_msg_id: str | None = None) -> "Receipt":
        def clean(v):  # 빈 문자열/공백/가짜 null 문자열 → None
            if not isinstance(v, str):
                return None
            s = v.strip()
            return None if not s or s.lower() in ("null", "none", "n/a", "없음") else s

        return cls(
            is_receipt=bool(data.get("is_receipt", False)),
            date=clean(data.get("date")),
            vendor=clean(data.get("vendor")),
            amount=data.get("amount"),
            currency=clean(data.get("currency")),
            receipt_no=clean(data.get("receipt_no")),
            is_refund=bool(data.get("is_refund", False)),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            source_msg_id=source_msg_id,
        )

    @property
    def signed_amount(self) -> float | None:
        """원래 통화 기준, 부호 있는 금액. 환불이면 음수."""
        if self.amount is None:
            return None
        magnitude = abs(self.amount)
        return -magnitude if self.is_refund else magnitude

    @property
    def is_krw(self) -> bool:
        return (self.currency or "").upper() == "KRW"

    def column_value(self, column: str) -> str:
        """시트 컬럼 이름 → 셀 문자열.

        amount_krw 는 시트에서 수식으로 채우므로 여기선 빈 문자열을 돌려준다
        (sheets_client 가 삽입 후 '=금액*$환율셀' 수식으로 덮어쓴다).
        """
        if column == "date":
            return _to_sheet_date(self.date)
        if column == "vendor":
            return self.vendor or ""
        if column == "currency":
            return (self.currency or "").upper()
        if column == "amount":
            amt = self.signed_amount
            return "" if amt is None else _num(amt)
        if column == "amount_krw":
            return ""
        if column == "receipt_no":
            return self.receipt_no or ""
        return ""


def _to_sheet_date(date: str | None) -> str:
    """'YYYY-MM-DD' → 'MM월 DD일' (기존 시트 형식). 형식이 다르면 원본 유지."""
    if not date:
        return ""
    parts = date.split("-")
    if len(parts) == 3 and all(parts):
        return f"{parts[1]}월 {parts[2]}일"
    return date


def _num(value: float) -> str:
    """정수면 소수점 제거 (200.0 → '200')."""
    return str(int(value)) if float(value).is_integer() else str(value)
