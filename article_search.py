"""
기사 의미기반(semantic) 검색 - ChromaDB 대체.

설계:
  기사 원문/청크는 MySQL(espo) 의 article / articlechunk 에만 존재한다(단일 원천).
  임베딩은 그 데이터에서 파생되는 "캐시"일 뿐이므로 별도 벡터DB(ChromaDB)가 필요 없다.

동작:
  - 최초 검색 시 MySQL articlechunk 를 전부 읽어 ko-sroberta 로 임베딩하고,
    L2 정규화한 행렬을 메모리에 올린다(코사인 유사도 = 행렬곱).
  - 청크 수가 수백~수천 규모라 numpy 만으로 충분히 빠르다(faiss 등 불필요).
  - 임베딩은 ./article_embeddings.npz 에 캐시한다. 청크 개수가 그대로면
    재기동 시 재계산 없이 즉시 로드한다.

공개 API:
  search_articles(question, top_k=3) -> list[str]   # 관련 청크 본문 목록
  (기존 rag_answer.search_articles 와 시그니처 동일 → pipeline 교체만으로 대체 가능)
"""

import os
from datetime import datetime

import numpy as np
from sentence_transformers import SentenceTransformer

from mysql_connect import get_connection

_MODEL_NAME = "jhgan/ko-sroberta-multitask"
_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "article_embeddings.npz",
)

# 2025 LCK 시즌 범위. season_scoped 검색일 때 이 범위 밖 기사(시즌후 이적 등)를 제외한다.
SEASON_START = datetime(2025, 4, 2, 0, 0, 0)
SEASON_END = datetime(2025, 9, 28, 23, 59, 59)


def _to_datetime(value):
    """published_at(datetime 또는 문자열)을 datetime으로. 파싱 실패 시 None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _in_season(published_at):
    """기사 발행일이 2025 LCK 시즌 범위 안이면 True. 날짜 불명이면 배제하지 않는다(True)."""
    dt = _to_datetime(published_at)
    if dt is None:
        return True
    return SEASON_START <= dt <= SEASON_END


def _season_mask():
    """_chunk_rows 각 청크가 시즌 범위 안인지 여부의 boolean 배열."""
    if not _chunk_rows:
        return None
    return np.array([_in_season(r.get("published_at")) for r in _chunk_rows], dtype=bool)


def _period_mask(start_date=None, end_date=None):
    """질문에서 확정된 날짜 범위에 속하는 청크 마스크를 반환한다."""
    start = _to_datetime(start_date)
    end = _to_datetime(end_date)
    if end is not None and len(str(end_date or "")) <= 10:
        end = end.replace(hour=23, minute=59, second=59)
    if not _chunk_rows or (start is None and end is None):
        return None
    values = []
    for row in _chunk_rows:
        published = _to_datetime(row.get("published_at"))
        values.append(
            published is None
            or ((start is None or published >= start) and (end is None or published <= end))
        )
    return np.array(values, dtype=bool)


def _article_ids_for_entities(entity_names):
    """canonical/alias로 확인된 엔티티가 실제 등장한 전체 article_id를 반환한다."""
    names = list(dict.fromkeys(
        str(value or "").strip().casefold()
        for value in (entity_names or [])
        if str(value or "").strip()
    ))
    if not names:
        return set()

    placeholders = ", ".join(["%s"] * len(names))
    sql = f"""
        SELECT DISTINCT ae.article_id
        FROM articleentity ae
        JOIN entity e ON e.entity_id = ae.entity_id
        LEFT JOIN entityalias ea ON ea.entity_id = e.entity_id
        WHERE LOWER(e.canonical_name) IN ({placeholders})
           OR LOWER(ea.alias_name) IN ({placeholders})
           OR LOWER(ae.entity_text) IN ({placeholders})
           OR LOWER(ae.matched_alias) IN ({placeholders})
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (*names, *names, *names, *names))
            return {row["article_id"] for row in cur.fetchall()}
    finally:
        conn.close()

# 지연 로딩용 전역 상태
_model = None
_chunk_texts = None      # list[str]
_chunk_rows = None       # list[dict], UI 근거용 기사/청크 메타데이터
_chunk_matrix = None     # np.ndarray (N, dim), L2 정규화됨


