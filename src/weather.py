"""기상청 단기예보: 응답 원문 받기, 시간별 표 파싱, 구간 요약, 체감온도, 플래그."""

import copy
import json
import os
import re
from datetime import date, timedelta
from statistics import mean

from src.config import FIXTURE_DIR, WEATHER_FIXTURES, WEATHER_MODES, today, weather_fixture, weather_mode

CATEGORIES = ("TMP", "POP", "PTY", "PCP", "WSD", "SKY", "REH")
HOURS = range(6, 23)  # 06~22시
SEGMENTS = {  # 키: (이름, 시작 시, 끝 시)
    "am": ("출근", 7, 9),
    "day": ("낮", 12, 15),
    "pm": ("퇴근", 18, 20),
}
PTY_NAMES = {0: "없음", 1: "비", 2: "비/눈", 3: "눈", 4: "소나기"}
SKY_NAMES = {1: "맑음", 3: "구름많음", 4: "흐림"}

UMBRELLA_POP = 60   # 이 강수확률 이상이면 비 오는 시간
LAYER_RANGE = 10    # 일교차가 이 이상이면 벗기 쉬운 겉옷
WINDY_WSD = 7.0     # 최대 풍속(m/s)이 이 이상이면 바람막이

FAIL_PREFIX = "날씨 조회 실패: "
NO_GUESS = "날씨를 추정해 추천하지 말고 사용자에게 조회 실패를 알리세요."


def fetch_raw() -> dict | str:
    """오늘 예보의 기상청 응답 원문을 가져온다. 실패하면 안내 문자열을 돌려준다.

    fixture 모드는 data/fixtures 파일을 읽고, live 모드는 아직 연결되지 않아 실패 안내를 돌려준다.
    """
    mode = weather_mode()
    if mode == "fixture":
        return _read_fixture(weather_fixture())
    if mode == "live":
        return FAIL_PREFIX + "기상청 실API가 아직 연결되지 않았습니다. WEATHER_MODE=fixture로 실행하세요."
    return FAIL_PREFIX + f"WEATHER_MODE 값 {mode!r}은(는) 지원하지 않습니다. {'/'.join(WEATHER_MODES)} 중 하나로 지정하세요."


def _read_fixture(name: str) -> dict | str:
    """fixture 파일을 읽어 날짜를 today()로 옮긴 응답 원문을 돌려준다."""
    if name not in WEATHER_FIXTURES:
        return FAIL_PREFIX + f"WEATHER_FIXTURE 값 {name!r}은(는) 없습니다. {'/'.join(WEATHER_FIXTURES)} 중 하나로 지정하세요."
    with open(os.path.join(FIXTURE_DIR, f"weather_{name}.json"), encoding="utf-8") as f:
        raw = json.load(f)
    return shift_dates(raw, today())


def _items(raw: dict) -> list[dict]:
    """응답 원문에서 예보 항목 목록을 꺼낸다. 항목이 없으면 빈 목록."""
    items = raw.get("response", {}).get("body", {}).get("items", {})
    return items.get("item", []) if isinstance(items, dict) else []


def _ymd(text: str) -> date:
    """YYYYMMDD 문자열을 날짜로 바꾼다."""
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def shift_dates(raw: dict, target: date) -> dict:
    """응답 원문의 baseDate·fcstDate를 첫 항목의 baseDate가 target이 되도록 같은 날수만큼 옮긴다."""
    items = _items(raw)
    if not items:
        return raw
    delta = target - _ymd(items[0]["baseDate"])
    if delta == timedelta(0):
        return raw
    shifted = copy.deepcopy(raw)
    for it in _items(shifted):
        it["baseDate"] = (_ymd(it["baseDate"]) + delta).strftime("%Y%m%d")
        it["fcstDate"] = (_ymd(it["fcstDate"]) + delta).strftime("%Y%m%d")
    return shifted


def _pcp_mm(text: str) -> float:
    """PCP 문구를 mm 숫자로 바꾼다. '강수없음'은 0, '1mm 미만'은 0.5, 범위·이상 표현은 아래 끝값."""
    if text == "강수없음":
        return 0.0
    if "미만" in text:
        return 0.5
    m = re.search(r"\d+(\.\d+)?", text)
    return float(m.group()) if m else 0.0


def _value(category: str, text: str):
    """예보 항목 값을 항목별 타입으로 바꾼다."""
    if category == "PCP":
        return _pcp_mm(text)
    if category in ("TMP", "WSD"):
        return float(text)
    return int(float(text))


