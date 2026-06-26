"""Gmail API 래퍼 — 메일 검색, 본문/첨부 추출, 라벨 부착."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from googleapiclient.discovery import build

from .auth import get_credentials
from .config import Config

log = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    mime: str
    data: bytes


@dataclass
class Message:
    id: str
    subject: str
    received_date: str  # YYYY-MM-DD (메일 헤더 기준)
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)


class GmailClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        creds = get_credentials(cfg)
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._label_cache: dict[str, str] = {}

    # ── 검색 ──────────────────────────────────────────────────
    def search(self, query: str | None = None, max_results: int | None = None) -> list[str]:
        """쿼리에 맞는 메일 id 목록 반환 (최신순)."""
        query = query if query is not None else self.cfg.gmail_query
        max_results = max_results or self.cfg.max_messages
        ids: list[str] = []
        page_token = None
        while len(ids) < max_results:
            resp = (
                self.svc.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(100, max_results - len(ids)),
                    pageToken=page_token,
                )
                .execute()
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids[:max_results]

    # ── 메일 한 건 가져오기 ───────────────────────────────────
    def fetch(self, message_id: str) -> Message:
        msg = (
            self.svc.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        subject = headers.get("subject", "(제목 없음)")
        received = _received_date(msg, headers)

        body_parts: list[str] = []
        attachments: list[Attachment] = []
        self._walk(payload, message_id, body_parts, attachments)

        return Message(
            id=message_id,
            subject=subject,
            received_date=received,
            body_text="\n".join(p for p in body_parts if p.strip()),
            attachments=attachments,
        )

    def _walk(self, part: dict, message_id: str, body: list[str], attachments: list[Attachment]) -> None:
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body_meta = part.get("body", {})

        # 멀티파트 → 재귀
        for sub in part.get("parts", []) or []:
            self._walk(sub, message_id, body, attachments)

        # 첨부 (이미지/PDF)
        if filename and (mime.startswith("image/") or mime == "application/pdf"):
            data = self._attachment_bytes(message_id, body_meta)
            if data:
                attachments.append(Attachment(filename=filename, mime=mime, data=data))
            return

        # 본문 텍스트 (plain 우선, html 은 fallback)
        if mime == "text/plain" and body_meta.get("data"):
            body.append(_decode(body_meta["data"]))
        elif mime == "text/html" and body_meta.get("data") and not body:
            body.append(_strip_html(_decode(body_meta["data"])))

    def _attachment_bytes(self, message_id: str, body_meta: dict) -> bytes | None:
        if body_meta.get("data"):
            return base64.urlsafe_b64decode(body_meta["data"])
        att_id = body_meta.get("attachmentId")
        if not att_id:
            return None
        att = (
            self.svc.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=att_id)
            .execute()
        )
        return base64.urlsafe_b64decode(att["data"])

    # ── 라벨 ──────────────────────────────────────────────────
    def label_id(self, name: str) -> str:
        """라벨 id 반환. 없으면 생성."""
        if name in self._label_cache:
            return self._label_cache[name]
        labels = self.svc.users().labels().list(userId="me").execute().get("labels", [])
        for lb in labels:
            if lb["name"] == name:
                self._label_cache[name] = lb["id"]
                return lb["id"]
        created = (
            self.svc.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        self._label_cache[name] = created["id"]
        return created["id"]

    def add_label(self, message_id: str, label_name: str) -> None:
        lid = self.label_id(label_name)
        self.svc.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [lid]}
        ).execute()


# ── 헬퍼 ──────────────────────────────────────────────────────
def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _received_date(msg: dict, headers: dict) -> str:
    """메일 수신 날짜 (YYYY-MM-DD). internalDate(ms) 우선."""
    internal = msg.get("internalDate")
    if internal:
        dt = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    # fallback: Date 헤더 파싱 생략, 오늘 날짜
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
