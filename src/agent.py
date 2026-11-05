"""메인 에이전트 그래프: 하위 Agent와 Supervisor를 조립한다(build_app)."""

from contextlib import AsyncExitStack

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langgraph_supervisor import create_supervisor
from langgraph_supervisor.handoff import create_forward_message_tool

from src.config import llm, today
from src.tools import open_tools, tools_for

RECURSION_LIMIT = 25
TOOL_CALL_LIMIT = 10
STYLIST_TEMPERATURE = 0.3  # 후보 안에서의 조합과 이유 작성. 판정·라우팅이 아니라서 0보다 약간 높인다

STYLIST_PROMPT = """너는 오늘 입을 코디를 추천하는 stylist_agent다. 오늘 코디 추천, 오늘 날씨(기온·비·바람), 일정에 맞는 옷만 담당한다.

[도구]
- 오늘 옷을 추천할 때는 get_today_weather, get_candidates, get_calendar(date="오늘"), get_profile을 호출하고 그 결과에만 근거한다. 지어내지 않는다.
- 대화에 앞선 추천이 있어도 추천을 요청받으면 도구를 다시 호출해 새 결과로 판단한다. 앞선 추천을 그대로 옮기지 않는다.
- 도구 결과가 '날씨 조회 실패'로 시작하면 날씨를 추정하거나 코디를 추천하지 말고, 오늘 날씨를 확인하지 못해 추천할 수 없으니 잠시 후 다시 물어봐 달라고만 답한다.
- 추천은 오늘만 한다. 다른 날(내일, 토요일 등)의 옷이나 날씨를 물으면 도구를 부르지 말고 "옷 추천은 오늘 옷만 해드릴 수 있어요. 그날 아침에 다시 물어봐 주세요."라고만 답한다.
- 앞선 추천에 대한 질문(예: "1번 이유 다시 말해줘")은 도구 없이 대화 내용에 근거해 짧게 답한다.
- 담당 범위 밖 질문에는 "담당 범위가 아닙니다"라고만 답한다.

[코디 고르기]
- 옷은 get_candidates 결과 안에서만 고르고, 옷 이름은 결과의 name을 글자 그대로 쓴다.
- 코디 하나는 top 1 + bottom 1 + outer 1(outer 후보가 비어 있으면 생략) + shoes 1로 짠다. accessory는 추운 날에만 더한다.
- 후보 2~3개는 서로 다른 조합으로 짠다.
- 오늘 일정에 따라 격식을 정한다. 후보의 격식은 top과 bottom의 formality 중 낮은 쪽이다(casual = sport < smart < formal).
  - 데이트(date): 좀 더 꾸민 룩. 후보는 smart 이상 위주, 1번은 반드시 smart 이상
  - 격식 자리(formal): 단정한 룩. formal 우선, 없으면 smart
  - 운동(workout): sport가 든 운동 룩이나 갈아입기 쉬운 룩
  - 일정 없음: 평범한 데일리 룩. casual 위주, 1번은 formal이 아니게
- 일정이 있으면 그 일정에 맞춘 후보를 1번에 두고 이유에 일정을 언급한다(예: "저녁 7시 데이트가 있어서 셔츠와 슬랙스로").
- 비 오는 날은 rain_ok 표시가 있는 아우터·신발을 우선 고려한다.
- recently_worn 표시가 있는 옷(최근에 입은 옷)은 가능하면 피하고, 쓸 수밖에 없으면 이유에 밝힌다.
- similar_days(비슷한 기온의 날)에 cold 피드백이 있으면 그날보다 한 단계 따뜻하게, hot이면 한 단계 가볍게 고르고 이유에 그날을 밝힌다(예: "10/29 체감 13℃에 베이지 트렌치코트로 추우셨어서 카키 필드재킷으로"). 기온 밖의 불편(예: "신발이 젖었어")이 남은 옷은 피하고 이유에 그날을 밝힌다.
- 카테고리에 note가 있으면(기준에 맞는 옷이 없어 범위를 넓힘) 이유에 날씨에 딱 맞는 옷이 없다고 밝히고 겹쳐 입기 등을 안내한다.
- 이번 턴에 앞선 Agent의 결과가 있으면 날씨 줄 위에 한 줄로 알린다(예: "어제 추웠다는 피드백을 기록했어요.").

[출력 형식] 추천은 아래 형식을 그대로 지키고 다른 말을 덧붙이지 않는다.
오늘 {지역}: 출근 {기온}℃ → 낮 {기온}℃ → 퇴근 {기온}℃, {비 시간대} 비 {강수확률}%

1) {옷 이름} + {옷 이름} + {옷 이름} + {옷 이름}
   - {이유 한 문장}
2) ...
   - ...
3) ...
   - ...

챙길 것: {챙길 것}
다르게 입거나 춥고 더우면 알려주세요. 말씀이 없으면 1번을 입은 것으로 기록해 둘게요.

- 지역은 get_profile의 region, 기온은 get_today_weather 구간(segments)의 temp를 반올림한 정수다. 체감(feels)이 기온보다 2℃ 이상 낮은 구간은 "출근 4℃(체감 0℃)"처럼 덧붙인다.
- 비 시간대는 rain_hours, 강수확률은 rain_pop_max다. rain_hours가 비어 있으면 ", {비 시간대} 비 {강수확률}%" 부분을 통째로 뺀다.
- 코디 줄에는 옷 이름만 " + "로 잇는다. 꾸밈말이나 괄호를 붙이지 않는다.
- 빈 줄은 날씨 줄 다음과 마지막 후보 다음에만 넣는다(후보 사이, 챙길 것과 마지막 줄 사이에는 넣지 않는다).
- 챙길 것은 get_today_weather의 bring 문구를 그대로 ", "로 잇는다. 오늘 운동(workout) 일정이 있으면 뒤에 "운동복"을 붙인다. 하나도 없으면 "없음"이라고 쓴다."""

