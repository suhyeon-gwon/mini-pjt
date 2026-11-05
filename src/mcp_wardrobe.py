"""FastMCP 옷장 서버: src/wardrobe.py·feedback.py·retriever.py를 감싸는 얇은 래퍼(후보 옷, 일정, 프로필, 착용 기록 검색)."""

import contextlib
import io
import json
from typing import Annotated, Literal

from pydantic import Field

with contextlib.redirect_stderr(io.StringIO()):  # fastmcp가 불러오는 authlib의 강제 DeprecationWarning을 숨긴다
    from fastmcp import FastMCP

from src import feedback, retriever
from src.config import resolve_date, today
from src import wardrobe
from src.guardrails import audit, rule_check
from src.wardrobe import (
    events_on, filter_candidates, load_clothes, load_profile, load_wear_log, recent_wear, recommendation_on,
    weather_record, write_wear,
)
from src.weather import today_weather

mcp = FastMCP("wardrobe")

DATE_HELP = '"오늘", "어제", "내일", "토요일", "이번 주 토요일", "다음 주 금요일", "11월 7일" 또는 YYYY-MM-DD'


def _dump(data) -> str:
    """도구 결과를 JSON 문자열로 만든다."""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _error(e: Exception) -> str:
    """예상하지 못한 오류를 다음 행동 안내 문자열로 만든다."""
    return f"에러: 처리 중 문제가 발생했습니다({type(e).__name__}). 잠시 후 다시 시도하세요."


def _bad_date(text: str) -> str:
    """날짜를 해석하지 못했을 때의 안내."""
    return f"에러: 날짜 '{text}'를 해석할 수 없습니다. {DATE_HELP} 형식으로 다시 호출하세요."


@mcp.tool()
def get_candidates() -> str:
    """오늘 날씨에 맞춰 옷장에서 고른 카테고리별 후보 옷과, 비슷한 기온이었던 날(입은 옷, 피드백)을 조회한다.

    오늘 옷을 추천할 때 쓴다. 코디는 반드시 이 결과 안의 옷으로만 짠다. 세탁 중인 옷은 빠져 있다.
    note는 기준에 맞는 옷이 없어 범위를 넓혔다는 뜻이고, recently_worn은 최근에 입은 옷 표시, similar_days는 비슷한 기온의 날이다.
    다른 날의 코디나 옷장 전체 목록에는 쓰지 않는다(옷 목록은 list_clothes). 날씨 수치는 get_today_weather를 쓴다.

    Args:
        없음. 항상 오늘 날씨 기준이다.
    """
    try:
        weather = today_weather()
        if isinstance(weather, str):
            return weather
        return _dump(filter_candidates(weather, load_profile(), load_clothes(), load_wear_log()))
    except Exception as e:
        return _error(e)


@mcp.tool()
def get_calendar(
    date: Annotated[str, Field(description=DATE_HELP, max_length=20)] = "",
) -> str:
    """내 캘린더에서 해당 날짜의 일정(제목, 종류, 시간)을 조회한다. 결과의 date는 코드가 해석한 날짜이고, 일정이 없으면 events가 빈 목록이다.

    오늘 옷을 추천하기 전에 오늘 일정(데이트, 운동, 격식 자리)을 확인할 때 쓴다. 특정 날짜의 일정을 물을 때도 쓴다.
    일정을 추가할 때는 쓰지 않는다(add_event). 날짜를 직접 계산하지 말고 사용자가 말한 표현을 그대로 넘긴다.

    Args:
        date: 사용자가 말한 날짜 표현 그대로("오늘", "토요일", "다음 주 금요일", "11월 7일") 또는 YYYY-MM-DD. 비우면 오늘.
    """
    day = resolve_date(date)
    if day is None:
        return _bad_date(date)
    try:
        return _dump({"date": day.isoformat(), "events": events_on(day)})
    except Exception as e:
        return _error(e)


