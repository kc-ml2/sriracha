"""파이프라인 오케스트레이션 — cron 진입점.

    python -m sriracha.run            # 정상 실행
    python -m sriracha.run --dry-run  # 시트/라벨 안 건드리고 추출만 출력

각 메일은 독립적으로 try/except 로 격리한다. 한 건이 실패해도 나머지는 진행하고,
실패한 메일은 done 마킹을 하지 않으므로 다음 cron 주기에 자동 재시도된다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .extract import SourceExtractor
from .gmail_client import GmailClient
from .llm import LLMClient
from .sheets_client import SheetsClient
from .store import Store

log = logging.getLogger("sriracha")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("sriracha.log", encoding="utf-8"),
        ],
    )


def run(cfg: Config, dry_run: bool = False) -> int:
    gmail = GmailClient(cfg)
    extractor = SourceExtractor(cfg)
    llm = LLMClient(cfg)
    store = Store(cfg.db_path)
    sheets = None if dry_run else SheetsClient(cfg)

    processed = 0
    try:
        ids = [mid for mid in gmail.search() if not store.is_message_done(mid)]
        log.info("미처리 후보 메일 %d건 (query=%r)", len(ids), cfg.gmail_query)
        if not ids:
            return 0

        # Phase 0 (순차): 메일 본문/첨부 가져오기.
        # Gmail API(httplib2)는 스레드 안전하지 않아 순차로 처리한다.
        sources_list = []
        for mid in ids:
            try:
                msg = gmail.fetch(mid)
                log.info("가져옴: %s | %s", mid, msg.subject)
                sources_list.append(extractor.extract(msg))
            except Exception as e:
                log.exception("메일 가져오기 실패 (%s): %s", mid, e)
                store.mark_failed(mid, f"fetch: {e}")

        # Phase 1 (병렬): LLM 추출. vLLM 이 동시에 못 받으면 큐에 쌓여 대기할 뿐.
        results = _extract_parallel(llm, sources_list, cfg.concurrency)

        # Phase 2 (순차): 시트 입력 / 라벨 / store 기록. 공유 상태는 여기서만 건드린다.
        processed = 0
        for sources, receipt, error in results:
            try:
                _apply(cfg, gmail, sheets, store, sources, receipt, error, dry_run)
                if receipt is not None:
                    processed += 1
            except Exception as e:
                log.exception("반영 실패 (%s): %s", sources.message_id, e)
                store.mark_failed(sources.message_id, str(e))
    finally:
        store.close()

    log.info("완료: %d건 처리", processed)
    return processed


def _extract_parallel(llm, sources_list, concurrency):
    """각 메일을 병렬로 LLM 추출. (sources, receipt|None, error|None) 리스트 반환."""
    results = [None] * len(sources_list)

    def work(i, sources):
        if not sources.has_content:
            return i, sources, None, "empty"
        try:
            receipt = llm.extract(sources)
            log.info(
                "추출 결과: %s | is_receipt=%s refund=%s date=%s vendor=%s amount=%s no=%s conf=%.2f",
                sources.message_id, receipt.is_receipt, receipt.is_refund, receipt.date,
                receipt.vendor, receipt.signed_amount, receipt.receipt_no, receipt.confidence,
            )
            return i, sources, receipt, None
        except Exception as e:
            log.warning("추출 최종 실패 (%s): %s", sources.message_id, e)
            return i, sources, None, str(e)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(work, i, s) for i, s in enumerate(sources_list)]
        for fut in as_completed(futures):
            i, sources, receipt, error = fut.result()
            results[i] = (sources, receipt, error)

    return results


def _apply(cfg, gmail, sheets, store, sources, receipt, error, dry_run) -> None:
    """추출 결과를 시트/라벨/store 에 반영 (순차 단계)."""
    mid = sources.message_id

    # 내용 없음 → skip
    if error == "empty":
        log.info("내용 없음 → skip: %s", mid)
        if not dry_run:
            store.mark_skipped(mid, "empty")
            gmail.add_label(mid, cfg.done_label)
        return

    # 추출 실패 → done 마킹 안 함 (다음 cron 때 재시도)
    if receipt is None:
        if not dry_run:
            store.mark_failed(mid, error or "unknown")
        return

    if dry_run:
        return

    # 영수증 아님 → 시트에 안 넣고 다시 안 보게 마킹
    if not receipt.is_receipt:
        store.mark_skipped(mid, "not_receipt")
        gmail.add_label(mid, cfg.done_label)
        return

    # 금액 없는 결제확인(예: 'Welcome to Max plan') → 시트에 안 넣음
    if receipt.amount is None:
        log.info("금액 없음 → skip: %s", mid)
        store.mark_skipped(mid, "no_amount")
        gmail.add_label(mid, cfg.done_label)
        return

    # 같은 영수증번호가 이미 들어갔으면 중복 → skip
    if store.receipt_no_exists(receipt.receipt_no):
        log.info("중복 영수증번호 → skip: %s", receipt.receipt_no)
        store.mark_skipped(mid, "duplicate_receipt_no")
        gmail.add_label(mid, cfg.done_label)
        return

    tab, sheet_row = sheets.insert_receipt(receipt)
    store.mark_done(mid, receipt, sheet_row)
    gmail.add_label(mid, cfg.done_label)
    log.info("시트 입력 완료: %s [%s] row=%s", mid, tab, sheet_row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sriracha — 영수증 메일 → 구글 시트")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="시트/라벨/DB 변경 없이 추출 결과만 출력",
    )
    args = parser.parse_args()

    setup_logging()
    cfg = Config.load()
    run(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
