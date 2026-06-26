"""로컬 JSON 파일 기반 처리 기록 / 중복 방지.

단일 JSON 파일(dict: message_id → 레코드)이라 백업/복제가 쉽다 (cp 한 번, git 추적 가능).
쓰기는 임시파일 + os.replace 로 원자적 — 도중에 죽어도 기존 파일이 깨지지 않는다.

Gmail 라벨과 함께 이중으로 중복 입력을 막는다.
- message_id: 같은 메일을 두 번 처리하지 않음
- receipt_no: 같은 영수증이 다른 메일로 재발송돼도 중복 입력 방지 (보조키)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class Store:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.records: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.records = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # 손상 시 백업해두고 새로 시작
                self.path.replace(self.path.with_suffix(self.path.suffix + ".bak"))
                self.records = {}

    def close(self) -> None:  # 파일 백엔드라 별도 정리 불필요 (인터페이스 호환용)
        pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── 조회 ──────────────────────────────────────────────────
    def is_message_done(self, message_id: str) -> bool:
        """이미 최종 처리(done/skipped)된 메일인지. failed 는 재시도 대상이라 False."""
        rec = self.records.get(message_id)
        return bool(rec) and rec.get("status") in ("done", "skipped")

    def receipt_no_exists(self, receipt_no: str | None) -> bool:
        """같은 영수증번호가 이미 시트에 들어갔는지 (done 상태만)."""
        if not receipt_no:
            return False
        return any(
            r.get("receipt_no") == receipt_no and r.get("status") == "done"
            for r in self.records.values()
        )

    def attempts(self, message_id: str) -> int:
        rec = self.records.get(message_id)
        return int(rec.get("attempts", 0)) if rec else 0

    # ── 기록 ──────────────────────────────────────────────────
    def mark_done(self, message_id: str, receipt, sheet_row: int | None) -> None:
        self._upsert(
            message_id,
            status="done",
            is_receipt=True,
            date=receipt.date,
            vendor=receipt.vendor,
            amount=receipt.amount,
            receipt_no=receipt.receipt_no,
            sheet_row=sheet_row,
            error=None,
        )

    def mark_skipped(self, message_id: str, reason: str = "not_receipt") -> None:
        """영수증이 아니거나 중복이라 시트에 안 넣고 더 볼 필요 없는 메일."""
        self._upsert(message_id, status="skipped", is_receipt=False, error=reason)

    def mark_failed(self, message_id: str, error: str) -> None:
        """실패 — done 마킹 안 하고 다음 cron 때 재시도. attempts 증가."""
        self._upsert(
            message_id, status="failed", error=error, attempts=self.attempts(message_id) + 1
        )

    def _upsert(self, message_id: str, **fields) -> None:
        rec = self.records.get(message_id, {})
        rec.update(fields)
        rec["message_id"] = message_id
        rec["processed_at"] = time.time()
        rec.setdefault("attempts", self.attempts(message_id))
        self.records[message_id] = rec
        self._flush()

    def _flush(self) -> None:
        """원자적 쓰기: 같은 디렉토리에 임시파일 쓰고 교체."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