@mcp.tool()
def get_profile() -> str:
    """내 프로필(지역, 추위 민감도 -2~+2, 선호 스타일, 최근 착용 회피 일수)과 바꿀 수 있는 범위(change_rules)를 조회한다.

    추천할 때 지역 이름과 선호 스타일을 확인하거나, 사용자가 자기 설정을 물을 때, 프로필을 바꾸기 전에 범위를 볼 때 쓴다.
    프로필을 바꿀 때는 쓰지 않는다(update_profile).

    Args:
        없음.
    """
    try:
        return _dump({**load_profile(), "change_rules": feedback.change_rules()})
    except Exception as e:
        return _error(e)


@mcp.tool()
def get_recent_wear(
    days: Annotated[int, Field(description="오늘을 포함해 며칠 전까지 볼지 (1~30)", ge=1, le=30)] = 7,
) -> str:
    """오늘을 포함한 최근 N일의 착용 기록(날짜별 입은 옷 이름, 기록 종류, 피드백)을 최신순으로 조회한다.

    "최근에 뭐 입었지?", "지난주에 뭐 입었어?"처럼 지난 며칠의 기록을 날짜순으로 볼 때 쓴다. 기록이 없으면 records가 빈 목록이다.
    특정 날짜의 추천 후보 번호를 볼 때는 쓰지 않는다(get_recommendation). 비 온 날·데이트 날·특정 옷을 입은 날처럼
    조건이나 내용으로 찾을 때는 쓰지 않는다(search_wear_history).

    Args:
        days: 1~30. 기본 7.
    """
    try:
        return _dump({"today": today().isoformat(), "days": days, "records": recent_wear(today().isoformat(), days)})
    except Exception as e:
        return _error(e)


@mcp.tool()
def get_recommendation(
    date: Annotated[str, Field(description=DATE_HELP, max_length=20)],
) -> str:
    """그날 추천했던 후보를 번호별 옷 이름·id로 조회한다. 결과의 date는 코드가 해석한 날짜다.

    "오늘 2번 입을게", "어제 2번 입었어"처럼 지난 추천을 번호로 말할 때, confirm_wear에 넘길 item_ids를 찾으려고 쓴다.
    지난 며칠의 실제 착용 기록을 볼 때는 쓰지 않는다(get_recent_wear).

    Args:
        date: 사용자가 말한 날짜 표현 그대로("오늘", "어제") 또는 YYYY-MM-DD.
    """
    day = resolve_date(date)
    if day is None:
        return _bad_date(date)
    try:
        rec = recommendation_on(day.isoformat())
        if rec is None:
            return f"그날 추천이 없습니다({day.isoformat()}). 추천 번호 대신 입은 옷 이름을 알려 달라고 하세요."
        return _dump(rec)
    except Exception as e:
        return _error(e)


def _mask_feedback(result: dict) -> dict:
    """저장된 피드백 텍스트에 1차 정규식을 적용해, 걸리면 "[가려짐]"으로 바꾸고 감사 로그에 남긴다(원본 JSON은 그대로)."""
    for record in result["records"]:
        for fb in record["feedback"]:
            hit = rule_check(fb["text"])
            if hit:
                audit("retrieval", hit[0], {"date": record["date"], "reason": hit[1], "text": fb["text"][:200]})
                fb["text"] = "[가려짐]"
    return result


