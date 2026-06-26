"""Google Sheets 래퍼 — 날짜 정렬 위치에 영수증 행 삽입.

행 형식은 config.columns 순서(기본 date, vendor, amount, receipt_no)를 따른다.
기존 날짜 컬럼을 읽어 새 영수증 날짜가 들어갈 위치를 계산해 그 자리에 삽입하고,
계산이 애매하면 맨 아래 append 로 폴백한다.
"""

from __future__ import annotations

import logging
import string

from googleapiclient.discovery import build

from .auth import get_credentials
from .config import Config
from .models import Receipt

log = logging.getLogger(__name__)


class SheetsClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        creds = get_credentials(cfg)
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.spreadsheet_id = cfg.require_spreadsheet()
        self._sheet_id: int | None = None

    # ── 메타 ──────────────────────────────────────────────────
    def _tab_sheet_id(self) -> int:
        if self._sheet_id is not None:
            return self._sheet_id
        meta = self.svc.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        for sh in meta.get("sheets", []):
            if sh["properties"]["title"] == self.cfg.sheet_tab:
                self._sheet_id = sh["properties"]["sheetId"]
                return self._sheet_id
        raise ValueError(f"탭을 찾을 수 없습니다: {self.cfg.sheet_tab}")

    def _tab_ref(self) -> str:
        """range 용 탭 참조. 공백/한글 탭명을 작은따옴표로 감싼다."""
        return "'" + self.cfg.sheet_tab.replace("'", "''") + "'"

    def _date_col_letter(self) -> str:
        idx = self.cfg.columns.index("date") if "date" in self.cfg.columns else 0
        return string.ascii_uppercase[idx]

    def _last_col_letter(self) -> str:
        return string.ascii_uppercase[len(self.cfg.columns) - 1]

    # ── 삽입 위치 계산 ────────────────────────────────────────
    def _insert_row_index(self, date: str) -> int | None:
        """date(YYYY-MM-DD) 가 들어갈 0-based 행 인덱스. None 이면 append."""
        col = self._date_col_letter()
        first = self.cfg.first_data_row
        rng = f"{self._tab_ref()}!{col}{first}:{col}"
        resp = (
            self.svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=rng)
            .execute()
        )
        values = resp.get("values", [])
        dates = [(row[0] if row else "") for row in values]

        # 오름차순 정렬 가정: date 보다 큰 첫 행 앞에 삽입
        for i, d in enumerate(dates):
            if d and d > date:
                return (first - 1) + i  # 0-based sheet row index
        return None  # 끝에 append

    # ── 삽입 ──────────────────────────────────────────────────
    def insert_receipt(self, receipt: Receipt) -> int:
        """영수증 행을 삽입하고 1-based 행 번호를 반환."""
        row_values = [receipt.column_value(c) for c in self.cfg.columns]

        insert_idx = None
        if receipt.date:
            try:
                # 시트의 날짜도 'MM월 DD일' 형식이므로 같은 형식 문자열로 비교한다.
                insert_idx = self._insert_row_index(receipt.column_value("date"))
            except Exception as e:
                log.warning("삽입 위치 계산 실패, append 로 폴백: %s", e)

        if insert_idx is None:
            row = self._append(row_values)
        else:
            row = self._insert_at(insert_idx, row_values)

        self._set_krw_formula(row, receipt)
        return row

    def _set_krw_formula(self, row: int, receipt: Receipt) -> None:
        """원화 컬럼에 '=금액*$환율셀' 수식을 넣는다 (KRW면 환율 곱 없이 금액 그대로)."""
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
            range=f"{self._tab_ref()}!{krw_col}{row}",
            valueInputOption="USER_ENTERED",
            body={"values": [[formula]]},
        ).execute()

    def _insert_at(self, row_index: int, row_values: list[str]) -> int:
        sheet_id = self._tab_sheet_id()
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
                        {
                            "values": [
                                {"userEnteredValue": _cell(v)} for v in row_values
                            ]
                        }
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
        return row_index + 1  # 1-based

    def _append(self, row_values: list[str]) -> int:
        last_col = self._last_col_letter()
        rng = f"{self._tab_ref()}!A{self.cfg.first_data_row}:{last_col}"
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
    """'H7' → '$H$7' (절대참조). 행 삽입/이동에도 환율셀 참조가 안 흔들리게."""
    import re

    m = re.match(r"([A-Za-z]+)(\d+)", cell.strip())
    if not m:
        return cell
    return f"${m.group(1).upper()}${m.group(2)}"


def _cell(value: str) -> dict:
    """문자열을 적절한 userEnteredValue 로. 숫자처럼 보이면 숫자로."""
    if value == "":
        return {"stringValue": ""}
    try:
        num = float(value)
        return {"numberValue": num}
    except ValueError:
        return {"stringValue": value}


def _row_from_range(a1: str) -> int:
    """'Sheet1!A5:D5' → 5. 실패하면 -1."""
    import re

    m = re.search(r"![A-Z]+(\d+)", a1)
    return int(m.group(1)) if m else -1
