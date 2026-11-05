"""가드레일: 고정 거절 문구, 감사 로그, 출력 가드레일(output_guard)."""

import json
import re
from datetime import datetime

from src.config import GUARD_AUDIT_PATH
from src.wardrobe import load_clothes

REFUSAL_MESSAGES = {
    "injection": "죄송합니다. 해당 요청은 처리할 수 없습니다. 오늘의 코디 추천, 옷장·일정 관리, 착용 기록을 도와드릴 수 있어요.",
    "off_topic": "죄송합니다. 그 내용은 제가 확인하거나 도울 수 없어요. 옷, 날씨(기온·비·바람), 일정에 관한 것을 물어봐 주세요.",
    "no_outfit": "죄송합니다. 지금은 옷장 안에서 코디를 만들지 못했어요. 다시 한 번 물어봐 주세요.",
}

OUTFIT_LINE = re.compile(r"^(\s*)(\d+)\)\s+(.+?)\s*$")  # 코디 줄: "1) 옷 + 옷 + 옷"


def audit(stage: str, kind: str, detail) -> None:
    """가드레일이 막거나 고친 사유를 logs/guard_audit.log에 JSON 한 줄로 남긴다(사용자에게는 보여주지 않는다)."""
    record = {"at": datetime.now().isoformat(timespec="seconds"), "stage": stage, "kind": kind, "detail": detail}
    with open(GUARD_AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def outfit_lines(text: str) -> list[str]:
    """응답에서 코디 줄("1) …")만 앞뒤 공백을 떼어 꺼낸다."""
    return [line.strip() for line in text.splitlines() if OUTFIT_LINE.match(line)]


def _is_reason_line(line: str) -> bool:
    """코디 줄 아래에 붙는 이유 줄(들여쓴 줄이나 '-'로 시작하는 줄)인지."""
    return bool(line.strip()) and (line[:1].isspace() or line.startswith("-")) and not OUTFIT_LINE.match(line)


def output_guard(text: str) -> tuple[str, list[list[str]]]:
    """최종 응답의 코디 후보를 옷장과 대조해 (내보낼 응답, 남은 후보별 item_ids)를 돌려준다.

    코디 줄을 " + "로 나눠 조각마다 clothes.json의 name과 정확히 일치하는지, 세탁 중이 아닌지 본다.
    걸린 후보는 이유 줄까지 통째로 빼고 남은 후보 번호를 1부터 다시 매기며, 뺀 사유는 감사 로그에 남긴다.
    남은 후보가 없으면 no_outfit 문구로 바꾼다. 코디 줄이 없는 응답(추천이 아닌 답)은 (원문, [])이다.
    """
    lines = text.splitlines()
    if not any(OUTFIT_LINE.match(line) for line in lines):
        return text, []

    by_name = {c["name"]: c for c in load_clothes()}
    out, kept, removed = [], [], []
    i = 0
    while i < len(lines):
        m = OUTFIT_LINE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        end = i + 1
        while end < len(lines) and _is_reason_line(lines[end]):
            end += 1
        names = [p.strip() for p in m.group(3).split(" + ")]
        missing = [n for n in names if n not in by_name]
        laundry = [n for n in names if n in by_name and by_name[n]["status"] == "laundry"]
        if missing or laundry:
            removed.append({"no": int(m.group(2)), "missing": missing, "laundry": laundry})
        else:
            kept.append([by_name[n]["id"] for n in names])
            out.append(f"{m.group(1)}{len(kept)}) {m.group(3)}")
            out.extend(lines[i + 1:end])
        i = end

    if removed:
        audit("output", "outfit_removed", removed)
    if not kept:
        audit("output", "no_outfit", "남은 후보가 없어 no_outfit 문구로 교체")
        return REFUSAL_MESSAGES["no_outfit"], []
    return "\n".join(out), kept
