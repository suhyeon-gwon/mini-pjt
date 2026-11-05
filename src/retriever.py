"""착용 기록 검색(RAG): 검색 문서 생성, 색인 동기화, 조건 필터 + 하이브리드 검색."""

import difflib
import hashlib
import json

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from src.config import CALENDAR_PATH, REGION
from src.wardrobe import load_clothes, load_wear_log, read_json

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
BM25_WEIGHT, VECTOR_WEIGHT = 0.3, 0.7
BM25_TAGS = {"NNG", "NNP", "NNB", "NR", "NP", "VV", "VA", "SL", "SN"}  # 명사·동사·형용사·외국어·숫자
NO_RECORD = "조건에 맞는 착용 기록이 없습니다. 기록을 지어내지 말고 그런 기록이 없다고 안내하세요."

# 서버 프로세스 안에서 이어 쓰는 상태(처음 쓸 때 만든다)
_kiwi = None
_store = None
_embeddings = None
_hashes: dict[str, str] = {}  # 날짜 → 색인에 넣은 문서의 해시


class CachedEmbeddings(Embeddings):
    """임베딩 앞에 둔 본문별 캐시. 같은 본문·질의는 다시 임베딩하지 않는다. embedded는 실제로 임베딩한 본문 수."""

    def __init__(self, inner: Embeddings):
        self.inner = inner
        self.cache: dict[str, list[float]] = {}
        self.embedded = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self.cache]
        if missing:
            self.embedded += len(missing)
            self.cache.update(zip(missing, self.inner.embed_documents(missing)))
        return [self.cache[t] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        if text not in self.cache:
            self.embedded += 1
            self.cache[text] = self.inner.embed_query(text)
        return self.cache[text]


def embed_count() -> int:
    """서버가 뜬 뒤 실제로 임베딩한 본문 수(색인 동기화·질의 캐시 확인용)."""
    return _embeddings.embedded if _embeddings else 0


def _tokenize(text: str) -> list[str]:
    """BM25용 한국어 형태소 토크나이저(kiwipiepy)."""
    global _kiwi
    if _kiwi is None:
        from kiwipiepy import Kiwi
        _kiwi = Kiwi()
    return [t.form for t in _kiwi.tokenize(text) if t.tag in BM25_TAGS]


def _vector_store():
    """인메모리 Chroma(persist_directory 없음). 임베딩은 Titan v2, 리전은 .env의 AWS_DEFAULT_REGION."""
    global _store, _embeddings
    if _store is None:
        from langchain_aws import BedrockEmbeddings
        from langchain_chroma import Chroma
        _embeddings = CachedEmbeddings(BedrockEmbeddings(model_id=EMBED_MODEL, region_name=REGION))
        _store = Chroma(collection_name="wear_history", embedding_function=_embeddings)
    return _store


def _events_by_date(calendar: list[dict]) -> dict[str, list[dict]]:
    """날짜 → 그날 일정(시간순)."""
    events: dict[str, list[dict]] = {}
    for e in sorted(calendar, key=lambda e: e.get("time", "")):
        events.setdefault(e["date"], []).append(e)
    return events


def build_documents(wear_log: list[dict], clothes: list[dict], calendar: list[dict]) -> list[Document]:
    """wear_log 항목 하나를 Document 하나(id = 날짜)로 만든다. 본문은 날씨·일정·입은 옷·피드백을 합친 글이다.

    메타데이터 목록 값(event_types, item_ids, directions)은 쉼표로 이은 문자열이다(Chroma 메타데이터에 리스트를 넣지 않는다).
    """
    names = {c["id"]: c["name"] for c in clothes}
    events = _events_by_date(calendar)
    docs = []
    for rec in wear_log:
        day, weather = rec["date"], rec.get("weather") or {}
        day_events, feedback, item_ids = events.get(day, []), rec.get("feedback", []), rec.get("item_ids", [])
        if weather:
            head = [day, "비 옴" if weather.get("rain") else "비 없음",
                    f"출근 체감 {weather['am_feels']:g}℃, 낮 {weather['day_feels']:g}℃"]
        else:
            head = [day, "날씨 기록 없음"]
        if day_events:
            head.append("일정: " + ", ".join(e["title"] for e in day_events))
        lines = [" · ".join(head), "입은 옷: " + (", ".join(names.get(i, i) for i in item_ids) or "기록 없음")]
        if feedback:
            lines.append("피드백: " + " / ".join(f"{f['text']} ({f['direction']})" for f in feedback))
        docs.append(Document(id=day, page_content="\n".join(lines), metadata={
            "date": day,
            "rain": bool(weather.get("rain")),
            "event_types": ",".join(e["type"] for e in day_events),
            "item_ids": ",".join(item_ids),
            "directions": ",".join(f["direction"] for f in feedback),
        }))
    return docs


def _doc_hash(doc: Document) -> str:
    return hashlib.sha256((doc.page_content + json.dumps(doc.metadata, sort_keys=True)).encode("utf-8")).hexdigest()


def sync_index(docs: list[Document]) -> None:
    """색인을 지금 wear_log에 맞춘다. 본문 해시가 바뀐 문서만 다시 임베딩해 upsert하고, 사라진 날짜는 지운다.

    wear_log는 서버 밖에서도 바뀌므로(ask()의 자동 기록, run_eval의 백업·복원) 검색할 때마다 부른다.
    """
    store = _vector_store()
    current = {d.id: _doc_hash(d) for d in docs}
    gone = [day for day in _hashes if day not in current]
    if gone:
        store.delete(ids=gone)
        for day in gone:
            del _hashes[day]
    changed = [d for d in docs if _hashes.get(d.id) != current[d.id]]
    if changed:
        store.add_documents(changed, ids=[d.id for d in changed])
        _hashes.update({d.id: current[d.id] for d in changed})


def _hybrid(query: str, hits: list[Document], docs: list[Document], k: int) -> list[Document]:
    """조건 필터를 통과한 문서 안에서 BM25 0.3 + 벡터 0.7 하이브리드로 상위 k건을 고른다."""
    from langchain_classic.retrievers import EnsembleRetriever
    from langchain_community.retrievers import BM25Retriever

    sync_index(docs)
    bm25 = BM25Retriever.from_documents(hits, preprocess_func=_tokenize, k=k)  # 남은 문서로 매번 만든다
    vector = _vector_store().as_retriever(
        search_kwargs={"k": k, "filter": {"date": {"$in": [d.metadata["date"] for d in hits]}}})
    ranked = EnsembleRetriever(retrievers=[bm25, vector], weights=[BM25_WEIGHT, VECTOR_WEIGHT]).invoke(query)
    return ranked[:k]


def _record(rec: dict, names: dict, day_events: list[dict]) -> dict:
    """검색 결과 한 건: 날씨, 일정, 입은 옷 이름, 피드백, 기록 종류."""
    return {
        "date": rec["date"],
        "weather": rec.get("weather"),
        "events": [{"title": e["title"], "type": e["type"], "time": e.get("time", "")} for e in day_events],
        "items": [names.get(i, i) for i in rec.get("item_ids", [])],
        "feedback": [{"text": f["text"], "direction": f["direction"]} for f in rec.get("feedback", [])],
        "source": rec.get("source"),
    }


def search(query: str, rain: str = "", event_type: str = "", item_name: str = "", direction: str = "",
           k: int = 3) -> dict | str:
    """조건 필터(코드) → 하이브리드 검색으로 지난 착용 기록을 찾는다.

    rain·event_type·direction·item_name으로 먼저 거르고, 0건이면 안내 문자열, k건 이하면 검색 없이 최신순 전부,
    그보다 많으면 하이브리드 검색 상위 k건을 돌려준다. item_name은 clothes.json의 name과 정확히 일치해야 한다.
    """
    clothes, wear_log, calendar = load_clothes(), load_wear_log(), read_json(CALENDAR_PATH)
    names = {c["id"]: c["name"] for c in clothes}
    item_id = ""
    if item_name.strip():
        ids = {name: i for i, name in names.items()}
        item_id = ids.get(item_name.strip(), "")
        if not item_id:
            close = difflib.get_close_matches(item_name.strip(), list(ids), n=5, cutoff=0.3) or list(ids)
            return f"옷장에 없는 옷입니다. 가능한 이름: {', '.join(close)}. 이 중 하나로 다시 호출하거나, 그 옷을 입은 기록은 없다고 안내하세요."

    def keep(m: dict) -> bool:
        return ((not rain or m["rain"] == (rain == "yes"))
                and (not event_type or event_type in m["event_types"].split(","))
                and (not item_id or item_id in m["item_ids"].split(","))
                and (not direction or direction in m["directions"].split(",")))

    docs = build_documents(wear_log, clothes, calendar)
    hits = [d for d in docs if keep(d.metadata)]
    if not hits:
        return NO_RECORD
    if len(hits) > k:
        hits = _hybrid(query, hits, docs, k)
    else:
        hits = sorted(hits, key=lambda d: d.metadata["date"], reverse=True)

    by_day, events = {r["date"]: r for r in wear_log}, _events_by_date(calendar)
    filters = {key: value for key, value in
               {"rain": rain, "event_type": event_type, "item_name": item_name.strip(), "direction": direction}.items() if value}
    records = [_record(by_day[d.metadata["date"]], names, events.get(d.metadata["date"], [])) for d in hits]
    return {"query": query, "filters": filters, "count": len(records), "records": records}
