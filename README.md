# 생산일정 재편성 콘솔

OR-Library `ft06` 공정 데이터로 생산일정을 수립하고, 설비 고장·긴급주문이 생기면
잔여 일정을 다시 계산해 납기 영향을 비교하는 웹 시연 시스템.

```
static/index.html  웹 화면 (01 기준정보 ~ 05 AI 분석)
server.py          웹 서버 — 화면을 내려주고 계산 API 를 제공 (Starlette)
scheduler.py       일정 수립·재편성 엔진 (OR-Tools CP-SAT + 휴리스틱)
agent.py           Claude API tool calling 에이전트 (+ 오프라인 대체)
parser.py          OR-Library jobshop1.txt 파서 (82 instances)
data/jobshop1.txt  OR-Library 원본 데이터
app.py             Streamlit 화면 (이전 버전, 그대로 실행 가능)
```

화면은 브라우저에서, 계산은 서버에서 합니다.
서버에 닿지 않으면(파일을 직접 열었거나 연결이 끊김) 화면이 **브라우저 안의 휴리스틱으로 자동 전환**해서
시연 흐름이 끊기지 않습니다. 오른쪽 위 표시등이 `서버 연결 · CP-SAT`(초록)인지 `오프라인`(빨강)인지로 구분합니다.

---

## 1. 내 PC 에서 실행

Python 3.11 또는 3.12 를 쓰세요. (3.9 이하는 `parser.py` 가 표준 모듈 이름과 겹쳐 오류가 날 수 있습니다.)

```powershell
cd C:\Users\dksgu\Desktop\production-rescheduler\rescheduler
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --reload
```

브라우저에서 **http://localhost:8000** 을 엽니다.

`data/jobshop1.txt` 가 없으면 처음 실행할 때 OR-Library 에서 받습니다. 외부 통신이 막혀 있으면
아래 주소에서 직접 받아 `data/` 에 넣으세요. 파일이 없으면 화면에 내장된 5개 인스턴스로만 동작합니다.

```
https://people.brunel.ac.uk/~mastjjb/jeb/orlib/files/jobshop1.txt
```

## 2. Claude API 키 — 서버 비밀값으로만 둔다

키는 **서버 환경변수 `ANTHROPIC_API_KEY`** 에만 둡니다. (Supabase 의 `secrets set` 과 같은 역할)
`server.py` 의 `/api/agent` 가 실행될 때만 그 값을 꺼내 Claude 를 부르고, 응답에는 Claude 답변과 도구 결과만 담깁니다.
웹 코드(`static/index.html`)와 GitHub 저장소에는 키가 없고, 개발자 도구로 통신을 열어 봐도 보이지 않습니다.
서버에 키가 있으면 화면의 [API 설정]에 키 입력란이 아예 나타나지 않습니다.

**배포 서버** — Render 대시보드의 Environment 에 넣습니다 (3장). 웹 화면에서 입력하므로 터미널 기록에 남지 않습니다.

**내 PC** — 아래처럼 `Read-Host` 로 받으면 키가 PowerShell 입력 기록에 남지 않습니다.

```powershell
$env:ANTHROPIC_API_KEY = Read-Host "Claude API 키"     # 프롬프트에 붙여넣기
uvicorn server:app --reload
```

`$env:ANTHROPIC_API_KEY="sk-ant-..."` 처럼 한 줄에 직접 쳤다면 PowerShell 입력 기록 파일에 키가 평문으로 남습니다.
지우려면 (기록 전체가 지워집니다):

```powershell
Remove-Item (Get-PSReadLineOption).HistorySavePath
```