@mcp.tool()
def search_wear_history(
    query: Annotated[str, Field(description="찾고 싶은 기록을 자연어로 (예: 비 와서 신발이 젖은 날)", min_length=1, max_length=200)],
    rain: Annotated[Literal["", "yes", "no"], Field(description="yes=비 온 날만, no=비 안 온 날만, 빈 값이면 거르지 않음")] = "",
    event_type: Annotated[Literal["", "date", "workout", "formal", "outdoor", "etc"], Field(description="그 종류의 일정이 있던 날만. 빈 값이면 거르지 않음")] = "",
    item_name: Annotated[str, Field(description="그 옷을 입은 날만. 옷 이름(clothes의 name) 그대로. 빈 값이면 거르지 않음", max_length=30)] = "",
    direction: Annotated[Literal["", "cold", "hot", "ok"], Field(description="그 피드백(추웠다/더웠다/괜찮았다)이 있던 날만. 빈 값이면 거르지 않음")] = "",
    k: Annotated[int, Field(description="돌려받을 기록 수 (1~5)", ge=1, le=5)] = 3,
) -> str:
    """상황(비 온 날, 데이트 날), 입은 옷, 피드백 표현으로 지난 착용 기록(날씨, 일정, 입은 옷, 피드백)을 찾는다.

    "저번에 비 왔을 때 뭐 입었더라?", "베이지 트렌치코트 입었던 날 어땠어?", "신발 젖었던 날 언제야?"처럼
    지난 기록을 조건이나 내용으로 찾을 때 쓴다. 조건에 맞는 기록이 없으면 그렇다고 안내하는 문자열을 돌려준다.
    최근 며칠의 기록을 날짜순으로 볼 때는 쓰지 않는다(get_recent_wear). 기온이 비슷했던 날은 get_candidates의 similar_days를 본다.

    Args:
        query: 찾을 내용 자연어.
        rain: yes / no / 빈 값.
        event_type: date / workout / formal / outdoor / etc / 빈 값.
        item_name: 옷 이름 그대로 또는 빈 값.
        direction: cold / hot / ok / 빈 값.
        k: 1~5. 기본 3.
    """
    try:
        result = retriever.search(query.strip(), rain, event_type, item_name, direction, k)
        return result if isinstance(result, str) else _dump(_mask_feedback(result))
    except Exception as e:
        return _error(e)


@mcp.tool()
def confirm_wear(
    date: Annotated[str, Field(description=DATE_HELP, max_length=20)],
    item_ids: Annotated[list[str], Field(description="입은 옷 id 목록 (예: ['c001', 'c006'])", min_length=1, max_length=6)],
) -> str:
    """그날 실제로 입은 옷을 착용 기록에 남긴다(같은 날 기록이 있으면 바꾸고, 피드백은 유지한다).

    학습 기록을 바꾸는 작업이므로 사람 승인이 필요하다.
    사용자가 추천과 다르게 입었다고 말할 때("오늘 2번 입을게", "어제 2번 입었어") 쓴다. 번호로 말하면 먼저 get_recommendation으로 item_ids를 찾는다.
    추천 1번을 입은 경우나 춥다·덥다 같은 피드백에는 쓰지 않는다(1번은 자동 기록됨, 피드백은 record_feedback).

    Args:
        date: 사용자가 말한 날짜 표현 그대로("오늘", "어제") 또는 YYYY-MM-DD.
        item_ids: 입은 옷 id 목록.
    """
    day = resolve_date(date)
    if day is None:
        return _bad_date(date)
    try:
        by_id = {c["id"]: c for c in load_clothes()}
        unknown = [i for i in item_ids if i not in by_id]
        if unknown:
            return f"에러: 옷장에 없는 id입니다: {', '.join(unknown)}. get_recommendation이나 list_clothes로 id를 확인해 다시 호출하세요."
        weather = None
        if day == today():
            summary = today_weather()
            weather = weather_record(summary) if isinstance(summary, dict) else None
        write_wear(day.isoformat(), item_ids, "confirmed", weather)
        return _dump({
            "date": day.isoformat(),
            "recorded_items": [by_id[i]["name"] for i in item_ids],
            "source": "confirmed",
            "안내": "실제로 기록된 옷은 recorded_items다. 사용자에게 이 목록 그대로 안내한다.",
        })
    except Exception as e:
        return _error(e)


