"""FastMCP 날씨 서버: src/weather.py를 감싸는 얇은 래퍼."""

import json

from fastmcp import FastMCP

from src.weather import today_weather

mcp = FastMCP("weather")


@mcp.tool()
def get_today_weather() -> str:
    """오늘 날씨 요약을 조회한다: 출근·낮·퇴근 기온과 체감온도, 최저·최고, 일교차, 비 시간대, 플래그, 챙길 것.

    오늘 옷을 추천할 때 쓴다. '날씨 조회 실패'로 시작하는 결과가 오면 날씨를 추정하지 말고 실패를 알린다.
    다른 날(내일, 토요일)의 날씨나 미세먼지·자외선에는 쓰지 않는다(조회할 수 없다). 후보 옷은 get_candidates를 쓴다.

    Args:
        없음. 항상 오늘 날씨를 돌려준다.
    """
    try:
        result = today_weather()
    except Exception as e:
        return f"날씨 조회 실패: 처리 중 문제가 발생했습니다({type(e).__name__}). 날씨를 추정하지 말고 실패를 알리세요."
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run(show_banner=False)
