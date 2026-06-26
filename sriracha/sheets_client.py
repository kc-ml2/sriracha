"""Google Sheets 래퍼 — 연도별 탭 라우팅 + 날짜 정렬 삽입.

- 영수증 날짜의 연도로 탭을 고른다 (config.sheet_tab 에 '{year}' 플레이스홀더).
- 해당 연도 탭이 없으면 표준 레이아웃(제목/헤더/환율셀/서식)으로 자동 생성한다.
- 행은 config.columns 순서로, 날짜 정렬 위치에 삽입(안 되면 append).
- 원화 컬럼은 '=금액*$환율셀' 수식, 환불은 음수.
"""

from __future__ import annotations

import logging
import re
import string

from googleapiclient.discovery import build

from .auth import get_credentials
from .config import Config
from .models import Receipt

log = logging.getLogger(__name__)

# 컬럼 키 → 시트 헤더 라벨 (탭 자동생성 시 사용)
COL_LABELS = {
    "date": "날짜",
    "vendor": "내역",
    "currency": "통화",
    "amount": "금액",
    "amount_krw": "원화",
    "receipt_no": "영수증번호",
}


class SheetsClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        creds = get_credentials(cfg)
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.spreadsheet_id = cfg.require_spreadsheet()
        self._sheet_ids: dict[str, int] = {}  # 탭명 → gid 캐시

    # ── 탭 라우팅/생성 ────────────────────────────────────────
    def _resolve_tab(self, receipt: Receipt) -> str:
        """영수증 날짜의 연도로 탭명 결정."""
        template = self.cfg.sheet_tab
        if "{year}" not in template:
            return template
        if not receipt.date or len(receipt.date) < 4:
            raise ValueError(f"연도 라우팅에 필요한 날짜가 없음: {receipt.date!r}")
        return template.format(year=receipt.date[:4])

    def _tab_ref(self, tab: str) -> str:
        """range 용 탭 참조. 공백/한글 탭명을 작은따옴표로 감싼다."""
        return "'" + tab.replace("'", "''") + "'"

    def _sheet_id(self, tab: str) -> int:
        """탭 gid 반환. 없으면 표준 레이아웃으로 생성."""
        if tab in self._sheet_ids:
            return self._sheet_ids[tab]
        meta = self.svc.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        for sh in meta.get("sheets", []):
            self._sheet_ids[sh["properties"]["title"]] = sh["properties"]["sheetId"]
        if tab not in self._sheet_ids:
            self._sheet_ids[tab] = self._create_tab(tab)
        return self._sheet_ids[tab]

    def _create_tab(self, tab: str) -> int:
        """제목/헤더/환율셀/숫자서식을 갖춘 새 연도 탭 생성."""
        log.info("탭 자동생성: %s", tab)
        resp = self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
        sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        ref = self._tab_ref(tab)
        header_row = self.cfg.first_data_row - 1  # 헤더는 첫 데이터행 바로 위

        data = []
        # 제목 (헤더 위에 여유가 있으면 1행)
        if header_row >= 2:
            data.append({"range": f"{ref}!A1", "values": [[self.cfg.sheet_title]]})
        # 헤더
        labels = [COL_LABELS.get(c, c) for c in self.cfg.columns]
        data.append({"range": f"{ref}!A{header_row}", "values": [labels]})
        # 환율셀 + 라벨
        data.append(
            {"range": f"{ref}!{self.cfg.rate_cell}", "values": [[self.cfg.default_rate]]}
        )
        left = _cell_left(self.cfg.rate_cell)
        if left:
            data.append({"range": f"{ref}!{left}", "values": [["기준환율"]]})
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()

        # 금액/원화 컬럼 천단위 서식
        reqs = []
        for key in ("amount", "amount_krw"):
            if key in self.cfg.columns:
                idx = self.cfg.columns.index(key)
                reqs.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": self.cfg.first_data_row - 1,
                                "startColumnIndex": idx,
                                "endColumnIndex": idx + 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    }
                )
        if reqs:
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": reqs}
            ).execute()
        return sheet_id

    # ── 컬럼 헬퍼 ─────────────────────────────────────────────
    def _date_col_letter(self) -> str:
        idx = self.cfg.columns.index("date") if "date" in self.cfg.columns else 0
        return string.ascii_uppercase[idx]

    def _last_col_letter(self) -> str:
        return string.ascii_uppercase[len(self.cfg.columns) - 1]

    # ── 삽입 위치 계산 ────────────────────────────────────────
    def _insert_row_index(self, tab: str, date: str) -> int | None:
        """date('MM월 DD일')가 들어갈 0-based 행 인덱스. None 이면 append."""
        col = self._date_col_letter()
        first = self.cfg.first_data_row
        rng = f"{self._tab_ref(tab)}!{col}{first}:{col}"
        resp = (
            self.svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=rng)
            .execute()
        )
        values = resp.get("values", [])
        dates = [(row[0] if row else "") for row in values]
        for i, d in enumerate(dates):
            if d and d > date:
                return (first - 1) + i
        return None

    # ── 삽입 ──────────────────────────────────────────────────
    def insert_receipt(self, receipt: Receipt) -> tuple[str, int]:
        """영수증 행을 알맞은 연도 탭에 삽입하고 (탭명, 1-based 행) 반환."""
        tab = self._resolve_tab(receipt)
        self._sheet_id(tab)  # 없으면 생성
        row_values = [receipt.column_value(c) for c in self.cfg.columns]

        insert_idx = None
        if receipt.date:
            try:
                insert_idx = self._insert_row_index(tab, receipt.column_value("date"))
            except Exception as e:
                log.warning("삽입 위치 계산 실패, append 로 폴백: %s", e)

        if insert_idx is None:
            row = self._append(tab, row_values)
        else:
            row = self._insert_at(tab, insert_idx, row_values)

        self._set_krw_formula(tab, row, receipt)
        return tab, row

    def _set_krw_formula(self, tab: str, row: int, receipt: Receipt) -> None:
        """원화 컬럼에 '=금액*$환율셀' 수식 (KRW면 환율 곱 없이 금액 그대로)."""
        if "amount_krw" not in self.cfg.columns or receipt.signed_amount is None:
            return
        amount_col = string.ascii_uppercase[self.cfg.columns.index("amount")]
        krw_col = string.ascii_uppercase[self.cfg.columns.index("amount_krw")]
        if receipt.is_krw:
            formula = f"={amount_col}{row}"
        else:
            formula = f"={amount_col}{row}*{_abs_ref(self.cfg.rate_cell)}"
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self._tab_ref(tab)}!{krw_col}{row}",
            valueInputOption="USER_ENTERED",
            body={"values": [[formula]]},
        ).execute()

    def _insert_at(self, tab: str, row_index: int, row_values: list[str]) -> int:
        sheet_id = self._sheet_id(tab)
        requests = [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_index,
                        "endIndex": row_index + 1,
                    },
                    "inheritFromBefore": row_index > 0,
                }
            },
            {
                "updateCells": {
                    "rows": [
                        {"values": [{"userEnteredValue": _cell(v)} for v in row_values]}
                    ],
                    "fields": "userEnteredValue",
                    "start": {
                        "sheetId": sheet_id,
                        "rowIndex": row_index,
                        "columnIndex": 0,
                    },
                }
            },
        ]
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        ).execute()
        return row_index + 1

    def _append(self, tab: str, row_values: list[str]) -> int:
        last_col = self._last_col_letter()
        rng = f"{self._tab_ref(tab)}!A{self.cfg.first_data_row}:{last_col}"
        resp = (
            self.svc.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=rng,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row_values]},
            )
            .execute()
        )
        updated_range = resp.get("updates", {}).get("updatedRange", "")
        return _row_from_range(updated_range)


