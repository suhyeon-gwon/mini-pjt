"""피드백 기록, 추위 민감도 변경 제안값 계산, 프로필 변경 검증·반영."""

import re
from datetime import datetime

from src.config import PROFILE_PATH, WEAR_LOG_PATH
from src.wardrobe import load_profile, load_wear_log, write_json
from src.weather import REGION_GRID

SENSITIVITY_MIN, SENSITIVITY_MAX = -2, 2
PROFILE_FIELDS = ("cold_sensitivity", "preferred_styles", "region")


def suggest_change(sensitivity: int, direction: str) -> dict | None:
    """피드백 방향에 따른 추위 민감도 변경 제안. cold는 +1, hot은 −1이고 범위(−2~+2)를 넘으면 제안하지 않는다."""
    step = {"cold": 1, "hot": -1}.get(direction)
    if step is None or not SENSITIVITY_MIN <= sensitivity + step <= SENSITIVITY_MAX:
        return None
    return {"field": "cold_sensitivity", "from": sensitivity, "to": sensitivity + step}


def record_feedback(day_iso: str, text: str, direction: str) -> dict:
    """그날 착용 기록에 피드백을 덧붙인다. 그날 기록이 없으면 feedback_only 항목을 만든다.

    반환: 기록한 날짜·내용과, 바꿀 여지가 있으면 suggest_profile_change(추위 민감도 ±1 제안).
    """
    log = load_wear_log()
    entry = next((e for e in log if e["date"] == day_iso), None)
    if entry is None:
        entry = {"date": day_iso, "item_ids": [], "source": "feedback_only", "weather": None, "feedback": []}
        log.append(entry)
        log.sort(key=lambda e: e["date"])
    entry.setdefault("feedback", []).append(
        {"text": text, "direction": direction, "at": datetime.now().isoformat(timespec="seconds")}
    )
    write_json(WEAR_LOG_PATH, log)

    result = {"date": day_iso, "recorded": text, "direction": direction, "source": entry["source"]}
    suggestion = suggest_change(load_profile().get("cold_sensitivity", 0), direction)
    if suggestion:
        result["suggest_profile_change"] = suggestion
    elif direction in ("cold", "hot"):
        result["note"] = "추위 민감도가 이미 범위 끝(−2~+2)이라 더 바꿀 수 없습니다"
    return result


def _as_int(value) -> int | None:
    """정수나 정수 모양 문자열("+1", "-1")을 int로. 아니면 None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def change_rules() -> dict:
    """프로필 항목별로 바꿀 수 있는 값(change_profile의 검증 규칙을 사람이 읽는 문장으로)."""
    current = load_profile().get("cold_sensitivity", 0)
    steps = [v for v in (current - 1, current + 1) if SENSITIVITY_MIN <= v <= SENSITIVITY_MAX]
    return {
        "cold_sensitivity": f"정수 {SENSITIVITY_MIN}~+{SENSITIVITY_MAX}, 한 번에 1단계만(지금 {current}에서 바꿀 수 있는 값: "
                            f"{', '.join(map(str, steps))})",
        "preferred_styles": "스타일 이름 목록",
        "region": f"가능한 지역: {', '.join(REGION_GRID)}",
    }


def change_profile(field: str, value) -> dict | str:
    """프로필 한 항목을 검증하고 반영한다. 반영하면 {field, from, to}, 검증에 걸리면 다시 호출 방법을 담은 안내 문자열.

    cold_sensitivity는 정수 −2~+2, 한 번에 ±1까지. preferred_styles는 문자열 목록.
    region은 지역→격자 표에 있는 이름만 받고 nx, ny를 함께 바꾼다.
    """
    profile = load_profile()
    if field == "cold_sensitivity":
        new, old = _as_int(value), profile.get("cold_sensitivity", 0)
        if new is None:
            return f"에러: cold_sensitivity는 정수여야 합니다(받은 값: {value!r}). 현재 값 {old}에서 ±1인 정수로 다시 호출하세요."
        if not SENSITIVITY_MIN <= new <= SENSITIVITY_MAX:
            return f"에러: 추위 민감도는 {SENSITIVITY_MIN}~+{SENSITIVITY_MAX} 범위만 가능합니다(요청 값: {new}). 바꾸지 않았습니다."
        if abs(new - old) > 1:
            return f"에러: 추위 민감도는 한 번에 1단계만 바꿀 수 있습니다(현재 {old}, 요청 {new}). {old - 1 if new < old else old + 1}로 다시 호출하세요."
        profile[field] = new
    elif field == "preferred_styles":
        styles = [s.strip() for s in value.split(",")] if isinstance(value, str) else value
        if not isinstance(styles, list) or not all(isinstance(s, str) and s.strip() for s in styles):
            return '에러: preferred_styles는 스타일 이름 목록이어야 합니다(예: ["minimal", "casual"]). 다시 호출하세요.'
        old, profile[field] = profile.get(field), [s.strip() for s in styles]
    elif field == "region":
        name = value.strip() if isinstance(value, str) else ""
        if name not in REGION_GRID:
            return f"에러: '{value}'은(는) 지원하지 않는 지역입니다. 가능한 지역: {', '.join(REGION_GRID)}"
        old = profile.get("region")
        profile["region"], (profile["nx"], profile["ny"]) = name, REGION_GRID[name]
    else:
        return f"에러: '{field}'은(는) 바꿀 수 없는 항목입니다. 가능한 항목: {', '.join(PROFILE_FIELDS)}"
    write_json(PROFILE_PATH, profile)
    return {"field": field, "from": old, "to": profile[field]}