**신경 쓸 점은 키 노출이 아니라 "남이 내 키로 질문하는 것"입니다.** 로그인 없는 공개 사이트라서 누구나 화면을 열고 질문할 수 있고, 그만큼 사용료가 나갑니다.
- **월 사용 한도 (꼭)**: console.anthropic.com 의 Billing 에서 Spend limit 을 낮게(예: 월 $5~10) 걸어 두세요. 넘으면 자동으로 멈춥니다.
- **서버에 들어 있는 제한**: 다른 사이트에서 온 요청 거절 · JSON 요청만 받음 · IP당 10분에 30회(`AGENT_RATE_LIMIT`) · 메시지 1000자 · 도구 호출 6회 · 답변 1600토큰.
- **접속 코드 (선택)**: `APP_PASSCODE` 를 넣으면 화면 [API 설정]에 그 코드를 넣은 사람만 Claude 를 부를 수 있습니다.

키가 없으면 **규칙 기반 모드**로 자동 전환됩니다. 같은 도구를 같은 방식으로 호출하고 설명만 템플릿으로 만듭니다.
(서버에 키가 없을 때만 [API 설정]에 개인 키를 넣어 시험할 수 있고, 그 키는 탭 메모리에만 있다가 새로고침하면 지워집니다.)

기본 모델은 `claude-sonnet-5-5` 입니다. [API 설정]에서 `claude-opus-5-5`, `claude-haiku-5-5` 로 바꿀 수 있습니다.

---

## 3. 배포 (Render 무료 플랜)

1. 이 폴더(`rescheduler`) 내용을 GitHub 저장소 **맨 위**에 올립니다.
   `static/`, `data/jobshop1.txt`, `.python-version` 이 같이 올라가야 합니다.
2. render.com 가입 → **New → Web Service** → GitHub 저장소 연결 → 아래처럼 입력합니다.

   | 항목 | 값 |
   |---|---|
   | Runtime | Python 3 |
   | Build Command | `pip install -r requirements-server.txt` |
   | Start Command | `uvicorn server:app --host 0.0.0.0 --port $PORT` |
   | Instance Type | Free |

3. **Environment** 에 변수를 넣습니다.
   - `ANTHROPIC_API_KEY` = `sk-ant-...`
   - `APP_PASSCODE` = 아무 접속 코드 (권장). 배포 주소는 누구나 열 수 있어서,
     이걸 넣어 두면 서버 키로 Claude 를 부를 때 화면 [API 설정]에서 이 코드를 넣어야 합니다.
4. **Deploy** → 몇 분 뒤 `https://<이름>.onrender.com` 주소가 생깁니다.

`render.yaml` 이 들어 있어서 **New → Blueprint** 로 저장소를 골라도 같은 설정으로 만들어집니다.
이후 `git push` 하면 자동으로 다시 배포됩니다.

**무료 플랜 주의**
- 15분 동안 접속이 없으면 잠들고, 다시 깨우는 데 1분쯤 걸립니다. **발표 10분 전에 주소를 한 번 열어 두세요.**
- CPU 가 작습니다(0.1 CPU). 큰 인스턴스나 납기지연 목적에서는 CP-SAT 가 제한시간 안에 최적을 증명하지 못해
  `제한시간 내 최선해`로 표시될 수 있습니다. 그때는 01 화면의 **제한 시간**을 20~30초로 늘리세요.
- 환경변수 값은 Render 에 로그인한 본인만 볼 수 있습니다. 키를 바꾸면 Render 가 서비스를 다시 시작합니다.

선택 환경변수: `AGENT_RATE_LIMIT`(AI 분석 한도, 기본 `30/600` = 10분에 30회), `SOLVE_RATE_LIMIT`(일정 계산 한도, 기본 `300/600`),
`CLAUDE_MODEL`(기본 모델), `CPSAT_WORKERS`(CP-SAT 병렬 수, 기본 8), `HEURISTIC_BUDGET`(휴리스틱 시간 상한 초, 기본 5).

**왜 Supabase Edge Function 이 아닌가.** 키를 숨기는 방식은 같지만, Edge Function 은 Deno(TypeScript) 환경이라
Python OR-Tools(CP-SAT)를 돌릴 수 없습니다. 그래서 계산과 Claude 호출을 한 Python 서버에 둡니다.

