"""메일 → LLM 입력 소스 정규화.

본문 텍스트는 그대로, 이미지/PDF 첨부는 base64 이미지로 변환한다.
(멀티모달 모델 전제. 추후 텍스트 전용 모델로 교체 시 여기에 OCR 경로를 끼우면 된다.)
"""

from __future__ import annotations

import base64
import io
import logging

import fitz  # PyMuPDF
from PIL import Image

from .config import Config
from .gmail_client import Attachment, Message
from .models import ImageSource, MailSources

log = logging.getLogger(__name__)


class SourceExtractor:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def extract(self, msg: Message) -> MailSources:
        images: list[ImageSource] = []
        for att in msg.attachments:
            try:
                if att.mime == "application/pdf":
                    images.extend(self._pdf_to_images(att))
                elif att.mime.startswith("image/"):
                    img = self._image_attachment(att)
                    if img:
                        images.append(img)
            except Exception as e:  # 첨부 하나가 깨져도 나머지는 진행
                log.warning("첨부 처리 실패 (%s): %s", att.filename, e)

        return MailSources(
            message_id=msg.id,
            subject=msg.subject,
            received_date=msg.received_date,
            body_text=msg.body_text,
            images=images,
        )

    # ── PDF → 페이지 이미지 ───────────────────────────────────
    def _pdf_to_images(self, att: Attachment) -> list[ImageSource]:
        out: list[ImageSource] = []
        zoom = self.cfg.pdf_dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        with fitz.open(stream=att.data, filetype="pdf") as doc:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=matrix)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                out.append(
                    self._encode(img, f"attachment:{att.filename}#p{i + 1}")
                )
        return out

    # ── 이미지 첨부 ───────────────────────────────────────────
    def _image_attachment(self, att: Attachment) -> ImageSource | None:
        img = Image.open(io.BytesIO(att.data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        return self._encode(img, f"attachment:{att.filename}")

    # ── 공통 인코딩 (리사이즈 + PNG base64) ───────────────────
    def _encode(self, img: Image.Image, origin: str) -> ImageSource:
        max_edge = self.cfg.image_max_edge
        if max(img.size) > max_edge:
            ratio = max_edge / max(img.size)
            new_size = (round(img.width * ratio), round(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return ImageSource(mime="image/png", b64=b64, origin=origin)
