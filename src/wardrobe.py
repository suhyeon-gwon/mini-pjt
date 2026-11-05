"""옷장 저장소(JSON)와 날씨 기준 1차 필터."""

import json
import os
import tempfile
from datetime import date, timedelta

from src.config import CALENDAR_PATH, CLOTHES_PATH, PROFILE_PATH, RECOMMENDATIONS_PATH, WEAR_LOG_PATH

CATEGORIES = ("top", "bottom", "outer", "shoes", "accessory")
SIMILAR_FEELS = 2.0  # 비슷한 기온의 날: 출근·낮 체감온도 차이가 각각 이 이내
SIMILAR_LIMIT = 3

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


def load_wear_log() -> list[dict]:
    """착용 기록 전체(날짜별 입은 옷, 날씨 요약, 피드백)."""
    return read_json(WEAR_LOG_PATH)


def recent_wear(today_iso: str, days: int) -> list[dict]:
    """오늘을 포함한 최근 days일의 착용 기록(최신순). 옷 id 대신 이름을 담는다."""
    names = {c["id"]: c["name"] for c in load_clothes()}
    start = (date.fromisoformat(today_iso) - timedelta(days=days - 1)).isoformat()
    rows = [e for e in load_wear_log() if start <= e["date"] <= today_iso]
    return [
        {
            "date": e["date"],
            "items": [names.get(i, i) for i in e.get("item_ids", [])],
            "source": e["source"],
            "feedback": [{"text": f["text"], "direction": f["direction"]} for f in e.get("feedback", [])],
        }
        for e in sorted(rows, key=lambda e: e["date"], reverse=True)
    ]


def recommendation_on(day_iso: str) -> dict | None:
    """그날 추천 후보(번호별 옷 id·이름). 그날 추천이 없으면 None."""
    rec = next((r for r in read_json(RECOMMENDATIONS_PATH) if r["date"] == day_iso), None)
    if rec is None:
        return None
    names = {c["id"]: c["name"] for c in load_clothes()}
    return {
        "date": day_iso,
        "candidates": [
            {"no": c["no"], "items": [{"id": i, "name": names.get(i, i)} for i in c["item_ids"]]}
            for c in rec["candidates"]
        ],
    }


def weather_record(weather: dict) -> dict:
    """착용·추천 기록에 함께 저장할 날씨 요약(출근·낮·퇴근 체감온도, 일교차, 비 여부)."""
    seg = weather["segments"]
    return {
        "am_feels": seg["am"]["feels"],
        "day_feels": seg["day"]["feels"],
        "pm_feels": seg["pm"]["feels"],
        "range": weather["temp_range"],
        "rain": weather["flags"]["umbrella"],
    }


def save_recommendation(day_iso: str, candidates: list[list[str]], weather: dict | None) -> None:
    """그날 추천 후보(번호별 item_ids)를 recommendations.json에 저장한다. 같은 날 추천은 마지막 것으로 덮어쓴다."""
    recs = [r for r in read_json(RECOMMENDATIONS_PATH) if r["date"] != day_iso]
    recs.append({
        "date": day_iso,
        "candidates": [{"no": no, "item_ids": ids} for no, ids in enumerate(candidates, 1)],
        "weather": weather,
    })
    recs.sort(key=lambda r: r["date"])
    write_json(RECOMMENDATIONS_PATH, recs)


def write_wear(day_iso: str, item_ids: list[str], source: str, weather: dict | None) -> bool:
    """그날 착용 기록을 쓴다. 쓰면 True, 건드리지 않으면 False.

    자동 기록(auto)은 대화에서 확인된(confirmed) 기록을 덮지 않는다. 어느 쪽이 덮어쓰든 feedback은 유지하고,
    weather가 None이면 기존 날씨 요약을 그대로 둔다.
    """
    log = load_wear_log()
    entry = next((e for e in log if e["date"] == day_iso), None)
    if entry is None:
        log.append({"date": day_iso, "item_ids": item_ids, "source": source, "weather": weather, "feedback": []})
        log.sort(key=lambda e: e["date"])
    elif source == "auto" and entry["source"] == "confirmed":
        return False
    else:
        entry.update({
            "item_ids": item_ids,
            "source": source,
            "weather": weather if weather is not None else entry.get("weather"),
        })
    write_json(WEAR_LOG_PATH, log)
    return True


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


