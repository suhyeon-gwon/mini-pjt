"""하위 Agent 미들웨어."""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from src.config import get_text


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