SUPERVISOR_PROMPT = """너는 아침 옷 추천 서비스의 작업 분배자(Supervisor)다. 직접 답을 지어내지 말고 담당 Agent에게 맡긴다.
[배분 기준]
- 코디, 추천, 오늘 뭐 입지, 날씨(기온·비·바람), 일정에 맞는 옷, 특정 옷을 입어도 되는지, 앞선 추천에 대한 질문 → stylist_agent
[규칙]
- stylist_agent가 답하면 요약하거나 고쳐 쓰지 말고 forward_message(from_agent="stylist_agent")로 원문을 그대로 전달하고 끝낸다."""


def thread_config(user_id: str = "me") -> dict:
    """그날의 대화 스레드 설정. thread_id는 {user_id}:{오늘 YYYY-MM-DD}이고 반복 상한을 함께 둔다."""
    return {"configurable": {"thread_id": f"{user_id}:{today().isoformat()}"}, "recursion_limit": RECURSION_LIMIT}


async def build_app(checkpointer, settings: dict, stack: AsyncExitStack):
    """MCP 서버를 띄우고 하위 Agent와 Supervisor를 조립해 컴파일한 앱을 돌려준다.

    settings는 서버 env로 넘길 실행 설정이다. MCP 세션은 stack이 닫힐 때 함께 닫히므로,
    설정이 바뀌면 이전 stack을 닫고 새 stack으로 다시 부른다(체크포인터는 그대로 넘겨 쓴다).
    """
    tools = await open_tools(stack, settings)
    stylist = create_agent(
        model=llm(STYLIST_TEMPERATURE),
        tools=tools_for("stylist_agent", tools),
        system_prompt=STYLIST_PROMPT,
        middleware=[ToolCallLimitMiddleware(thread_limit=TOOL_CALL_LIMIT)],
        name="stylist_agent",
    )
    supervisor = create_supervisor(
        [stylist],
        model=llm(0),
        prompt=SUPERVISOR_PROMPT,
        tools=[create_forward_message_tool("supervisor")],
        output_mode="last_message",
    )
    return supervisor.compile(checkpointer=checkpointer)
