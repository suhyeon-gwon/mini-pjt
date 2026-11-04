# 프로젝트 규칙

날씨 기반 아침 옷 추천 에이전트. 무엇을 만드는지는 [SERVICE.md](SERVICE.md), 설계 상세는 [extra.md](docs/extra.md), 기술 패턴은 [docs/lecture.md](docs/lecture.md)를 따른다. 문서끼리 충돌하면 SERVICE.md > extra.md > lecture.md 순서로 따른다.

## 기술 스택
- Python, LangChain, LangGraph, FastMCP (패키지는 requirements.txt에 있는 것만 쓴다. 기상청 HTTP 호출은 표준 라이브러리 `urllib.request`)
- 모델은 Amazon Bedrock `ChatBedrockConverse` (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us-east-1`). 분류·판정·라우팅·가드레일은 `temperature=0`
- Agent 생성은 `langchain.agents`의 `create_agent`, 최상위는 `langgraph_supervisor`의 `create_supervisor`. Supervisor에는 `create_forward_message_tool("supervisor")`를 붙여 추천을 다시 쓰지 않고 원문으로 넘긴다
- 도구는 자체 FastMCP 서버 2개(stdio)로 공급하고 `MultiServerMCPClient`로 붙인다. 그래서 **전부 비동기**다(`ainvoke`, `AsyncSqliteSaver`, `awrap_*`)
- MCP 서버에는 실행 설정(`WEATHER_MODE`, `WEATHER_FIXTURE`, `TODAY_OVERRIDE`)을 서버별 `env`로 명시해 넘긴다(stdio는 환경변수를 다 물려주지 않는다). 설정이 바뀌면(`/date`, 평가 문항별 fixture) `build_app()`을 다시 부른다. 비밀 값은 넘기지 않고 서버가 `.env`를 읽는다
- 구조화 출력은 `with_structured_output`으로 통일한다
- RAG는 착용 기록 검색(`search_wear_history`) 한 곳에만 쓴다. BM25(`kiwipiepy`) + 인메모리 Chroma 하이브리드, 임베딩은 `BedrockEmbeddings`(`amazon.titan-embed-text-v2:0`)
- 날씨는 기상청 단기예보 실API. 미세먼지는 범위 밖이다

## 폴더 구조 (제출 규약. 바꾸지 않는다)
```
src/agent.py                   메인 에이전트 그래프: 하위 Agent 4개 + Supervisor 조립 (build_app)
src/tools.py                   도메인 도구: MCP 서버에서 도구를 불러와 Agent별로 나눈다
src/mcp_weather.py             FastMCP 날씨 서버
src/mcp_wardrobe.py            FastMCP 옷장 서버 (옷장·기록·프로필·일정·추천 기록)
src/weather.py                 기상청 클라이언트, 구간 요약, 체감온도, 플래그
src/wardrobe.py                JSON 저장소, 1차 필터, 비슷한 기온의 날
src/feedback.py                피드백 기록, 프로필 변경 제안값
src/retriever.py               착용 기록 검색(RAG): 검색 문서 생성, 색인 동기화, 조건 필터 + 하이브리드 검색
src/config.py                  llm 팩토리, get_text, 경로 상수, WEATHER_MODE, 날짜 오버라이드(TODAY_OVERRIDE, today()), 상대 날짜 해석
src/guardrails.py              input_guard, output_guard, REFUSAL_MESSAGES, 감사 로그
src/middleware.py              LoggingMiddleware
src/tracer.py                  FileTracer (JSONL, 턴별 도구 호출·결과 수집)
src/chat.py                    CLI 대화 진입점 (HITL 프롬프트), 한 턴 공통 처리 ask()
data/                          clothes, wear_log, profile, calendar, recommendations JSON
data/fixtures/                 평가·오프라인용 날씨 fixture (rainy, cold, mild, error)
evaluation/test_queries.csv    인-아웃 세트
evaluation/run_eval.py         자동 판정 스크립트
evaluation/round1_report.md    1차 자체 평가 결과
evaluation/round2_report.md    2차 자체 평가 결과 (개선 후)
logs/                          trace.jsonl, agent.jsonl, guard_audit.log
SERVICE.md                     서비스 스펙
README.md                      
requirements.txt
run.sh, Dockerfile             선택
```
- 도메인 로직은 `src/weather.py`, `src/wardrobe.py`, `src/feedback.py`에 순수 함수로 둔다. MCP 서버(`src/mcp_*.py`)는 그 함수를 감싸는 얇은 래퍼다
- `src/chat.py`의 `ask()`가 한 턴의 공통 처리를 맡는다: `input_guard` → Supervisor → (최종 응답이 stylist 추천 원문과 다르면 stylist 원문으로 되돌림) → `output_guard` → 추천 응답이면 추천 기록 저장 + 오늘 1번 자동 기록(오늘 기록이 `confirmed`면 건너뜀). HITL 결정은 `ask()`가 인자로 받은 `decide` 함수로 얻는다. 자동 기록과 저장은 Agent 도구를 거치지 않고 도메인 함수를 직접 호출한다. `evaluation/run_eval.py`도 `ask()`를 쓴다
- 착용 기록 검색(RAG) 로직은 `src/retriever.py`에 두고 `src/mcp_wardrobe.py`가 감싼다(extra.md §8.3). 그 밖의 RAG 파일은 만들지 않는다

## 역할 분담 (코드 vs LLM)
- 코드가 한다: 날씨 API 호출, 체감온도·일교차·강수 시간대 계산, 우산·겉옷·바람 플래그, 기온 기준 1차 필터, 비슷한 기온의 날 찾기, 프로필 변경값 계산(±1, -2~+2), 추천 1번 자동 기록, 추천 옷 실재 검증, 추천 기록 저장, 상대 날짜("토요일", "내일") 해석, 착용 기록 검색의 조건 필터·색인 동기화
- LLM이 한다: 후보 안에서 조합(색, 스타일, 일정에 맞는 격식), 최근 입은 옷 피하기, 피드백 해석, 추천 이유 작성, 자연어 옷·일정 정보 파싱
- 계산이나 판정을 LLM 프롬프트로 옮기지 않는다

## 주고받는 형식
- 진입점: `python -m src.chat` (명령 `/date YYYY-MM-DD`, `/quit`), `python evaluation/run_eval.py`. 정해진 시간 자동 실행은 없고, 사용자가 "오늘 뭐 입지?"라고 물으면 그날 추천이 시작된다
- `ask()`는 `{"answer", "contexts", "trace"}` dict를 돌려준다(상세는 extra.md §12.3). `chat.py`는 `answer`만 출력한다
  - `answer`: `output_guard`를 거친 최종 응답 문자열
  - `contexts`: 이번 턴에 실행된 MCP 도구 결과 전체(호출 순서). 항목은 `{"kind": "retrieval" | "tool", "agent", "tool", "content"}`이고 `search_wear_history`만 `retrieval`이다. Supervisor 핸드오프는 넣지 않는다
  - `trace`: 이번 턴의 도구 호출 목록 `{"agent", "tool", "args", "status": "ok" | "error" | "interrupted"}`. HITL interrupt는 `ask()`가 `interrupted`로 넣는다. `expected_tools` 판정은 이것으로 한다
  - `contexts`·`trace`는 `FileTracer`가 모은 이번 턴의 도구 이벤트로 만든다. `input_guard`에서 막히면 둘 다 빈 리스트다
- thread_id는 `{user_id}:{YYYY-MM-DD}`, 기본 user_id는 `me`. 평가는 user_id 자리에 `eval-{문항 id}`를 쓴다(데이터 파일은 user_id로 나누지 않는다)
- 도구 결과는 JSON 문자열이다. 오류는 예외 대신 "무엇이 잘못됐고 어떻게 다시 호출할지"를 담은 문자열로 돌려준다
- 추천 출력: 오늘 날씨 한 줄 → 번호 붙은 후보 2~3개(각각 이유) → `챙길 것:` 한 줄(날씨 플래그 문구 + 운동 일정이면 "운동복", 없으면 "없음"). 옷 이름은 clothes.json의 `name`을 그대로 쓴다
- 옷 추천은 오늘만 한다. `get_today_weather`, `get_candidates`는 날짜 인자가 없다. 다른 날을 물으면 도구를 부르지 않고 오늘 옷만 추천할 수 있다고 안내한다
- 추천 응답 마지막 줄: "다르게 입거나 춥고 더우면 알려주세요. 말씀이 없으면 1번을 입은 것으로 기록해 둘게요." 그날 옷에 대한 피드백은 그날 받는다
- 주요 흐름 (발화 → 담당 Agent → 도구)
  - "오늘 뭐 입지?" → `stylist_agent` → `get_today_weather`, `get_candidates`, `get_calendar`, `get_profile`
  - "오늘 추웠어" / "어제 추웠어" → `log_agent` → `record_feedback`, `update_profile`(HITL)
  - "이 옷 추가해줘" → `closet_agent` → `add_clothing`
  - "이 일정 추가해줘" → `closet_agent` → `add_event`
  - "저번에 비 왔을 때 뭐 입었더라?" → `log_agent` → `search_wear_history`
- HITL 재개 값: `Command(resume={"decisions": [{"type": "approve" | "edit" | "reject", ...}]})` (langchain 1.4.8 형식)
- `WEATHER_MODE=fixture`(+ `WEATHER_FIXTURE=rainy|cold|mild|error`)면 API 대신 fixture를 읽는다. fixture는 기상청 응답 원문 형식이고 `error`는 실패 응답이다. 평가는 항상 fixture로 돌린다
- `evaluation/test_queries.csv` (과제 규약, 상세는 extra.md §13)
  - 컬럼 7개, 순서 고정: `id,category,input,expected_traits,forbidden,expected_tools,note`
  - `category`는 `positive`(40%) / `negative`(20%) / `edge`(25%) / `guardrail`(15%) 중 하나. 현재 45건 = 18 / 9 / 11 / 7
  - 여러 값은 `;`로 구분한다. `expected_tools`에는 실제 도구 이름을 쓴다
  - `forbidden`에서 작은따옴표로 감싼 항목은 문자열 그대로 검사하고, 나머지는 judge가 판정한다
  - `note`는 기본값과 다른 사전 조건이 있으면 `[fixture=..., calendar=..., state=..., hitl=...]`로 시작한다. 없으면 생략하고 기본값을 쓴다
  - 1차·2차 결과는 `evaluation/round1_report.md`, `round2_report.md`에 남긴다

## 코드 규칙
- 파일 하나에 한 가지 역할만 둔다
- 함수와 도구에는 한국어 docstring을 쓴다. 도구 docstring은 무엇 / 언제 쓰는지 / 언제 쓰지 않는지(대신 쓸 도구) / Args 순서로 쓴다
- 도구 인자는 Pydantic으로 검증하고, 허용값은 `Literal`로 제한한다
- 조회 도구와 실행 도구를 구분한다. HITL은 `confirm_wear`, `update_profile`에만 건다(`log_agent`의 `HumanInTheLoopMiddleware`)
- 메시지 텍스트는 항상 `get_text()`로 읽는다(Bedrock content가 블록 리스트일 수 있음)
- 파일 입출력은 `encoding="utf-8"`, JSON은 `ensure_ascii=False, indent=2`. 데이터 파일 쓰기는 임시 파일에 쓴 뒤 교체한다
- 경로는 `os.path.dirname(os.path.abspath(__file__))` 기준으로 잡고, 하위 프로세스 파이썬은 `sys.executable`을 쓴다
- 반복에는 상한을 둔다: `recursion_limit=25`, `ToolCallLimitMiddleware(thread_limit=10)`
- 미들웨어는 sync 훅과 `awrap_*`를 함께 구현한다
- `output_guard`는 Supervisor 뒷단 함수로 최종 응답만 검사한다. 없는 옷·세탁 중인 옷이 든 후보는 빼고 번호를 다시 매긴다. 응답을 바꿨으면 같은 `id`의 메시지로 체크포인트도 갱신한다
- 응답은 토큰 스트리밍하지 않는다. `stream_mode="updates"`로 진행만 보여주고, 최종 응답은 `output_guard`를 거친 뒤 출력한다
- 비밀 값(AWS 키, `KMA_SERVICE_KEY`)은 `.env`에서 읽고 코드나 로그에 적지 않는다. 확인이 필요하면 앞 4자리만 찍는다
- 기능을 추가하면 `evaluation/test_queries.csv`에 문항도 함께 추가한다. 건수가 바뀌면 SERVICE.md §5 통과 기준, extra.md §13.2·§14, 위 카테고리 건수도 함께 고친다

## 하지 말 것
- 요청하지 않은 파일을 새로 만들지 않는다
- 기존 파일을 통째로 다시 쓰지 않는다. 바뀐 부분만 고친다
- 옷장에 없는 옷이나 세탁 중인 옷을 추천에 넣지 않는다
- 날씨 조회가 실패했을 때 fixture나 추정값으로 몰래 대체하지 않는다(실패를 알린다)
- 대화에서 확인된(`confirmed`) 착용 기록을 자동 기록으로 덮어쓰지 않는다
- 사용자에게 묻지 않고 프로필을 바꾸지 않는다
- 가드레일 차단 사유를 사용자에게 보여주지 않는다(감사 로그에만 남긴다)
- 계산 도구나 파싱에 `eval()`을 쓰지 않는다
- 스케줄러나 별도 아침 실행 스크립트를 만들지 않는다