def similar_days(weather: dict, wear_log: list[dict], clothes: list[dict]) -> list[dict]:
    """오늘과 출근·낮 체감온도가 각각 ±2℃ 안인 지난 날을 최근 순으로 최대 3개 찾는다(입은 옷 이름, 피드백 포함).

    날씨 요약이 없거나 입은 옷이 없는 기록, 오늘 기록은 뺀다.
    """
    names = {c["id"]: c["name"] for c in clothes}
    am, day = weather["segments"]["am"]["feels"], weather["segments"]["day"]["feels"]
    found = [
        e for e in wear_log
        if e["date"] < weather["date"] and e.get("weather") and e.get("item_ids")
        and abs(e["weather"]["am_feels"] - am) <= SIMILAR_FEELS
        and abs(e["weather"]["day_feels"] - day) <= SIMILAR_FEELS
    ]
    found.sort(key=lambda e: e["date"], reverse=True)
    return [
        {
            "date": e["date"],
            "weather": e["weather"],
            "items": [names.get(i, i) for i in e["item_ids"]],
            "feedback": [{"text": f["text"], "direction": f["direction"]} for f in e.get("feedback", [])],
        }
        for e in found[:SIMILAR_LIMIT]
    ]


def recently_worn(wear_log: list[dict], today_iso: str, days: int) -> dict[str, int]:
    """최근 days일 안(오늘 제외)에 입은 옷 id → 며칠 전에 입었는지(가장 최근 기준)."""
    today_date = date.fromisoformat(today_iso)
    worn = {}
    for e in wear_log:
        ago = (today_date - date.fromisoformat(e["date"])).days
        if 1 <= ago <= days:
            for item_id in e.get("item_ids", []):
                worn[item_id] = min(ago, worn.get(item_id, ago))
    return worn


def _item_view(item: dict, umbrella: bool, worn: dict[str, int]) -> dict:
    """후보로 내보낼 옷 정보. 비 오는 날 방수 아우터·신발에는 rain_ok, 최근 입은 옷에는 recently_worn을 붙인다."""
    view = {k: item[k] for k in ("id", "name", "warmth", "color", "formality", "waterproof")}
    if umbrella and item["waterproof"] and item["category"] in ("outer", "shoes"):
        view["rain_ok"] = True
    if item["id"] in worn:
        view["recently_worn"] = f"{worn[item['id']]}일 전"
    return view


def filter_candidates(weather: dict, profile: dict, clothes: list[dict], wear_log: list[dict] | None = None) -> dict:
    """오늘 날씨 요약과 프로필로 카테고리별 후보 옷을 고른다(extra.md §7).

    기준 온도 = 구간 체감온도 − 추위 민감도×2. top은 낮, outer는 출근, bottom은 둘의 평균을 쓴다.
    세탁 중인 옷은 빼고, layer면 top 범위를 1 낮추고, 후보가 0개면 범위를 넓혀 note에 밝힌다.
    wear_log가 있으면 최근 입은 옷에 recently_worn을 표시하고(빼지 않는다) 비슷한 기온의 날을 붙인다.
    """
    wear_log = wear_log or []
    worn = recently_worn(wear_log, weather["date"], profile.get("avoid_recent_days", 3))
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
        entry["items"] = [_item_view(c, flags.get("umbrella", False), worn) for c in picked]
        if note:
            entry["note"] = note
        result[cat] = entry

    return {
        "date": weather["date"],
        "cold_sensitivity": sensitivity,
        "flags": flags,
        "categories": result,
        "similar_days": similar_days(weather, wear_log, clothes),
    }