def _abs_ref(cell: str) -> str:
    """'J1' → '$J$1' (절대참조). 행 삽입/이동에도 환율셀 참조가 안 흔들리게."""
    m = re.match(r"([A-Za-z]+)(\d+)", cell.strip())
    return f"${m.group(1).upper()}${m.group(2)}" if m else cell


def _cell_left(cell: str) -> str | None:
    """'J1' → 'I1' (왼쪽 셀). A열이면 None."""
    m = re.match(r"([A-Za-z]+)(\d+)", cell.strip())
    if not m:
        return None
    col, rownum = m.group(1).upper(), m.group(2)
    if col == "A":
        return None
    left = chr(ord(col[-1]) - 1)  # 단일 문자 가정 (A~Z)
    return f"{left}{rownum}"


def _cell(value: str) -> dict:
    """문자열을 적절한 userEnteredValue 로. 숫자처럼 보이면 숫자로."""
    if value == "":
        return {"stringValue": ""}
    try:
        return {"numberValue": float(value)}
    except ValueError:
        return {"stringValue": value}


def _row_from_range(a1: str) -> int:
    """'Sheet1!A5:D5' → 5. 실패하면 -1."""
    m = re.search(r"![A-Z]+(\d+)", a1)
    return int(m.group(1)) if m else -1