@mcp.tool()
def record_feedback(
    date: Annotated[str, Field(description=DATE_HELP, max_length=20)],
    text: Annotated[str, Field(description="사용자가 한 말 그대로 (예: '저녁에 좀 추웠어')", min_length=1, max_length=200)],
    direction: Annotated[Literal["cold", "hot", "ok"], Field(description="추웠으면 cold, 더웠으면 hot, 그 밖은 ok")],
) -> str:
    """그날 옷에 대한 피드백(춥다, 덥다, 괜찮다, 불편했다)을 착용 기록에 바로 남긴다.

    "오늘 추웠어", "어제 좀 더웠어", "신발이 젖었어"처럼 입은 옷이 어땠는지 말할 때 쓴다. 날짜를 말하지 않으면 "오늘".
    결과에 suggest_profile_change가 있으면 곧바로 update_profile로 그 값을 요청한다.
    입은 옷 자체를 바꿀 때는 쓰지 않는다(confirm_wear). 체질을 직접 말할 때("나 추위 많이 타")는 update_profile.

    Args:
        date: 사용자가 말한 날짜 표현 그대로("오늘", "어제") 또는 YYYY-MM-DD.
        text: 사용자가 한 말.
        direction: cold / hot / ok.
    """
    day = resolve_date(date)
    if day is None:
        return _bad_date(date)
    try:
        return _dump(feedback.record_feedback(day.isoformat(), text, direction))
    except Exception as e:
        return _error(e)


@mcp.tool()
def update_profile(
    field: Annotated[Literal["cold_sensitivity", "preferred_styles", "region"], Field(description="바꿀 항목")],
    value: Annotated[int | str | list[str], Field(description="cold_sensitivity는 정수(현재 ±1), preferred_styles는 문자열 목록, region은 지역 이름")],
) -> str:
    """프로필 한 항목(추위 민감도, 선호 스타일, 지역)을 바꾼다. 코드가 값을 검증하고 실제로 반영한 값을 돌려준다.

    학습 기록을 바꾸는 작업이므로 사람 승인이 필요하다.
    record_feedback 결과에 suggest_profile_change가 있을 때, 또는 사용자가 체질·선호·지역을 직접 바꾸려 할 때 쓴다.
    추위 민감도는 −2(더위 많이 탐)~+2(추위 많이 탐), 한 번에 ±1까지만 바꿀 수 있다. 조회만 할 때는 쓰지 않는다(get_profile).

    Args:
        field: cold_sensitivity / preferred_styles / region.
        value: 새 값.
    """
    try:
        result = feedback.change_profile(field, value)
        if isinstance(result, str):
            return result
        return _dump({**result, "안내": "실제로 반영된 값은 to다. 사용자에게 이 값 그대로 안내한다."})
    except Exception as e:
        return _error(e)


@mcp.tool()
def list_clothes(
    category: Annotated[Literal["", "top", "bottom", "outer", "shoes", "accessory"], Field(description="빈 값이면 전체")] = "",
    status: Annotated[Literal["", "available", "laundry"], Field(description="available=입을 수 있음, laundry=세탁 중. 빈 값이면 전체")] = "",
) -> str:
    """옷장의 옷 목록(id, 이름, 종류, 두께, 색, 격식, 방수, 세탁 상태)을 조회한다.

    "내 아우터 뭐 있어?"처럼 옷장을 볼 때, 특정 옷("빨간 패딩")이 옷장에 있는지·세탁 중인지 확인할 때,
    옷 이름으로 id를 찾을 때(update_status, confirm_wear 전에) 쓴다.
    오늘 날씨에 맞는 후보를 고를 때는 쓰지 않는다(get_candidates).

    Args:
        category: top / bottom / outer / shoes / accessory. 빈 값이면 전체.
        status: available / laundry. 빈 값이면 전체.
    """
    try:
        rows = wardrobe.list_clothes(category, status)
        return _dump({"count": len(rows), "clothes": rows})
    except Exception as e:
        return _error(e)


