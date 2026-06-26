# 🌶️ Sriracha

영수증 메일을 읽어 구글 스프레드시트에 `날짜 | 내역 | 통화 | 금액 | 원화 | 영수증번호`로 자동 입력하는 봇.
추론은 **직접 운영하는 vLLM**(멀티모달, OpenAI 호환)에서 하므로 영수증 데이터가 외부 LLM 서비스로 나가지 않는다. vLLM 서버는 로컬이든 사내 네트워크든 상관없다.

## 동작 방식

```
cron ──> python -m sriracha.run
  1. Gmail 미처리 메일 검색 (-label:sriracha/done)
  2. 본문 텍스트 / 이미지·PDF 첨부 → LLM 입력 소스로 정규화
     (PDF는 PyMuPDF로 페이지 렌더, 이미지는 리사이즈 후 base64)
  3. vLLM 호출 → {is_receipt, date, vendor, currency, amount, receipt_no, is_refund, ...}
     - 영수증 여부도 LLM이 판별 (라벨에만 의존하지 않음)
     - 여러 메일을 동시(병렬) 추출
  4. 중복 확인 (message-id + receipt_no)
  5. 영수증 연도로 탭 선택(없으면 자동 생성) → 날짜 정렬 위치에 행 삽입
     - 원화 컬럼은 '=금액*$환율셀' 수식, 환불은 음수로 기록
  6. Gmail에 sriracha/done 라벨 + JSON 파일에 기록
```

실패한 메일은 `done` 마킹을 하지 않으므로 **다음 cron 주기에 자동 재시도**된다 (cron 자체가 재시도 메커니즘). 한 메일의 실패가 나머지를 막지 않는다.

## 설치

```bash
python -m venv venv        # 기존 venv 사용 가능 (Python 3.10+)
./venv/bin/pip install -e .
cp .env.example .env       # 값 채우기
```

## 설정

### 1. Google OAuth 자격증명
1. [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트 생성
2. **Gmail API**, **Google Sheets API** 사용 설정
3. OAuth 동의 화면 구성 → **데스크톱 앱** OAuth 클라이언트 생성
4. 받은 JSON을 `credentials.json`으로 저장
5. 토큰 발급 (브라우저 동의 1회):
   ```bash
   ./venv/bin/python -m sriracha.auth
   ```
   → `token.json` 생성됨 (이후 자동 갱신)

### 2. `.env` 주요 항목
| 변수 | 설명 |
|---|---|
| `SRIRACHA_SPREADSHEET_ID` | 대상 스프레드시트 ID (URL의 `/d/<ID>/` 부분) |
| `SRIRACHA_SHEET_TAB` | 데이터 탭 이름. `{year}` 넣으면 연도별 탭 자동 라우팅/생성 |
| `SRIRACHA_SHEET_TITLE` | 탭 자동생성 시 1행 제목 |
| `SRIRACHA_DEFAULT_RATE` | 탭 자동생성 시 환율셀 초기값 |
| `SRIRACHA_FIRST_DATA_ROW` | 첫 데이터 행 (헤더 다음) |
| `SRIRACHA_COLUMNS` | 컬럼 순서. 키: `date,vendor,currency,amount,amount_krw,receipt_no` |
| `SRIRACHA_RATE_CELL` | 기준환율이 든 시트 셀 (예: `J1`). 원화 수식이 절대참조로 사용 |
| `SRIRACHA_GMAIL_QUERY` | 검색 쿼리. 키워드/`from:`/`list:` 등으로 좁힐 수 있음 |
| `SRIRACHA_VLLM_BASE_URL` | vLLM OpenAI 호환 엔드포인트 |
| `SRIRACHA_MODEL` | 비우면 `/v1/models`에서 자동 선택. 특정 모델 강제 시 id 지정 |
| `SRIRACHA_CONCURRENCY` | 동시 LLM 요청 수 (vLLM `max-num-seqs`와 맞춤) |

> **모델명은 비워두는 것을 권장** — vLLM에 올라온 모델을 자동으로 사용한다.

### 3. vLLM 띄우기 (예시)
```bash
vllm serve <멀티모달-모델> --port 8000
```
멀티모달(vision) 입력을 지원하는 모델이어야 이미지/PDF 영수증을 처리할 수 있다.

## 실행

```bash
# 시트/라벨 건드리지 않고 추출 결과만 확인 (초기 검증용)
./venv/bin/python -m sriracha.run --dry-run

# 실제 실행
./venv/bin/python -m sriracha.run
```

## cron 등록

영수증은 실시간성이 필요 없으니 1시간 주기면 충분하다 (`crontab -e`):
```cron
# 매시간 7분에 실행
7 * * * * cd /path/to/sriracha && ./venv/bin/python -m sriracha.run >> sriracha.log 2>&1
```
vLLM이 꺼져 있거나 일부가 실패해도 그 회차만 건너뛰고 다음 회차에 자동 재시도된다.

## 중복 방지

- **Gmail 라벨** `sriracha/done` — 처리한 메일에 부착 (Gmail에서 눈으로 확인 가능)
- **로컬 JSON** `sriracha_store.json` — message-id 기록 (라벨이 꼬여도 안전망)
- **영수증번호** — 같은 영수증이 다른 메일로 재발송돼도 중복 입력 방지

세 가지가 독립적으로 동작해 이중·삼중으로 막는다.

기록은 단일 JSON 파일이라 백업/복제가 쉽다 — `cp sriracha_store.json backup.json` 한 번이면 끝이고 사람이 열어봐도 읽힌다. 쓰기는 임시파일+원자적 교체라 도중에 죽어도 파일이 깨지지 않는다.

## 파일 구조

```
sriracha/
  config.py        설정 (.env 로드)
  auth.py          Google OAuth (Gmail + Sheets)
  gmail_client.py  메일 검색 / 본문·첨부 / 라벨
  extract.py       메일 → 텍스트·이미지(base64) 정규화
  llm.py           vLLM 호출 + JSON schema 추출
  store.py         JSON 파일 중복방지 / 처리기록
  sheets_client.py 날짜 위치 행 삽입
  run.py           파이프라인 (cron 진입점)
  models.py        Receipt / MailSources 데이터모델
```

## TODO / 아이디어

- [ ] **추론 백엔드 추상화** — 지금은 vLLM(OpenAI 호환)에 직접 의존. 추론 호출을 인터페이스로 분리해 백엔드를 갈아끼울 수 있게:
  - [ ] vLLM 없이 자체 인퍼런스 구현 (예: `transformers` / `llama.cpp` 직접 로드)
  - [ ] Ollama 등 다른 OpenAI 호환 서버도 그대로 지원
  - [ ] 텍스트 전용 모델 + OCR 경로 (멀티모달 모델이 없을 때)
- [ ] **출력 대상 추상화** — 구글 시트 외에 CSV/DB/노션 등으로 내보내기
- [ ] **환율 자동 조회** — 고정 셀 대신 거래일 기준 환율 API 연동 (선택)
- [ ] **알림** — 처리/실패 결과를 슬랙·메일로 요약 통지
- [ ] **테스트** — 추출/시트/중복방지 단위 테스트 정식화 (현재는 수동 검증)
- [x] **연도별 탭 자동 라우팅/생성** — 영수증 연도로 탭 선택, 없으면 생성
- [ ] **월별 집계** — 월별 합계/리포트 탭
