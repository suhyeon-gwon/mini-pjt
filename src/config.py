"""공통 설정: 경로 상수, LLM 팩토리, 메시지 텍스트 헬퍼, 날짜·날씨 모드 설정."""

import os
import re
from datetime import date, timedelta

from dotenv import load_dotenv

# 경로 (이 파일 기준)
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
FIXTURE_DIR = os.path.join(DATA_DIR, "fixtures")
LOG_DIR = os.path.join(ROOT_DIR, "logs")

CLOTHES_PATH = os.path.join(DATA_DIR, "clothes.json")
WEAR_LOG_PATH = os.path.join(DATA_DIR, "wear_log.json")
PROFILE_PATH = os.path.join(DATA_DIR, "profile.json")
CALENDAR_PATH = os.path.join(DATA_DIR, "calendar.json")
RECOMMENDATIONS_PATH = os.path.join(DATA_DIR, "recommendations.json")

CHECKPOINT_PATH = os.path.join(LOG_DIR, "checkpoints.sqlite")
TRACE_LOG_PATH = os.path.join(LOG_DIR, "trace.jsonl")
AGENT_LOG_PATH = os.path.join(LOG_DIR, "agent.jsonl")
GUARD_AUDIT_PATH = os.path.join(LOG_DIR, "guard_audit.log")

os.makedirs(LOG_DIR, exist_ok=True)
load_dotenv(os.path.join(ROOT_DIR, ".env"))

# 모델
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
REGION = "us-east-1"

# 날씨 모드
WEATHER_MODES = ("live", "fixture")
WEATHER_FIXTURES = ("mild", "rainy", "cold", "error")

# 상대 날짜 해석 (resolve_date)
RELATIVE_DAYS = {"": 0, "오늘": 0, "어제": -1, "그제": -2, "그저께": -2, "내일": 1, "모레": 2}
WEEKDAYS = "월화수목금토일"  # date.weekday() 순서
WEEK_OFFSETS = {"이번주": 0, "다음주": 1, "지난주": -1, "저번주": -1}

# MCP 서버에 env로 넘기는 실행 설정 (비밀 값은 넣지 않는다)
SETTING_KEYS = ("WEATHER_MODE", "WEATHER_FIXTURE", "TODAY_OVERRIDE")


def llm(temperature: float = 0.0):
    """Bedrock Claude 채팅 모델을 만든다. 분류·판정·라우팅·가드레일은 temperature=0으로 부른다."""
    from langchain_aws import ChatBedrockConverse  # MCP 서버는 LLM을 쓰지 않으므로 시작을 늦추지 않게 여기서 import

    return ChatBedrockConverse(model=MODEL_ID, region_name=REGION, temperature=temperature)


def run_settings() -> dict:
    """현재 프로세스 환경변수에서 실행 설정(WEATHER_MODE, WEATHER_FIXTURE, TODAY_OVERRIDE)을 읽는다. 빈 값은 뺀다."""
    return {k: os.environ[k] for k in SETTING_KEYS if os.environ.get(k, "").strip()}


def get_text(message) -> str:
    """메시지의 텍스트를 꺼낸다. content가 블록 리스트여도 text 블록만 이어 붙인다."""
    content = message.content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content


def today() -> date:
    """오늘 날짜. TODAY_OVERRIDE(YYYY-MM-DD)가 있으면 그 값, 없으면 시스템 날짜를 쓴다."""
    override = os.environ.get("TODAY_OVERRIDE", "").strip()
    if not override:
        return date.today()
    try:
        return date.fromisoformat(override)
    except ValueError:
        raise ValueError(f"TODAY_OVERRIDE 형식이 잘못됐습니다: {override!r}. YYYY-MM-DD로 지정하세요.") from None


def resolve_date(text: str) -> date | None:
    """날짜 표현을 날짜로 바꾼다(기준은 today(), 한 주는 월요일 시작). 해석하지 못하면 None.

    받는 표현: 빈 값(오늘), 오늘/어제/그제/그저께/내일/모레, X요일(오늘 포함 다가오는 날),
    이번 주/다음 주/지난주/저번 주 X요일, M월 D일, M/D(올해), YYYY-MM-DD.
    """
    base = today()
    text = re.sub(r"\s+", "", text or "")
    if text in RELATIVE_DAYS:
        return base + timedelta(days=RELATIVE_DAYS[text])

    m = re.fullmatch(r"(이번주|다음주|지난주|저번주)?([월화수목금토일])요일", text)
    if m:
        target = WEEKDAYS.index(m.group(2))
        if m.group(1) is None:
            return base + timedelta(days=(target - base.weekday()) % 7)
        monday = base - timedelta(days=base.weekday()) + timedelta(weeks=WEEK_OFFSETS[m.group(1)])
        return monday + timedelta(days=target)

    m = re.fullmatch(r"(\d{1,2})월(\d{1,2})일|(\d{1,2})/(\d{1,2})", text)
    try:
        if m:
            month, day = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            return date(base.year, int(month), int(day))
        return date.fromisoformat(text) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None
    except ValueError:
        return None


def weather_mode() -> str:
    """날씨 모드(live|fixture). WEATHER_MODE가 없으면 live다. 허용값 검사는 weather.py가 한다."""
    return os.environ.get("WEATHER_MODE", "live").strip().lower() or "live"


def weather_fixture() -> str:
    """fixture 모드에서 읽을 fixture 이름(mild|rainy|cold|error). WEATHER_FIXTURE가 없으면 mild다."""
    return os.environ.get("WEATHER_FIXTURE", "mild").strip().lower() or "mild"
