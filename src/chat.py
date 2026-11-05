"""CLI 대화 진입점(python -m src.chat)과 한 턴 공통 처리 ask()."""

import asyncio
from contextlib import AsyncExitStack

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from src.agent import build_app, thread_config
from src.config import CHECKPOINT_PATH, get_text, run_settings, today


def _describe(node: str, update) -> str | None:
    """stream_mode="updates" 이벤트 하나를 진행 표시 한 줄로 바꾼다. 보여줄 것이 없으면 None."""
    if node != "supervisor":
        return f"✓ {node} 응답"
    for message in (update or {}).get("messages", []):
        for call in getattr(message, "tool_calls", None) or []:
            if call["name"].startswith("transfer_to_"):
                return f"→ {call['name'].removeprefix('transfer_to_')}에게 전달"
    return None


async def ask(app, question: str, config: dict, decide=None, on_progress=None) -> dict:
    """한 턴을 처리하고 {"answer", "contexts", "trace"}를 돌려준다.

    Supervisor를 stream_mode="updates"로 돌려 진행만 on_progress(한 줄 문자열)로 알리고, 최종 응답은 끝난 뒤 한 번에 꺼낸다.
    decide는 HITL interrupt의 재개 값을 얻는 함수다(HITL 연결 전까지는 쓰이지 않는다).
    contexts·trace는 FileTracer 연결 전까지 빈 리스트다.
    """
    async for event in app.astream({"messages": [HumanMessage(question)]}, config, stream_mode="updates"):
        for node, update in event.items():
            line = _describe(node, update)
            if on_progress and line:
                on_progress(line)
    state = await app.aget_state(config)
    return {"answer": get_text(state.values["messages"][-1]), "contexts": [], "trace": []}


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
                result = await ask(app, question, thread_config(), on_progress=lambda line: print(f"  {line}"))
            except Exception as e:
                print(f"\n처리 중 오류가 발생했습니다({type(e).__name__}). 다시 시도해 주세요.")
                continue
            print(f"\n{result['answer']}")


if __name__ == "__main__":
    asyncio.run(main())
