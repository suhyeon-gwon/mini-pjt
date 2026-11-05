"""CLI 대화 진입점(python -m src.chat)과 한 턴 공통 처리 ask()."""

import asyncio
import json
from contextlib import AsyncExitStack

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from langgraph_supervisor.handoff import METADATA_KEY_IS_HANDOFF_BACK

from src.agent import build_app, thread_config
from src.config import CHECKPOINT_PATH, get_text, run_settings, today
from src.guardrails import outfit_lines, output_guard
from src.wardrobe import save_recommendation, weather_record, write_wear
from src.weather import today_weather

MAX_RESUMES = 5  # 한 턴에서 HITL 승인 요청에 답하며 재개하는 횟수 상한


def reject_all(request: dict) -> dict:
    """HITL 요청의 모든 실행을 거절하는 결정. decide가 없거나 재개 상한에 닿았을 때 데이터를 바꾸지 않게 쓴다."""
    return {"decisions": [{"type": "reject", "message": "사용자 승인을 받지 못해 실행하지 않았습니다."}
                          for _ in request["action_requests"]]}


def cli_decide(request: dict) -> dict:
    """HITL 승인 요청을 CLI에서 묻고 재개 값({"decisions": [...]})을 만든다."""
    decisions = []
    for action in request["action_requests"]:
        print(f"\n  [확인 필요] {action['name']} {json.dumps(action['args'], ensure_ascii=False)}")
        answer = input("  [a]승인 / [r]거절 > ").strip().lower()
        decisions.append({"type": "approve"} if answer == "a" else {"type": "reject", "message": "사용자가 거절했습니다."})
    return {"decisions": decisions}


def _tool_call_ids(messages) -> set[str]:
    """메시지들에 든 도구 호출 id."""
    return {call["id"] for m in messages for call in getattr(m, "tool_calls", None) or []}


def _describe(node: str, update, seen: set[str]) -> str | None:
    """stream_mode="updates" 이벤트 하나를 진행 표시 한 줄로 바꾼다. 보여줄 것이 없으면 None.

    Supervisor 업데이트에는 앞서 본 메시지가 다시 실려 올 수 있어 seen(이미 본 도구 호출 id)으로 거른다.
    """
    if node != "supervisor":
        return f"✓ {node} 응답"
    line = None
    for message in (update or {}).get("messages", []):
        for call in getattr(message, "tool_calls", None) or []:
            if call["id"] not in seen and call["name"].startswith("transfer_to_"):
                line = f"→ {call['name'].removeprefix('transfer_to_')}에게 전달"
            seen.add(call["id"])
    return line


def _turn_stylist_message(messages: list) -> AIMessage | None:
    """이번 턴(마지막 사용자 메시지 이후)의 마지막 stylist_agent 답. 핸드오프 복귀 메시지는 뺀다."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return None
        if (isinstance(message, AIMessage) and message.name == "stylist_agent"
                and not message.response_metadata.get(METADATA_KEY_IS_HANDOFF_BACK)):
            return message
    return None


def _record_recommendation(candidates: list[list[str]]) -> None:
    """추천 후보를 오늘 날짜로 저장하고, 오늘 착용 기록을 1번 후보로 자동 기록한다(confirmed 기록은 덮지 않는다)."""
    weather = today_weather()
    record = weather_record(weather) if isinstance(weather, dict) else None
    day = today().isoformat()
    save_recommendation(day, candidates, record)
    write_wear(day, candidates[0], "auto", record)


async def _finish_turn(app, config: dict) -> str:
    """Supervisor가 끝난 뒤의 뒷단 처리를 하고 사용자에게 보낼 응답을 돌려준다.

    1) 최종 응답에 이번 턴 stylist 답의 코디 줄이 그대로 없으면(다시 쓰였으면) stylist 원문으로 되돌린다.
    2) output_guard로 없는 옷·세탁 중인 옷이 든 후보를 뺀다. 1)·2)로 응답이 바뀌면 같은 id로 체크포인트도 고친다.
    3) 남은 후보가 있으면(추천 응답이면) 추천 기록 저장과 오늘 1번 자동 기록을 한다.
    """
    messages = (await app.aget_state(config)).values["messages"]
    final = messages[-1]
    original = text = get_text(final)
    stylist = _turn_stylist_message(messages)
    if stylist is not None:
        expected = outfit_lines(get_text(stylist))
        if expected and not set(expected) <= set(outfit_lines(text)):
            text = get_text(stylist)
    answer, candidates = output_guard(text)
    if answer != original:
        await app.aupdate_state(config, {"messages": [AIMessage(content=answer, id=final.id, name=final.name)]})
    if candidates:
        _record_recommendation(candidates)
    return answer


async def ask(app, question: str, config: dict, decide=None, on_progress=None) -> dict:
    """한 턴을 처리하고 {"answer", "contexts", "trace"}를 돌려준다.

    Supervisor를 stream_mode="updates"로 돌려 진행만 on_progress(한 줄 문자열)로 알린다.
    HITL interrupt가 오면 decide(요청)로 재개 값을 얻어 Command(resume=...)로 이어 간다(decide가 없으면 모두 거절).
    끝나면 _finish_turn()(stylist 원문 복원 → output_guard → 추천 기록·자동 기록)을 거친 응답을 돌려준다.
    contexts·trace는 FileTracer 연결 전까지 빈 리스트다.
    """
    decide = decide or reject_all
    seen = _tool_call_ids((await app.aget_state(config)).values.get("messages", []))
    payload = {"messages": [HumanMessage(question)]}
    for attempt in range(MAX_RESUMES + 1):
        interrupts = []
        async for event in app.astream(payload, config, stream_mode="updates"):
            for node, update in event.items():
                if node == "__interrupt__":
                    interrupts.extend(update)
                    continue
                line = _describe(node, update, seen)
                if on_progress and line:
                    on_progress(line)
        if not interrupts:
            break
        request = interrupts[0].value
        payload = Command(resume=decide(request) if attempt < MAX_RESUMES else reject_all(request))
    return {"answer": await _finish_turn(app, config), "contexts": [], "trace": []}


async def main() -> None:
    """CLI 대화 루프. 입력마다 ask()를 부르고 answer만 출력한다. /quit로 끝낸다."""
    async with AsyncSqliteSaver.from_conn_string(CHECKPOINT_PATH) as checkpointer, AsyncExitStack() as stack:
        print("MCP 서버를 시작하는 중...")
        app = await build_app(checkpointer, run_settings(), stack)
        print(f"오늘({today()}) 옷 추천 도우미입니다. \"오늘 뭐 입지?\"라고 물어보세요. 끝내려면 /quit")
        while True:
            try:
                question = input("\n나> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not question:
                continue
            if question == "/quit":
                break
            if question.startswith("/"):
                print("사용할 수 있는 명령: /quit")
                continue
            try:
                result = await ask(app, question, thread_config(), decide=cli_decide,
                                   on_progress=lambda line: print(f"  {line}"))
            except Exception as e:
                print(f"\n처리 중 오류가 발생했습니다({type(e).__name__}). 다시 시도해 주세요.")
                continue
            print(f"\n{result['answer']}")


if __name__ == "__main__":
    asyncio.run(main())
