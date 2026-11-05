"""CLI 대화 진입점(python -m src.chat)과 한 턴 공통 처리 ask()."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import date

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from langgraph_supervisor.handoff import METADATA_KEY_IS_HANDOFF_BACK

from src.agent import build_app, thread_config
from src.config import CHECKPOINT_PATH, get_text, resolve_date, run_settings, today
from src.guardrails import REFUSAL_MESSAGES, input_guard, outfit_lines, output_guard
from src.tools import AGENT_TOOLS
from src.tracer import FileTracer
from src.wardrobe import CATEGORIES, load_clothes, load_profile, save_recommendation, weather_record, write_wear
from src.weather import today_weather

MAX_RESUMES = 5  # 한 턴에서 HITL 승인 요청에 답하며 재개하는 횟수 상한


def reject_all(request: dict) -> dict:
    """HITL 요청의 모든 실행을 거절하는 결정. decide가 없거나 재개 상한에 닿았을 때 데이터를 바꾸지 않게 쓴다."""
    return {"decisions": [{"type": "reject", "message": "사용자 승인을 받지 못해 실행하지 않았습니다."}
                          for _ in request["action_requests"]]}


PROFILE_LABELS = {"cold_sensitivity": "추위 기준", "preferred_styles": "선호 스타일", "region": "지역"}


def _signed(n: int) -> str:
    """추위 민감도 표기: 0, +1, -1."""
    return f"{n:+d}" if n else "0"


def _to_int(value):
    """정수나 정수 모양 문자열이면 int, 아니면 None."""
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _question(action: dict) -> str:
    """HITL 요청 하나를 사용자에게 보여 줄 질문 문장으로 만든다(옷 id는 이름으로 바꾼다)."""
    name, args = action["name"], action["args"]
    if name == "confirm_wear":
        by_id = {c["id"]: c["name"] for c in load_clothes()}
        day = resolve_date(args.get("date", ""))
        items = "\n".join(f"    - {by_id.get(i, f'(옷장에 없는 id {i})')}" for i in args.get("item_ids", []))
        return f"{day or args.get('date')}에 입은 옷을 아래처럼 기록할까요?\n{items}"
    if name == "update_profile":
        field, value, profile = args.get("field"), args.get("value"), load_profile()
        current = profile.get(field)
        if field == "cold_sensitivity" and _to_int(value) is not None:
            new = _to_int(value)
            way = "따뜻하게" if new > current else "가볍게"
            return f"추위 기준을 한 단계 {way} 바꿀까요? ({_signed(current)} → {_signed(new)})"
        return f"{PROFILE_LABELS.get(field, field)}을(를) 바꿀까요? ({current} → {value})"
    return f"{name}을(를) 실행할까요? {json.dumps(args, ensure_ascii=False)}"


def _choose(prompt: str, allowed: str) -> str:
    """allowed 글자 중 하나를 입력받을 때까지 묻는다."""
    while True:
        answer = input(prompt).strip().lower()
        if answer in allowed:
            return answer
        print(f"  {', '.join(allowed)} 중 하나를 입력하세요.")


def _edit_wear(args: dict) -> dict:
    """착용 기록 수정: 옷장 목록(id 이름)을 보여 주고 옷마다 바꿀 id를 받아 새 인자를 만든다."""
    clothes = load_clothes()
    by_id = {c["id"]: c for c in clothes}
    print("  옷장:")
    for cat in CATEGORIES:
        print(f"    [{cat}] " + ", ".join(f"{c['id']} {c['name']}" for c in clothes if c["category"] == cat))
    item_ids = []
    for item_id in args.get("item_ids", []):
        name = by_id[item_id]["name"] if item_id in by_id else item_id
        answer = input(f"  {name} → 바꿀 id (엔터=그대로, -=빼기) > ").strip()
        if answer == "-":
            continue
        if answer and answer not in by_id:
            print(f"  '{answer}'은(는) 없는 id라 그대로 둡니다.")
            answer = ""
        item_ids.append(answer or item_id)
    extra = input("  더 입은 옷 id (쉼표로 구분, 엔터=없음) > ").strip()
    item_ids += [i.strip() for i in extra.split(",") if i.strip() in by_id]
    return {**args, "item_ids": item_ids or args.get("item_ids", [])}


def _edit_profile(args: dict) -> dict:
    """프로필 변경 수정(선호 스타일·지역): 바꿀 값을 직접 받는다(검증은 도구가 한다)."""
    field = args.get("field")
    if field == "preferred_styles":
        value = [s.strip() for s in input("  선호 스타일 (쉼표로 구분) > ").split(",") if s.strip()]
    else:
        value = input(f"  새 {PROFILE_LABELS.get(field, field)} > ").strip()
    return {**args, "value": value}


def _choices(action: dict) -> str:
    """요청별로 고를 수 있는 결정. 추위 민감도는 한 단계씩만 바뀌어 고칠 값이 없으므로 승인·거절만 받는다."""
    if action["name"] == "update_profile" and action["args"].get("field") == "cold_sensitivity":
        return "ar"
    return "aer"


def cli_decide(request: dict) -> dict:
    """HITL 승인 요청을 CLI에서 [a]승인 / [e]수정 / [r]거절로 묻고 재개 값({"decisions": [...]})을 만든다."""
    labels = {"a": "[a]승인", "e": "[e]수정", "r": "[r]거절"}
    decisions = []
    for action in request["action_requests"]:
        print(f"\n  [확인] {_question(action)}")
        allowed = _choices(action)
        answer = _choose(f"  {' / '.join(labels[c] for c in allowed)} > ", allowed)
        if answer == "a":
            decisions.append({"type": "approve"})
        elif answer == "r":
            decisions.append({"type": "reject", "message": "사용자가 거절했습니다. 바꾸지 않았다고 안내하세요."})
        else:
            edit = _edit_wear if action["name"] == "confirm_wear" else _edit_profile
            decisions.append({"type": "edit", "edited_action": {"name": action["name"], "args": edit(action["args"])}})
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

    먼저 input_guard로 검사해 막히면 Supervisor를 부르지 않고 정해진 거절 문구를 돌려준다.
    Supervisor를 stream_mode="updates"로 돌려 진행만 on_progress(한 줄 문자열)로 알린다.
    HITL interrupt가 오면 decide(요청)로 재개 값을 얻어 Command(resume=...)로 이어 간다(decide가 없으면 모두 거절).
    끝나면 _finish_turn()(stylist 원문 복원 → output_guard → 추천 기록·자동 기록)을 거친 응답을 돌려준다.
    contexts·trace는 이번 턴의 FileTracer가 모은 도구 이벤트로 만든다(입력에서 막히면 빈 리스트).
    """
    blocked = await input_guard(question)
    if blocked:
        return {"answer": REFUSAL_MESSAGES[blocked], "contexts": [], "trace": []}
    decide = decide or reject_all
    tracer = FileTracer(config.get("configurable", {}).get("thread_id"))
    tracer.start_turn(question)
    run_config = {**config, "callbacks": [tracer]}
    seen = _tool_call_ids((await app.aget_state(config)).values.get("messages", []))
    payload = {"messages": [HumanMessage(question)]}
    for attempt in range(MAX_RESUMES + 1):
        interrupts = []
        async for event in app.astream(payload, run_config, stream_mode="updates"):
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
        for action in request["action_requests"]:
            tracer.add_interrupted(_hitl_agent(action["name"]), action["name"], action["args"])
        payload = Command(resume=decide(request) if attempt < MAX_RESUMES else reject_all(request))
    return {"answer": await _finish_turn(app, config), "contexts": tracer.contexts(), "trace": tracer.trace()}


