"""도메인 도구: MCP 서버 2개를 띄워 도구를 불러오고 Agent별로 나눈다."""

import sys
from contextlib import AsyncExitStack

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from src.config import ROOT_DIR

SERVERS = {"weather": "src.mcp_weather", "wardrobe": "src.mcp_wardrobe"}

AGENT_TOOLS = {
    "stylist_agent": ["get_today_weather", "get_candidates", "get_calendar", "get_profile"],
    "log_agent": ["get_recent_wear", "get_recommendation", "get_profile", "confirm_wear", "record_feedback", "update_profile"],
}
HITL_TOOLS = ("confirm_wear", "update_profile")  # 학습 기록(착용 기록, 프로필)을 바꾸는 도구만 사람 승인


def connections(settings: dict) -> dict:
    """서버별 stdio 연결 설정. 실행 설정은 env로 명시해 넘긴다(stdio는 환경변수를 다 물려주지 않는다)."""
    # 서버 stderr는 CLI 화면에 그대로 찍힌다. 도구 오류는 결과로 LLM에 전달되므로 서버 로그·경고는 끈다.
    env = {**settings, "FASTMCP_LOG_LEVEL": "CRITICAL", "PYTHONWARNINGS": "ignore"}
    return {
        name: {"transport": "stdio", "command": sys.executable, "args": ["-m", module], "cwd": ROOT_DIR, "env": env}
        for name, module in SERVERS.items()
    }


async def open_tools(stack: AsyncExitStack, settings: dict) -> dict[str, BaseTool]:
    """MCP 서버 2개와 세션을 열어 도구 이름 → 도구 사전을 돌려준다.

    세션은 stack이 닫힐 때까지 유지된다(도구 호출마다 서버를 새로 띄우지 않고, 서버 메모리의 색인을 이어 쓰게).
    """
    client = MultiServerMCPClient(connections(settings))
    tools = {}
    for name in SERVERS:
        session = await stack.enter_async_context(client.session(name))
        for t in await load_mcp_tools(session, server_name=name):
            tools[t.name] = t
    return tools


def tools_for(agent: str, tools: dict[str, BaseTool]) -> list[BaseTool]:
    """Agent에 배정된 도구 목록."""
    return [tools[name] for name in AGENT_TOOLS[agent]]
