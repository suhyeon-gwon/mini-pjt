"""관측: 콜백 트레이서(FileTracer). logs/trace.jsonl에 쓰고, 한 턴의 도구 호출·결과를 모아 contexts·trace를 만든다."""

import json
import time
import uuid
from datetime import datetime

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage

from src.config import TRACE_LOG_PATH, get_text

RETRIEVAL_TOOLS = {"search_wear_history"}
PREVIEW = 300  # trace.jsonl에 남길 도구 결과 미리보기 글자 수


def _agent(metadata: dict | None) -> str:
    """콜백 메타데이터의 checkpoint_ns 첫 마디가 실행 중인 Agent 이름이다(최상위는 supervisor)."""
    ns = (metadata or {}).get("checkpoint_ns") or ""
    return ns.split(":", 1)[0] or "unknown"


def _is_handoff(tool: str) -> bool:
    """Supervisor의 Agent 이동·전달 도구인지(근거 자료가 아니라 contexts에서 뺀다)."""
    return tool.startswith("transfer_") or tool == "forward_message"


def _output_text(output) -> str:
    """도구 결과를 문자열로. MCP 도구는 블록 리스트가 든 ToolMessage, 핸드오프는 Command라 빈 문자열."""
    if isinstance(output, ToolMessage):
        return get_text(output)
    return output if isinstance(output, str) else ""


class FileTracer(BaseCallbackHandler):
    """한 턴(ask() 한 번)마다 새로 만들어 최상위 callbacks로 넘긴다. 하위 Agent의 모델·도구 호출까지 잡는다."""

    run_inline = True  # 이벤트를 호출 순서대로 받기 위해 같은 스레드에서 바로 처리

    def __init__(self, thread_id: str | None = None):
        self.thread_id = thread_id
        self.turn = uuid.uuid4().hex[:8]
        self.events: list[dict] = []           # 이번 턴의 도구 호출(시작 순서)
        self._tools: dict = {}                 # run_id → 도구 호출
        self._llm_starts: dict = {}            # run_id → (시작 시각, Agent)

    def _write(self, event: str, **fields) -> None:
        record = {"at": datetime.now().isoformat(timespec="milliseconds"), "thread_id": self.thread_id,
                  "turn": self.turn, "event": event, **fields}
        with open(TRACE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def start_turn(self, question: str) -> None:
        """턴 시작을 남긴다."""
        self._write("turn_start", input=question[:200])

    def add_interrupted(self, agent: str, tool: str, args: dict) -> None:
        """HITL interrupt로 실행 전에 멈춘 도구 호출을 남긴다(콜백이 오지 않으므로 ask()가 직접 부른다)."""
        self.events.append({"agent": agent, "tool": tool, "args": args, "status": "interrupted", "content": None})
        self._write("tool_interrupted", agent=agent, tool=tool, args=args)

    # 모델
    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        self._llm_starts[run_id] = (time.perf_counter(), _agent(metadata))

    def on_llm_end(self, response, *, run_id, **kwargs):
        start, agent = self._llm_starts.pop(run_id, (None, "unknown"))
        message = getattr(response.generations[0][0], "message", None) if response.generations else None
        usage = getattr(message, "usage_metadata", None) or {}
        self._write("llm_end", agent=agent, latency_ms=round((time.perf_counter() - start) * 1000) if start else None,
                    input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))

    def on_llm_error(self, error, *, run_id, **kwargs):
        _, agent = self._llm_starts.pop(run_id, (None, "unknown"))
        self._write("llm_error", agent=agent, error=type(error).__name__, detail=str(error)[:200])

    # 도구
    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, inputs=None, **kwargs):
        call = {"agent": _agent(metadata), "tool": serialized.get("name", "?"),
                "args": inputs if inputs is not None else input_str, "status": None, "content": None}
        self._tools[run_id] = call
        self.events.append(call)
        self._write("tool_start", agent=call["agent"], tool=call["tool"], args=call["args"])

    def on_tool_end(self, output, *, run_id, **kwargs):
        call = self._tools.pop(run_id, None)
        if call is None:
            return
        call["content"] = _output_text(output)
        call["status"] = "error" if getattr(output, "status", None) == "error" else "ok"
        self._write("tool_end", agent=call["agent"], tool=call["tool"], status=call["status"],
                    output=call["content"][:PREVIEW])

    def on_tool_error(self, error, *, run_id, **kwargs):
        call = self._tools.pop(run_id, None)
        if call is None:
            return
        call["content"], call["status"] = f"{type(error).__name__}: {error}", "error"
        self._write("tool_error", agent=call["agent"], tool=call["tool"], error=call["content"][:PREVIEW])

    # ask() 반환용
    def contexts(self) -> list[dict]:
        """이번 턴에 실행된 MCP 도구 결과(호출 순서). search_wear_history는 retrieval, 나머지는 tool. 핸드오프는 뺀다."""
        return [
            {"kind": "retrieval" if e["tool"] in RETRIEVAL_TOOLS else "tool", "agent": e["agent"],
             "tool": e["tool"], "content": e["content"]}
            for e in self.events if e["status"] in ("ok", "error") and not _is_handoff(e["tool"])
        ]

    def trace(self) -> list[dict]:
        """이번 턴의 도구 호출 목록(호출 순서, 핸드오프 포함). status는 ok / error / interrupted."""
        return [{"agent": e["agent"], "tool": e["tool"], "args": e["args"], "status": e["status"]}
                for e in self.events if e["status"]]