def _hitl_agent(tool: str) -> str:
    """HITL로 멈춘 도구를 가진 Agent 이름."""
    return next((agent for agent, names in AGENT_TOOLS.items() if tool in names), "unknown")


COMMANDS = "사용할 수 있는 명령: /date YYYY-MM-DD (오늘 날짜 바꾸기, /date만 쓰면 시스템 날짜), /quit"


def _set_today(arg: str) -> str | None:
    """/date 인자로 TODAY_OVERRIDE를 바꾼다. 빈 값이면 지운다(시스템 날짜). 형식이 틀리면 안내 문자열."""
    if not arg:
        os.environ.pop("TODAY_OVERRIDE", None)
        return None
    try:
        os.environ["TODAY_OVERRIDE"] = date.fromisoformat(arg).isoformat()
    except ValueError:
        return f"날짜 형식이 잘못됐습니다: {arg}. /date YYYY-MM-DD로 입력하세요."
    return None


async def main() -> None:
    """CLI 대화 루프. 입력마다 ask()를 부르고 answer만 출력한다. /date로 오늘을 바꾸고 /quit로 끝낸다.

    /date는 이 프로세스의 TODAY_OVERRIDE를 바꾸고 build_app()을 다시 불러 MCP 서버에도 반영한다(체크포인터는 그대로).
    thread_id가 날짜를 담으므로 대화 스레드도 그날 것으로 바뀐다.
    """
    async with AsyncSqliteSaver.from_conn_string(CHECKPOINT_PATH) as checkpointer:
        stack = AsyncExitStack()
        try:
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
                if question == "/date" or question.startswith("/date "):
                    error = _set_today(question.removeprefix("/date").strip())
                    if error:
                        print(error)
                        continue
                    await stack.aclose()
                    stack = AsyncExitStack()
                    print("MCP 서버를 다시 시작하는 중...")
                    app = await build_app(checkpointer, run_settings(), stack)
                    print(f"오늘을 {today()}로 바꿨습니다.")
                    continue
                if question.startswith("/"):
                    print(COMMANDS)
                    continue
                try:
                    result = await ask(app, question, thread_config(), decide=cli_decide,
                                       on_progress=lambda line: print(f"  {line}"))
                except Exception as e:
                    print(f"\n처리 중 오류가 발생했습니다({type(e).__name__}). 다시 시도해 주세요.")
                    continue
                print(f"\n{result['answer']}")
        finally:
            await stack.aclose()


if __name__ == "__main__":
    asyncio.run(main())