---

## 4. 계산이 맞는지 확인하는 법

`02 초기 생산일정 수립` 화면에서 목적함수를 **완료시간 최소**로 바꾸면
`ft06` 의 makespan 이 **55** 로 나오고 `최적해 증명됨` · `문헌 최적 55 대비 일치 ✅` 가 표시됩니다.
ft06 의 알려진 최적값이 55이므로 이게 맞으면 모델이 맞는 겁니다. `la01` 로 바꿔도 666 (문헌 최적값) 이 나옵니다.

| | 총 지연 | makespan | 계산 시간 |
|---|---|---|---|
| CP-SAT (최적해 증명) | **25** | 60 | 0.2초 |
| 휴리스틱 (GRASP 1500회) | 33 | 76 | 0.5초 |

규모가 커지면 CP-SAT 가 제한 시간 안에 최적을 증명하지 못할 수 있습니다.
그때는 `제한시간 내 최선해` 로 표시되고, 계산 엔진을 휴리스틱으로 바꾸면 바로 답이 나옵니다.
`브라우저 계산 (서버 미연결)` 이 보이면 서버에 닿지 않은 것입니다.

---

## 5. 시연 시나리오 (이대로 고정해서 연습하세요)

1. `01 주문·설비 기준정보` — ft06, k=1.3, 계산 엔진 CP-SAT 확인. 오른쪽 위 `서버 연결 · CP-SAT` 확인.
2. `02 초기 생산일정 수립` — 간트와 납기 준수율 확인.
   목적함수를 **완료시간 최소**로 한 번 바꿔 **55** 를 보여주고 **납기지연 최소**로 되돌린다. (신뢰도 확보용)
3. `03 돌발상황 일정 재편성` — 기준시각 t=10, M2 를 10시부터 14시간 고장 등록 → **잔여 일정 재계산**.
   자동으로 `04 변경 전후 일정 비교` 로 넘어가 지연 막대그래프와 변경 전후 간트를 보여준다.
4. `05 일정 변경 영향 분석` — "3호기가 12시부터 15시간 멈췄어. 어떤 주문이 위험한지 알려줘."
   **도구 호출 N건**을 펼쳐서 **LLM 이 계산한 게 아니라 도구를 부른 것**임을 보여준다.
5. "원래 일정으로 되돌려줘." 로 복원.

이 흐름을 3번 연속 돌려 오류가 없는지 확인하세요.

### 잘못된 입력에 대한 처리

| 입력 | 결과 |
|---|---|
| 설비 번호 범위 초과 | `설비 번호는 0~5 사이여야 합니다` |
| 종료 ≤ 시작 / 중단 시간 0 | `고장 종료가 시작보다 뒤여야 합니다` / `중단 시간은 1 이상이어야 합니다` |
| 납기 ≤ 투입 시각 | `납기가 투입 시각보다 뒤여야 합니다` |
| 중단 시간이 전체 작업량보다 김 | 경고 표시 (계산은 됨) |
| 고장 구간이 착수 중인 작업과 겹침 | 작업 완료 후로 고장 시작을 자동 조정하고 안내 |
| API 키 오류 · 모델 없음 | AI 분석 화면에 원인 표시, 키를 비우면 규칙 기반으로 동작 |

마지막 두 개가 중요합니다. 고장 시간을 아무리 길게 넣어도 CP-SAT 는
"고장 끝난 뒤"로 일정을 밀어 **항상 해를 찾습니다.** 불능이 안 나므로
입력 검증에서 막지 않으면 조용히 이상한 결과가 나옵니다.

---

## 6. Streamlit 버전 (app.py)

이전 화면도 그대로 실행됩니다.

```powershell
streamlit run app.py
```

Streamlit Community Cloud 에 올릴 때는 메인 파일을 `app.py` 로 지정하고 Secrets 에 `ANTHROPIC_API_KEY` 를 넣으세요.

---

## 7. 설계 메모