def parse_hourly(raw: dict, day: date) -> list[dict] | str:
    """응답 원문에서 day의 06~22시 시간별 표를 만든다. 응답 오류나 빠진 시각이 있으면 안내 문자열."""
    header = raw.get("response", {}).get("header", {})
    code = header.get("resultCode")
    if code != "00":
        return FAIL_PREFIX + f"기상청 응답 오류({code} {header.get('resultMsg', '')}). {NO_GUESS}"
    key = day.strftime("%Y%m%d")
    table = {h: {} for h in HOURS}
    for it in _items(raw):
        if it.get("fcstDate") != key or it.get("category") not in CATEGORIES:
            continue
        hour = int(it["fcstTime"][:2])
        if hour in table:
            table[hour][it["category"]] = _value(it["category"], it["fcstValue"])
    missing = [f"{h:02d}시" for h, row in table.items() if len(row) < len(CATEGORIES)]
    if missing:
        return FAIL_PREFIX + f"{day} 예보에 빠진 시각이 있습니다({', '.join(missing)}). {NO_GUESS}"
    return [{"hour": h, **row} for h, row in table.items()]


def feels_like(temp: float, wsd: float) -> float:
    """체감온도(기상청 겨울철 공식). 기온 10℃ 이하이고 풍속 1.3m/s 이상일 때만 계산하고, 아니면 기온 그대로."""
    if temp <= 10 and wsd >= 1.3:
        v = (wsd * 3.6) ** 0.16
        return 13.12 + 0.6215 * temp - 11.37 * v + 0.3965 * temp * v
    return temp


def _spans(hours: list[int]) -> str:
    """연속된 시각을 묶어 '15~18시', '07시, 15~18시' 같은 문구로 만든다."""
    groups = []
    for h in hours:
        if groups and h == groups[-1][1] + 1:
            groups[-1][1] = h
        else:
            groups.append([h, h])
    return ", ".join(f"{a:02d}시" if a == b else f"{a:02d}~{b:02d}시" for a, b in groups)


def summarize(rows: list[dict], day: date) -> dict:
    """시간별 표로 출근/낮/퇴근 구간 요약, 최저·최고·일교차, 비 시간대, 플래그, 챙길 것을 만든다.

    구간 체감온도는 시각별 체감온도의 평균이다. 최저·최고는 06~22시 TMP로 계산한다.
    """
    by_hour = {r["hour"]: r for r in rows}
    segments = {}
    for key, (label, start, end) in SEGMENTS.items():
        seg = [by_hour[h] for h in range(start, end + 1)]
        segments[key] = {
            "label": label,
            "hours": f"{start:02d}~{end:02d}시",
            "temp": round(mean(r["TMP"] for r in seg), 1),
            "feels": round(mean(feels_like(r["TMP"], r["WSD"]) for r in seg), 1),
            "wind_max": max(r["WSD"] for r in seg),
            "pop_max": max(r["POP"] for r in seg),
            "pty": PTY_NAMES.get(max(r["PTY"] for r in seg), "알 수 없음"),
            "sky": SKY_NAMES.get(max(r["SKY"] for r in seg), "알 수 없음"),
        }

    temps = [r["TMP"] for r in rows]
    t_min, t_max = min(temps), max(temps)
    t_range = round(t_max - t_min, 1)
    wind_max = max(r["WSD"] for r in rows)
    rain_hours = [r["hour"] for r in rows if r["POP"] >= UMBRELLA_POP or r["PTY"] != 0]
    rain_text = _spans(rain_hours)

    flags = {
        "umbrella": bool(rain_hours),
        "layer": t_range >= LAYER_RANGE,
        "windy": wind_max >= WINDY_WSD,
    }
    bring = []
    if flags["umbrella"]:
        bring.append(f"우산 ({rain_text} 비)")
    if flags["layer"]:
        bring.append(f"벗기 쉬운 겉옷 (일교차 {t_range:g}℃)")
    if flags["windy"]:
        bring.append("바람막이 고려")

    return {
        "date": day.isoformat(),
        "segments": segments,
        "min_temp": t_min,
        "max_temp": t_max,
        "temp_range": t_range,
        "wind_max": wind_max,
        "rain_hours": rain_text,
        "rain_pop_max": max((by_hour[h]["POP"] for h in rain_hours), default=0),
        "flags": flags,
        "bring": bring,
    }


def today_weather() -> dict | str:
    """오늘(today()) 06~22시 예보의 구간 요약·플래그·챙길 것을 돌려준다. 실패하면 안내 문자열."""
    try:
        raw = fetch_raw()
        if isinstance(raw, str):
            return raw
        day = today()
        rows = parse_hourly(raw, day)
        if isinstance(rows, str):
            return rows
        return summarize(rows, day)
    except (OSError, KeyError, TypeError, ValueError) as e:
        return FAIL_PREFIX + f"예보를 읽거나 해석하지 못했습니다({type(e).__name__}). {NO_GUESS}"
