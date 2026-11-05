"""옷장 저장소(JSON)와 날씨 기준 1차 필터."""

import json
import os
import tempfile
from datetime import date

from src.config import CALENDAR_PATH, CLOTHES_PATH, PROFILE_PATH

CATEGORIES = ("top", "bottom", "outer", "shoes", "accessory")

# 기준 온도 하한(이상) → 카테고리별 warmth 범위. outer None은 겉옷이 필요 없는 기온이다.
TEMP_BANDS = [
    (28, {"top": (1, 1), "bottom": (1, 1), "outer": None}),
    (23, {"top": (1, 2), "bottom": (1, 2), "outer": None}),
    (20, {"top": (2, 2), "bottom": (2, 2), "outer": (1, 1)}),
    (17, {"top": (2, 3), "bottom": (2, 3), "outer": (1, 2)}),
    (12, {"top": (2, 3), "bottom": (3, 3), "outer": (2, 3)}),
    (9, {"top": (3, 3), "bottom": (3, 3), "outer": (3, 4)}),
    (5, {"top": (3, 4), "bottom": (3, 4), "outer": (4, 4)}),
    (float("-inf"), {"top": (4, 5), "bottom": (4, 4), "outer": (5, 5)}),
]
OPTIONAL_OUTER_FROM = 20  # 이 기준 온도 이상이면 겉옷은 없어도 된다
MAX_WIDEN = 2             # 후보가 0개일 때 범위를 넓히는 최대 단계


def read_json(path: str):
    """JSON 파일을 읽는다."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data) -> None:
    """JSON 파일을 임시 파일에 쓴 뒤 교체한다(쓰다 끊겨도 원본이 깨지지 않게)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load_clothes() -> list[dict]:
    """옷 목록 전체."""
    return read_json(CLOTHES_PATH)


def load_profile() -> dict:
    """사용자 프로필(지역, 추위 민감도, 선호 스타일 등)."""
    return read_json(PROFILE_PATH)


def events_on(day: date) -> list[dict]:
    """해당 날짜의 캘린더 일정(시간순). 없으면 빈 목록."""
    events = [e for e in read_json(CALENDAR_PATH) if e["date"] == day.isoformat()]
    return sorted(events, key=lambda e: e.get("time", ""))


def warmth_range(category: str, base_temp: float) -> tuple[int, int] | None:
    """기준 온도에 맞는 카테고리의 warmth 범위. 겉옷이 필요 없는 기온이면 None."""
    for lower, ranges in TEMP_BANDS:
        if base_temp >= lower:
            return ranges[category]
    raise AssertionError("TEMP_BANDS의 마지막 하한은 -inf여야 한다")


def _item_view(item: dict, umbrella: bool) -> dict:
    """후보로 내보낼 옷 정보. 비 오는 날 방수 아우터·신발에는 rain_ok를 붙인다."""
    view = {k: item[k] for k in ("id", "name", "warmth", "color", "formality", "waterproof")}
    if umbrella and item["waterproof"] and item["category"] in ("outer", "shoes"):
        view["rain_ok"] = True
    return view


def filter_candidates(weather: dict, profile: dict, clothes: list[dict]) -> dict:
    """오늘 날씨 요약과 프로필로 카테고리별 후보 옷을 고른다(extra.md §7).

    기준 온도 = 구간 체감온도 − 추위 민감도×2. top은 낮, outer는 출근, bottom은 둘의 평균을 쓴다.
    세탁 중인 옷은 빼고, layer면 top 범위를 1 낮추고, 후보가 0개면 범위를 넓혀 note에 밝힌다.
    """
    sensitivity = profile.get("cold_sensitivity", 0)
    am = weather["segments"]["am"]["feels"] - sensitivity * 2
    day = weather["segments"]["day"]["feels"] - sensitivity * 2
    base = {"top": day, "bottom": (am + day) / 2, "outer": am}
    flags = weather["flags"]
    available = [c for c in clothes if c["status"] == "available"]

    result = {}
    for cat in CATEGORIES:
        pool = [c for c in available if c["category"] == cat]
        entry = {}
        if cat in base:
            rng = warmth_range(cat, base[cat])
            entry["base_temp"] = round(base[cat], 1)
            if rng is None:
                result[cat] = {**entry, "warmth": None, "items": [], "note": "겉옷이 필요 없는 기온입니다"}
                continue
            if cat == "top" and flags.get("layer"):
                rng = (max(1, rng[0] - 1), max(1, rng[1] - 1))
            optional = cat == "outer" and base[cat] >= OPTIONAL_OUTER_FROM
            picked = [c for c in pool if rng[0] <= c["warmth"] <= rng[1]]
            note = "겉옷은 없어도 됩니다" if optional else ""
            if not picked and not optional:
                note = "해당 카테고리에 맞는 옷이 없습니다"
                for step in range(1, MAX_WIDEN + 1):
                    wide = (max(1, rng[0] - step), min(5, rng[1] + step))
                    picked = [c for c in pool if wide[0] <= c["warmth"] <= wide[1]]
                    if picked:
                        note = f"warmth {rng[0]}~{rng[1]}에 맞는 옷이 없어 {wide[0]}~{wide[1]}로 넓혔습니다"
                        break
            entry["warmth"] = list(rng)
        else:
            picked = pool  # shoes·accessory는 warmth로 거르지 않는다
            note = ""
        entry["items"] = [_item_view(c, flags.get("umbrella", False)) for c in picked]
        if note:
            entry["note"] = note
        result[cat] = entry

    return {"date": weather["date"], "cold_sensitivity": sensitivity, "flags": flags, "categories": result}
