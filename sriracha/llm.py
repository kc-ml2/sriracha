"""vLLM(OpenAI 호환) 호출 — 영수증 판별 + 구조화 추출.

한 번의 호출로 영수증 여부 판별과 {날짜, 내역, 금액, 영수증번호} 추출을 같이 한다.
모델명은 /v1/models 에서 자동 선택(config 에 명시하면 우선).
"""

from __future__ import annotations

import json
import logging
import time

from openai import OpenAI

from .config import Config
from .models import MailSources, Receipt

log = logging.getLogger(__name__)

# 영수증 한 건의 스키마 (한 메일에 여러 건일 수 있어 배열로 감싼다)
RECEIPT_ITEM = {
    "type": "object",
    "properties": {
        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "vendor": {
            "type": "string",
            "description": "발행처 회사명 + 서비스/품목. 예: 'Anthropic - Claude Max 구독'.",
        },
        "amount": {
            "type": ["number", "null"],
            "description": "총 결제/환불 금액. 항상 양수 절대값으로(부호 없이). 없으면 null.",
        },
        "currency": {
            "type": "string",
            "description": "통화 코드. 예: USD, KRW. 모르면 빈 문자열.",
        },
        "receipt_no": {
            "type": "string",
            "description": "영수증/거래/승인 번호 (메일 식별자 아님). 없으면 빈 문자열.",
        },
        "is_refund": {
            "type": "boolean",
            "description": "환불(Refund)이면 true, 일반 결제면 false",
        },
        "confidence": {"type": "number"},
    },
    "required": ["vendor", "currency", "amount", "receipt_no", "is_refund", "confidence"],
    "additionalProperties": False,
}

# vLLM guided output 용 — 메일 한 통의 영수증 목록
RECEIPTS_SCHEMA = {
    "type": "object",
    "properties": {
        "receipts": {
            "type": "array",
            "items": RECEIPT_ITEM,
            "description": "이 메일에 들어있는 영수증 목록. 영수증/결제내역이 없으면 빈 배열.",
        }
    },
    "required": ["receipts"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
당신은 영수증/결제내역 메일을 분석해 회계용 데이터를 추출하는 도우미입니다.
주어진 메일 본문과 첨부 이미지(영수증 PDF가 여러 장일 수 있음)를 보고,
이 메일에 든 모든 영수증을 receipts 배열로 추출하세요.

규칙:
- 첨부나 본문에 여러 건의 결제/환불이 있으면 각각을 별도 항목으로 넣습니다.
  (예: PDF 영수증 3장이면 보통 3개 항목, 날짜·금액이 각자 다를 수 있음)
- 영수증/결제내역이 전혀 없으면(광고, 뉴스레터, 안내, 로그인 링크 등) receipts 를 빈 배열 [] 로 둡니다.
- 각 항목에 대해:
  - date 는 거래(결제/환불) 발생일을 YYYY-MM-DD 로. 영수증에 날짜가 없으면 메일 수신일을 사용합니다.
  - amount 는 통화기호/콤마를 뺀 총 금액의 숫자만(부분금액 아닌 최종 합계).
    환불이어도 음수로 쓰지 말고 양수 절대값으로 적고, is_refund 로 구분합니다.
  - is_refund 는 환불(Refund/Refunded)이면 true, 일반 결제(Paid/Receipt)면 false.
  - vendor 는 반드시 채우되 "발행처 회사명 + 서비스/품목" 형식으로.
    예: Anthropic 의 Claude Max 구독 영수증이면 "Anthropic - Claude Max 구독".
  - currency 는 통화 코드(USD, KRW 등). receipt_no 는 영수증·거래·승인 번호(없으면 빈 문자열).
  - confidence 는 확신 정도(0~1).
- 반드시 지정된 JSON 스키마로만 답하세요."""


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # 병렬로 보내면 vLLM 큐에 쌓여 응답이 늦어질 수 있으므로 타임아웃을 넉넉히.
        self.client = OpenAI(
            base_url=cfg.vllm_base_url,
            api_key=cfg.vllm_api_key,
            timeout=cfg.request_timeout,
            max_retries=0,  # 재시도는 우리가 직접 관리
        )
        self._model: str | None = cfg.model or None

    @property
    def model(self) -> str:
        """모델명 — config 우선, 없으면 /v1/models 에서 자동 선택(첫 모델)."""
        if self._model:
            return self._model
        models = self.client.models.list()
        if not models.data:
            raise RuntimeError("vLLM /v1/models 가 비어 있습니다. 서버를 확인하세요.")
        self._model = models.data[0].id
        log.info("모델 자동선택: %s", self._model)
        return self._model

    def extract(self, sources: MailSources) -> list[Receipt]:
        """메일에서 영수증 목록 추출 (0건이면 영수증 아님). 실패 시 예외(상위서 failed 처리)."""
        messages = self._build_messages(sources)
        last_err: Exception | None = None

        for attempt in range(1, self.cfg.llm_retries + 2):  # 최초 + 재시도
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "receipts",
                            "schema": RECEIPTS_SCHEMA,
                        },
                    },
                    # Qwen3 계열의 사고과정(thinking) 출력을 끈다 → JSON 누수/속도 개선.
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                msg = resp.choices[0].message
                data = json.loads(_extract_json(msg.content))
                items = data.get("receipts", [])
                return [
                    Receipt.from_json(it, source_msg_id=sources.message_id) for it in items
                ]
            except Exception as e:
                last_err = e
                log.warning("LLM 추출 실패 (시도 %d): %s", attempt, e)
                time.sleep(min(2 * attempt, 5))

        raise RuntimeError(f"LLM 추출 {self.cfg.llm_retries + 1}회 실패: {last_err}")

    def _build_messages(self, sources: MailSources) -> list[dict]:
        text = (
            f"[메일 제목] {sources.subject}\n"
            f"[메일 수신일] {sources.received_date}\n"
            f"[본문]\n{sources.body_text or '(본문 없음)'}\n"
        )
        content: list[dict] = [{"type": "text", "text": text}]
        for img in sources.images:
            content.append(
                {"type": "image_url", "image_url": {"url": img.data_url()}}
            )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]


def _extract_json(content: str | None) -> str:
    """모델 응답에서 JSON 본문만 뽑아낸다.

    thinking 을 꺼도 코드펜스(```json)나 앞뒤 잡텍스트가 섞일 수 있어,
    마크다운 펜스를 제거하고 첫 '{' ~ 마지막 '}' 구간만 취한다.
    """
    if not content or not content.strip():
        raise ValueError("빈 응답 (content 없음 — thinking 누수 가능성)")
    text = content.strip()
    # ```json ... ``` 펜스 제거
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.lstrip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    # 가장 바깥 객체만
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"JSON 객체를 찾지 못함: {content[:80]!r}")
    return text[start : end + 1]
