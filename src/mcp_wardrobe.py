"""FastMCP 옷장 서버: src/wardrobe.py를 감싸는 얇은 래퍼(후보 옷, 일정, 프로필)."""

import json
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from src.config import resolve_date
from src.wardrobe import events_on, filter_candidates, load_clothes, load_profile
from src.weather import today_weather

mcp = FastMCP("wardrobe")


def _dump(data) -> str:
    """도구 결과를 JSON 문자열로 만든다."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _error(e: Exception) -> str:
    """예상하지 못한 오류를 다음 행동 안내 문자열로 만든다."""
    return f"에러: 처리 중 문제가 발생했습니다({type(e).__name__}). 잠시 후 다시 시도하세요."


@mcp.tool()
def get_candidates() -> str:
    """오늘 날씨에 맞춰 옷장에서 고른 카테고리별 후보 옷을 조회한다(세탁 중인 옷은 빠져 있다).

    오늘 옷을 추천할 때 쓴다. 코디는 반드시 이 결과 안의 옷으로만 짠다. note가 있으면 기준에 맞는 옷이 없어 범위를 넓혔다는 뜻이다.
    다른 날의 코디나 옷장 전체 목록에는 쓰지 않는다(옷 목록은 list_clothes). 날씨 수치는 get_today_weather를 쓴다.

    Args:
        없음. 항상 오늘 날씨 기준이다.
    """
    try:
        weather = today_weather()
        if isinstance(weather, str):
            return weather
        return _dump(filter_candidates(weather, load_profile(), load_clothes()))
    except Exception as e:
        return _error(e)


@mcp.tool()
def get_calendar(
    date: Annotated[str, Field(description='"오늘" 또는 YYYY-MM-DD. 비우면 오늘', max_length=20)] = "",
) -> str:
    """내 캘린더에서 해당 날짜의 일정(제목, 종류, 시간)을 조회한다. 일정이 없으면 events가 빈 목록이다.

    오늘 옷을 추천하기 전에 오늘 일정(데이트, 운동, 격식 자리)을 확인할 때 쓴다. 특정 날짜의 일정을 물을 때도 쓴다.
    일정을 추가할 때는 쓰지 않는다(add_event).

    Args:
        date: "오늘" 또는 YYYY-MM-DD. 비우면 오늘.
    """
    day = resolve_date(date)
    if day is None:
        return f"에러: 날짜 '{date}'를 해석할 수 없습니다. \"오늘\" 또는 YYYY-MM-DD 형식(예: 2026-11-05)으로 다시 호출하세요."
    try:
        return _dump({"date": day.isoformat(), "events": events_on(day)})
    except Exception as e:
        return _error(e)


@mcp.tool()
def get_profile() -> str:
    """내 프로필(지역, 추위 민감도 -2~+2, 선호 스타일, 최근 착용 회피 일수)을 조회한다.

    추천할 때 지역 이름과 선호 스타일을 확인하거나, 사용자가 자기 설정을 물을 때 쓴다.
    프로필을 바꿀 때는 쓰지 않는다(update_profile).

    Args:
        없음.
    """
    try:
        return _dump(load_profile())
    except Exception as e:
        return _error(e)


if __name__ == "__main__":
    mcp.run(show_banner=False)
