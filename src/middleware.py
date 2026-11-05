"""하위 Agent 미들웨어: 모델 호출 로깅(LoggingMiddleware), 도구 없는 Agent의 입력 정리(TextOnlyHistoryMiddleware),
도구 결과 없는 답 되돌리기(ToolFirstMiddleware)."""

import json
import time
from datetime import datetime

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_config

from src.config import AGENT_LOG_PATH, get_text

PREVIEW = 120  # 로그에 남길 메시지 미리보기 글자 수


class LoggingMiddleware(AgentMiddleware):
    """하위 Agent의 모델 호출을 logs/agent.jsonl에 남긴다.

    before_model: 입력 메시지 수와 마지막 메시지 미리보기. after_model: 도구 호출 결정과 토큰 수.
    wrap_model_call: 호출 지연 시간과 오류. 미들웨어 목록의 맨 뒤에 둔다(앞 미들웨어가 정리한 내용만 기록).
    """

    def __init__(self, agent_name: str):
        super().__init__()
        self.agent_name = agent_name

    def _write(self, event: str, **fields) -> None:
        """로그 한 줄(JSON)을 쓴다. 스레드 id는 실행 중인 그래프 설정에서 읽는다."""
        try:
            thread_id = get_config().get("configurable", {}).get("thread_id")
        except RuntimeError:
            thread_id = None
        record = {"at": datetime.now().isoformat(timespec="milliseconds"), "agent": self.agent_name,
                  "thread_id": thread_id, "event": event, **fields}
        with open(AGENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def before_model(self, state, runtime):
        messages = state["messages"]
        last = messages[-1] if messages else None
        self._write("model_input", messages=len(messages), last_type=getattr(last, "type", None),
                    preview=get_text(last)[:PREVIEW] if last is not None else "")
        return None

    def after_model(self, state, runtime):
        last = state["messages"][-1]
        usage = getattr(last, "usage_metadata", None) or {}
        self._write("model_output",
                    tool_calls=[{"name": c["name"], "args": c["args"]} for c in getattr(last, "tool_calls", None) or []],
                    answer_preview=get_text(last)[:PREVIEW],
                    input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
        return None

    async def abefore_model(self, state, runtime):
        return self.before_model(state, runtime)

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)

    def wrap_model_call(self, request, handler):
        start = time.perf_counter()
        try:
            response = handler(request)
        except Exception as e:
            self._write("model_error", error=type(e).__name__, latency_ms=round((time.perf_counter() - start) * 1000))
            raise
        self._write("model_call", latency_ms=round((time.perf_counter() - start) * 1000))
        return response

    async def awrap_model_call(self, request, handler):
        start = time.perf_counter()
        try:
            response = await handler(request)
        except Exception as e:
            self._write("model_error", error=type(e).__name__, latency_ms=round((time.perf_counter() - start) * 1000))
            raise
        self._write("model_call", latency_ms=round((time.perf_counter() - start) * 1000))
        return response


class TextOnlyHistoryMiddleware(AgentMiddleware):
    """도구가 없는 Agent의 모델 호출에 대화 텍스트(사용자 말, 도구 호출 없는 AI 답)만 넘긴다.

    Supervisor 대화 기록에는 핸드오프 도구 호출·결과가 섞여 있는데, 도구가 없는 Agent에 그대로 넘기면
    Bedrock이 이를 평문으로 바꿔 넘기고 모델이 "Transferring back to supervisor" 같은 문구를 흉내 낸다.
    State는 바꾸지 않고 이번 모델 호출의 입력만 거른다.
    """

    @staticmethod
    def _text_only(request):
        messages = [
            m for m in request.messages
            if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls and get_text(m).strip())
        ]
        return request.override(messages=messages)

    def wrap_model_call(self, request, handler):
        return handler(self._text_only(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._text_only(request))


TOOL_FIRST_NOTICE = ("이번 요청에 아직 도구를 호출하지 않았습니다. 이전 대화의 답을 따라 하지 말고, 오늘 날씨·코디 추천이나 옷장·세탁 상태·일정·"
                     "착용 기록·프로필을 바꾸거나 조회해야 하는 요청이면 지금 도구를 호출하고 그 결과로만 답하세요. "
                     "도구 없이 답하는 요청(오늘이 아닌 날의 추천 안내, 앞선 추천에 대한 질문, 정보가 빠져 되묻는 답 등)이었다면 같은 답을 다시 쓰세요.")


def _is_handoff(name: str) -> bool:
    """Supervisor의 Agent 이동·전달 도구인지."""
    return name.startswith("transfer_") or name == "forward_message"


class ToolFirstMiddleware(AgentMiddleware):
    """실행 도구를 가진 Agent가 이번 요청의 도구 결과로만 답하게 한다.

    Supervisor(output_mode="last_message")의 대화 기록에는 하위 Agent의 최종 답만 남고 도구 호출은 빠진다.
    그래서 모델이 "바꿨습니다"처럼 도구 없이 답하는 이전 턴과 핸드오프 메시지("Transferring back to supervisor")를
    흉내 내 실행하지 않은 일을 했다고 말할 수 있다.
    wrap_model_call: 모델 입력에서 앞선 턴의 핸드오프 도구 호출·결과를 뺀다(State는 그대로).
    after_model: 이번 요청에서 자기 도구를 한 번도 부르지 않고 최종 답을 내면 안내를 붙여 모델을 한 번 더 부른다.
    되돌림은 요청당 한 번이다(되묻는 답이면 모델이 같은 답을 다시 쓴다).
    """

    def __init__(self, tool_names: list[str], only_if=None):
        """only_if(답 텍스트) -> bool을 주면 그 답일 때만 되돌린다(예: stylist는 코디 줄이 있는 답만. 앞선 추천 설명은 도구 없이 답한다)."""
        super().__init__()
        self.tool_names = set(tool_names)
        self.only_if = only_if

    @staticmethod
    def _without_handoffs(request):
        """앞선 턴의 핸드오프만 뺀다. 이번 턴 것은 남긴다: 다 빼면 대화가 다른 Agent의 답(AIMessage)으로 끝나
        Bedrock이 이어쓰기로 받아들여 빈 답을 낸다(복합 질문에서 log_agent 다음 stylist가 빈 답을 낸 사례)."""
        messages = request.messages
        start = max((i for i, m in enumerate(messages)
                     if isinstance(m, HumanMessage) and get_text(m) != TOOL_FIRST_NOTICE), default=-1)
        kept = [
            m for i, m in enumerate(messages)
            if i > start
            or not (isinstance(m, ToolMessage) and _is_handoff(m.name or ""))
            and not (isinstance(m, AIMessage) and m.tool_calls and all(_is_handoff(c["name"]) for c in m.tool_calls))
        ]
        return request.override(messages=kept)

    def wrap_model_call(self, request, handler):
        return handler(self._without_handoffs(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._without_handoffs(request))

    def _check(self, state):
        messages = state["messages"]
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None
        if self.only_if is not None and not self.only_if(get_text(last)):
            return None
        # 이번 요청 = 마지막 사용자 발화(되돌림 안내 제외) 뒤
        start = max((i for i, m in enumerate(messages)
                     if isinstance(m, HumanMessage) and get_text(m) != TOOL_FIRST_NOTICE), default=-1)
        turn = messages[start + 1:]
        if any(isinstance(m, ToolMessage) and m.name in self.tool_names for m in turn):
            return None
        if any(isinstance(m, HumanMessage) for m in turn):  # 이미 한 번 되돌렸다
            return None
        return {"messages": [HumanMessage(content=TOOL_FIRST_NOTICE)], "jump_to": "model"}

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        return self._check(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        return self._check(state)