@mcp.tool()
def add_clothing(
    name: Annotated[str, Field(description='"색 + 종류" 형식 이름 (예: "카멜 울 코트")', min_length=2, max_length=30)],
    category: Annotated[Literal["top", "bottom", "outer", "shoes", "accessory"], Field(description="옷 종류")],
    warmth: Annotated[int, Field(description="두께·보온 1(아주 얇음)~5(아주 따뜻함)", ge=1, le=5)],
    color: Annotated[str, Field(description="영문 소문자 색 이름 하나 (예: camel, navy)", pattern=r"^[a-z]+$")],
    formality: Annotated[Literal["casual", "smart", "formal", "sport"], Field(description="격식")],
    waterproof: Annotated[bool, Field(description="방수 여부")] = False,
) -> str:
    """옷장에 새 옷을 바로 추가하고, 저장한 값(발급한 id 포함)을 그대로 돌려준다.

    "카멜 울 코트 추가해줘"처럼 옷을 새로 등록할 때 쓴다. 종류·두께·색·격식·방수는 이름에서 추정해 채운다.
    같은 이름이 이미 있으면 추가하지 않고 안내한다. 세탁 상태를 바꿀 때는 쓰지 않는다(update_status).

    Args:
        name: "색 + 종류" 이름.
        category: top / bottom / outer / shoes / accessory.
        warmth: 1~5.
        color: 영문 소문자 색 이름.
        formality: casual / smart / formal / sport.
        waterproof: 방수면 true.
    """
    try:
        item = {"name": name.strip(), "category": category, "warmth": warmth, "color": color,
                "formality": formality, "waterproof": waterproof}
        result = wardrobe.add_clothing(item)
        return result if isinstance(result, str) else _dump({"saved": result})
    except Exception as e:
        return _error(e)


@mcp.tool()
def update_status(
    item_id: Annotated[str, Field(description="옷 id (예: c006)", max_length=30)],
    status: Annotated[Literal["available", "laundry"], Field(description="available=입을 수 있음, laundry=세탁 중")],
) -> str:
    """옷의 세탁 상태를 바로 바꾸고 바뀐 값을 돌려준다.

    "네이비 니트 세탁 넣었어"(laundry), "회색 후드티 빨래 끝났어/세탁 다 했어"(available)처럼 세탁 상태가 바뀔 때 쓴다.
    id를 모르면 먼저 list_clothes로 찾는다. 앞선 대화에서 바꾼 적이 있어도 상태를 바꿀 때마다 새로 호출한다.
    옷을 새로 추가할 때는 쓰지 않는다(add_clothing).

    Args:
        item_id: 옷 id.
        status: available / laundry.
    """
    try:
        result = wardrobe.set_status(item_id.strip(), status)
        return result if isinstance(result, str) else _dump(result)
    except Exception as e:
        return _error(e)


@mcp.tool()
def add_event(
    date: Annotated[str, Field(description=DATE_HELP, max_length=20)],
    title: Annotated[str, Field(description="일정 제목 (예: 저녁 데이트)", min_length=1, max_length=40)],
    type: Annotated[Literal["date", "workout", "formal", "outdoor", "etc"], Field(description="데이트/운동/격식 자리/야외 활동/그 밖")],
    time: Annotated[str, Field(description="24시간 HH:MM (예: 19:00). 모르면 빈 값", pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$")] = "",
) -> str:
    """내 캘린더에 일정을 바로 추가하고, 저장한 날짜(코드가 해석한 날짜)·시간·종류를 돌려준다.

    "토요일 저녁 7시 데이트 일정 추가해줘"처럼 일정을 등록할 때 쓴다. 제목이나 날짜가 불분명하면 호출하지 말고 되묻는다.
    일정을 조회할 때는 쓰지 않는다(get_calendar).

    Args:
        date: 사용자가 말한 날짜 표현 그대로("토요일", "이번 주 토요일", "11월 7일") 또는 YYYY-MM-DD.
        title: 일정 제목.
        type: date / workout / formal / outdoor / etc.
        time: HH:MM 또는 빈 값.
    """
    day = resolve_date(date)
    if day is None:
        return _bad_date(date)
    try:
        saved = wardrobe.add_event({"date": day.isoformat(), "title": title.strip(), "type": type, "time": time})
        return _dump({"saved": saved})
    except Exception as e:
        return _error(e)


if __name__ == "__main__":
    mcp.run(show_banner=False)