**서버는 상태를 저장하지 않는다.**
화면이 현재 주문·고장·일정을 요청에 담아 보내고, 서버는 계산 결과만 돌려줍니다.
무료 서버가 잠들었다 깨거나 재시작돼도 화면의 작업 상태가 날아가지 않고, 여러 사람이 같은 주소로 들어와도 서로 섞이지 않습니다.

**API**

| 경로 | 하는 일 |
|---|---|
| `GET /api/health` | 서버 상태, CP-SAT 사용 가능 여부, 서버 키 유무 |
| `GET /api/instances` · `/api/instances/{이름}` | 82개 인스턴스 목록 · 공정 데이터 |
| `POST /api/solve` | 일정 수립. `baseOps`·`now`·`breakdowns` 를 주면 잔여 일정 재편성 |
| `POST /api/agent` | 자연어 → Claude tool calling (키 없으면 규칙 기반) |

**파서 — `instance` 줄 다음은 차원 줄이 아니다.**
원본은 `instance ft06` → *설명 줄* → `6 6` → 데이터 6줄 순서입니다.
82개 인스턴스 전부 설명 줄이 정확히 1줄 있습니다.
"instance 줄 아래 n줄을 읽는다"로 짜면 전부 깨집니다.
그래서 파서는 **정수 2개만 있는 첫 줄**을 차원 줄로 찾습니다.

**스케줄러 — 왜 CP-SAT 인가.**
변수는 공정별 시작시각, 제약은 공정 순서와 설비 겹침 금지 두 가지입니다.
이건 CP-SAT 의 `NewIntervalVar` + `AddNoOverlap` 에 그대로 들어맞습니다.
설비 고장도 "고정된 구간"을 같은 `NoOverlap` 에 넣으면 끝이고,
기준시각 이전 착수 공정도 상수 구간으로 넣으면 됩니다. 제약을 따로 짤 게 없습니다.
목적함수는 `총 지연 × (horizon+1) + makespan` 으로 두어 지연을 1순위,
완료시간을 2순위로 봅니다(사전식).

**우선순위 가중치.** 지연 합을 그냥 더하지 않고 우선순위별 가중치(긴급 4 / 보통 2 / 여유 1)를
곱해 최소화합니다. 화면에 표시하는 총 지연은 가중치 없는 원래 합입니다.

**휴리스틱은 시간으로 끊는다.** 웹 서버에서는 다중시작을 최대 5초(`HEURISTIC_BUDGET`)까지만 돕니다.
무료 서버 CPU 가 작아서 횟수(1500회)로만 두면 50×10 같은 큰 인스턴스에서 몇 분이 걸립니다.

**에이전트 — LLM 에게 계산을 시키지 않는다.**
`machine_breakdown`, `add_urgent_order` 가 상태를 바꾸는 유일한 도구이고,
실행 주체는 `SchedulingSession` 입니다. LLM 은 도구를 호출하고 반환된 JSON 을 설명만 합니다.
도구가 실패하면 `is_error` 로 모델에 돌려줘 스스로 고쳐 다시 부르게 합니다.
도구 호출은 6회로 제한해 무한 루프를 막습니다.

**원본 데이터의 한계.** OR-Library 는 공정마다 쓸 설비가 하나로 정해져 있습니다.
그래서 재편성은 "같은 설비 안에서 작업 순서를 바꾸는 것"이고, 다른 설비로 옮기는 재배정은 모델에 없습니다.

---

## 출처

공정 데이터: OR-Library `jobshop1.txt`, J.E. Beasley, Brunel University London.
Beasley (1990), "OR-Library: distributing test problems by electronic mail",
*Journal of the Operational Research Society* 41(11), 1069–1072.

`ft06` 원전: H. Fisher & G.L. Thompson (1963), *Industrial Scheduling*,
Prentice Hall, 225–251.

납기·우선순위·긴급주문·설비고장 정보는 원본에 포함되어 있지 않으며, 본 시연에서 설정한 값입니다.