def _get_model():
    global _model
    if _model is None:
        print("★ article_search: 임베딩 모델 로드", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _load_chunks_from_mysql():
    """MySQL articlechunk 에서 (chunk_text) 목록을 순서대로 읽는다."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ac.chunk_id,
                    ac.article_id,
                    ac.chunk_order,
                    ac.chunk_text,
                    a.title,
                    a.content,
                    a.provider,
                    a.published_at,
                    a.url
                FROM articlechunk ac
                LEFT JOIN article a ON a.article_id = ac.article_id
                WHERE ac.chunk_text IS NOT NULL AND ac.chunk_text <> ''
                ORDER BY ac.chunk_id
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return rows


def _normalize(mat):
    """행 단위 L2 정규화. 0벡터는 그대로 둔다."""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _build_index():
    """MySQL 청크 → 임베딩 행렬. 디스크 캐시가 최신이면 재사용."""
    global _chunk_texts, _chunk_rows, _chunk_matrix

    rows = _load_chunks_from_mysql()
    texts = [row["chunk_text"] for row in rows]

    if not texts:
        print("DEBUG article_search: articlechunk 가 비어있음")
        _chunk_texts = []
        _chunk_rows = []
        _chunk_matrix = np.zeros((0, 1), dtype=np.float32)
        return

    # 디스크 캐시: 청크 개수가 동일하면 임베딩 재계산 생략
    if os.path.exists(_CACHE_PATH):
        try:
            cached = np.load(_CACHE_PATH, allow_pickle=True)
            if int(cached["count"]) == len(texts):
                _chunk_texts = list(cached["texts"])
                _chunk_rows = rows
                _chunk_matrix = cached["matrix"].astype(np.float32)
                print(f"DEBUG article_search: 캐시 로드 {len(_chunk_texts)}청크")
                return
        except Exception as e:
            print("DEBUG article_search: 캐시 로드 실패, 재계산:", e)

    model = _get_model()
    emb = model.encode(texts, batch_size=32, show_progress_bar=False)
    emb = np.asarray(emb, dtype=np.float32)

    _chunk_matrix = _normalize(emb)
    _chunk_texts = texts
    _chunk_rows = rows

    try:
        np.savez(
            _CACHE_PATH,
            count=len(texts),
            texts=np.array(texts, dtype=object),
            matrix=_chunk_matrix,
        )
        print(f"DEBUG article_search: 임베딩 계산·캐시 저장 {len(texts)}청크")
    except Exception as e:
        print("DEBUG article_search: 캐시 저장 실패(무시):", e)


def _ensure_index():
    if _chunk_matrix is None:
        _build_index()


def _masked_scores(scores, season_scoped):
    """season_scoped면 시즌 범위 밖 청크의 점수를 -inf로 눌러 top-k에서 배제한다."""
    if not season_scoped:
        return scores
    mask = _season_mask()
    if mask is None or len(mask) != len(scores) or not mask.any():
        return scores  # 정렬 불가/전부 배제되는 이상 상황이면 필터를 포기(빈 결과 방지)
    return np.where(mask, scores, -np.inf)


def search_articles(question, top_k=3, season_scoped=False):
    """질문과 의미상 가장 가까운 기사 청크 top_k 개의 본문을 반환한다.

    season_scoped=True 면 2025 LCK 시즌 범위 밖 기사(시즌후 이적 등)를 제외하고 검색한다.
    """
    _ensure_index()

    if _chunk_matrix is None or len(_chunk_texts) == 0:
        return []
    if top_k is not None and int(top_k) <= 0:
        return []

    q = np.asarray(_get_model().encode(question), dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)

    scores = _masked_scores(_chunk_matrix @ q, season_scoped)  # 코사인 유사도(둘 다 정규화됨)

    k = min(top_k, len(_chunk_texts))
    top_idx = np.argpartition(-scores, k - 1)[:k]
    top_idx = top_idx[np.argsort(-scores[top_idx])]

    return [_chunk_texts[i] for i in top_idx]


def search_article_evidence(
    question,
    top_k=3,
    excerpt_chars=520,
    season_scoped=False,
    entity_names=None,
    start_date=None,
    end_date=None,
):
    """분석 또는 UI용 article.content 근거를 유사도 순으로 반환한다.

    ``top_k=None``이면 엔티티·기간 범위의 고유 기사 전체를 반환한다. top_k는
    청크가 아니라 고유 기사에 적용되어 대표 5건 요청 시 항상 최대 5개 기사를 준다.
    """
    _ensure_index()

    if _chunk_matrix is None or len(_chunk_texts) == 0:
        return []
    if top_k is not None and int(top_k) <= 0:
        return []

    q = np.asarray(_get_model().encode(question), dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)

    scores = _chunk_matrix @ q
    explicit_period_mask = _period_mask(start_date, end_date)
    if explicit_period_mask is not None:
        scores = np.where(explicit_period_mask, scores, -np.inf)
    else:
        scores = _masked_scores(scores, season_scoped)

    entity_article_ids = _article_ids_for_entities(entity_names)
    if entity_article_ids:
        # MySQL은 article_id를 문자열로, 청크 캐시는 정수로 들고 있어 그대로 비교하면
        # ('1' vs 1) 전부 불일치해 모든 기사가 마스킹됐다. 양쪽을 문자열로 맞춘다.
        entity_id_strs = {str(value) for value in entity_article_ids}
        entity_mask = np.array([
            str(row.get("article_id")) in entity_id_strs for row in (_chunk_rows or [])
        ], dtype=bool)
        scores = np.where(entity_mask, scores, -np.inf)

    ranked_idx = np.argsort(-scores)

    evidence = []
    seen_articles = set()
    for i in ranked_idx:
        if not np.isfinite(scores[i]):
            continue
        row = _chunk_rows[i] if _chunk_rows else {"chunk_text": _chunk_texts[i]}
        article_id = row.get("article_id")
        article_key = article_id if article_id is not None else f"chunk:{row.get('chunk_id', i)}"
        if article_key in seen_articles:
            continue
        seen_articles.add(article_key)

        content = (row.get("content") or row.get("chunk_text") or "").strip()
        excerpt = content[:excerpt_chars].strip()
        if len(content) > excerpt_chars:
            excerpt += "..."

        evidence.append({
            "article_id": article_id,
            "chunk_id": row.get("chunk_id"),
            "title": row.get("title") or "제목 없음",
            "provider": row.get("provider") or "",
            "published_at": str(row.get("published_at") or ""),
            "url": row.get("url") or "",
            "excerpt": excerpt,
            "matched_chunk": (row.get("chunk_text") or "")[:260],
            "analysis_text": (row.get("chunk_text") or "")[:excerpt_chars],
            "score": float(scores[i]),
        })
        if top_k is not None and len(evidence) >= int(top_k):
            break

    return evidence


if __name__ == "__main__":
    docs = search_articles("젠지 우승 전략을 알려줘", top_k=3)
    print(f"\n=== 검색된 청크 {len(docs)}개 ===")
    for i, d in enumerate(docs, 1):
        print(f"[{i}] {d[:120]}...")
