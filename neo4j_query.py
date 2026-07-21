from connect import get_driver, get_database
from graph_schema_config import (
    QUERY_EXCLUDED_LABELS,
    QUERY_EXCLUDED_RELATIONS,
    RELATION_SYNONYMS,
)
import csv
import os
import re
import unicodedata
from collections import Counter, defaultdict, deque

print("### LOADED neo4j_query.py ###", os.path.abspath(__file__))


# =========================
# 별칭 정규화 (alias_dictionary.csv 기반)
# =========================
# 그래프 노드가 기사 표기 그대로 저장돼 있어(Faker / 페이커 / 이상혁 각각 별개 노드)
# 조회·렌더링 시점에 대표명(canonical)으로 병합한다.
# ※ 사전의 canonical 은 한글이다(예: 이상혁 → 페이커).

_ALIAS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alias_dictionary.csv")


def _load_alias_map(path=_ALIAS_CSV):
    """alias_dictionary.csv → {alias: canonical}. pandas 없이 stdlib csv 사용."""
    mapping = {}
    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)  # 헤더 skip
            for row in reader:
                if len(row) < 2:
                    continue
                alias = row[0].strip()
                canonical = row[1].strip()
                if alias and canonical:
                    mapping[alias] = canonical
                    mapping[alias.lower()] = canonical
    except Exception as e:
        print("DEBUG alias map 로드 실패(정규화 생략):", e)
    return mapping


_ALIAS_MAP = _load_alias_map()


def _canonical(name):
    """별칭 → 대표명. 사전에 없으면 원본 유지."""
    if not name:
        return name
    n = str(name).strip()
    return _ALIAS_MAP.get(n) or _ALIAS_MAP.get(n.lower()) or n


def _norm_relation(rel_name):
    """관계명 → (표준관계, invert). 매칭 없으면 원본 유지."""
    if not rel_name:
        return rel_name, False
    low = str(rel_name).lower()
    for keyword, canonical, invert in RELATION_SYNONYMS:
        if keyword.lower() in low:
            return canonical, invert
    return rel_name, False


# 대표명 → 그 대표명을 공유하는 모든 별칭 집합 (검색어 확장용)
_CANON_TO_ALIASES = {}
for _alias, _canon in _ALIAS_MAP.items():
    _CANON_TO_ALIASES.setdefault(_canon, set()).add(_alias)


def _expand_with_aliases(keywords):
    """
    검색 키워드를 별칭까지 확장한다.
    예: "페이커" → ["페이커", "faker", "이상혁", ...]
    DB에 어떤 표기로 저장돼 있든(Faker/페이커) 매칭되게 해서 그래프 누락을 막는다.
    """
    expanded = list(keywords)
    for kw in keywords:
        canon = _canonical(kw)
        variants = set(_CANON_TO_ALIASES.get(canon, set())) | {canon}
        for v in variants:
            v = str(v).strip().lower()
            if v and v not in expanded:
                expanded.append(v)
    return list(dict.fromkeys(expanded))


# =========================
# 표시용 유틸
# =========================

def _node_name(node):
    return (
        node.get("display_name")
        or node.get("name")
        or node.get("canonical_name")
        or node.get("entity_text")
        or node.get("team_name")
        or node.get("player_name")
        or node.get("champion_name")
        or node.get("league_name")
        or node.get("tournament_name")
        or node.get("title")
        or node.get("alias_name")
        or node.get("date")
        or node.get("action")   # TacticalMove: 행동 문구를 표시명으로
        or "unknown"
    )


_NOUN_DISPLAY_LABELS = {"Strategy", "TacticalMove", "Objective", "Entity", "Node"}


def _nounize_node_name(name, label=None):
    """서술형 전략/행동 이름을 화면과 답변에서 쓸 짧은 명사구로 정리한다.

    원본 이름은 조회용 ``id``로 그대로 보존하고 표시명에만 적용한다. 따라서 기존
    Neo4j 노드를 다시 적재하지 않아도 긴 ``...했다`` 문장이 그래프에 노출되지 않는다.
    """
    value = re.sub(r"\s+", " ", str(name or "")).strip().strip("\"'“”‘’")
    value = value.rstrip(".!?。！？ ")
    if not value or label not in _NOUN_DISPLAY_LABELS:
        return value or str(name or "")

    # 문장형 종결어미를 제거해 "바론을 확보했다" → "바론을 확보"로 만든다.
    value = re.sub(
        r"\s*(?:하였습니다|했습니다|하였다|했다|합니다|한다|되었습니다|됐습니다|되었다|됐다|됩니다|된다|이었습니다|이었다|였습니다|였다|입니다|이다)$",
        "",
        value,
    ).strip()
    # 목적격/주격/주제 보조사를 걷어 명사구로 읽히게 한다.
    value = re.sub(r"([0-9A-Za-z가-힣]+)(?:을|를|이|가|은|는)\s+(?=[0-9A-Za-z가-힣])", r"\1 ", value)
    value = re.sub(r"([0-9A-Za-z가-힣]+)하는\s+(전략|운영|플레이|전술)", r"\1 \2", value)
    return re.sub(r"\s+", " ", value).strip() or str(name or "").strip()


def _node_label(node):
    labels = list(node.labels)

    # Coach·TacticalMove 등 도메인 라벨을 Entity보다 먼저 인식한다.
    # (안 그러면 [Entity, Coach] 노드가 "Entity"로 잡혀 코치 색을 못 받음)
    priority = [
        "Team",
        "Player",
        "Champion",
        "Coach",
        "League",
        "Tournament",
        "Match",
        "Game",
        "TacticalMove",
        "Position",
        "Objective",
        "Patch",
        "Strategy",
        "Entity",
    ]

    resolved = None
    for label in priority:
        if label in labels:
            resolved = label
            break
    if resolved is None:
        resolved = node.get("entity_type") or node.get("type") or (labels[0] if labels else "Node")

    # 1군/2군 구분: Player 중 RDB(실제 LCK 출전 기록) 화이트리스트에 없으면
    # 2군/기타로 라벨을 분리해 색을 다르게 준다. (프론트에서 PlayerSub 색 지정)
    if resolved == "Player":
        name = str(_node_name(node)).strip().lower()
        if name and name not in _rdb_whitelist():
            return "PlayerSub"
    return resolved


def _relation_name(rel):
    return rel.type or rel.get("relation_type") or rel.get("type") or "RELATED"


# =========================
# 질문 키워드 추출
# =========================

STOPWORDS = {
    "알려줘", "보여줘", "설명해줘", "분석해줘", "찾아줘", "정리해줘",
    "말해줘", "해줘", "줘",
    "관련", "관련된", "내용", "정보", "데이터", "그래프", "관계",
    "결과", "질문", "답변",
    "평균", "kda", "승률", "전적", "통계", "기록", "포지션",
    "로스터", "라인업", "멤버", "명단", "챔피언", "챔프",
    "어떻게", "왜", "뭐", "무엇", "누가", "언제", "어디서", "어떤",
    "은", "는", "이", "가", "을", "를", "의", "에", "에서", "으로", "로",
    "와", "과", "도", "만", "에게", "한테", "그리고", "또는",
}


def _normalize_text(text):
    text = str(text).lower().strip()
    text = re.sub(r"[?!,:;()\[\]{}\"']", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_keywords(keywords):
    if isinstance(keywords, str):
        raw_texts = [keywords]
    elif isinstance(keywords, list):
        raw_texts = keywords
    else:
        raw_texts = []

    result = []

    for text in raw_texts:
        text = _normalize_text(text)

        if not text:
            continue

        # 짧은 원문은 후보로 유지
        if 2 <= len(text) <= 20 and text not in STOPWORDS:
            result.append(text)
            if "." in text:
                result.append(text.replace(".", ""))

        for token in text.split():
            token = token.strip().strip(".")

            if not token:
                continue

            if len(token) < 2:
                continue

            if token in STOPWORDS:
                continue

            # 조사는 단어 내부에서 전역 치환하지 않고 접미사일 때만 제거한다.
            # 예전 전역 치환은 "로스터"의 "로"를 지워 "스터"를 만들고,
            # 이것이 "KT 롤스터"까지 검색하는 오탐을 일으켰다.
            stripped = token
            for particle in (
                "에서", "으로", "에게", "한테",
                "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만",
            ):
                if stripped.endswith(particle) and len(stripped) - len(particle) >= 2:
                    stripped = stripped[:-len(particle)]
                    break

            if stripped in STOPWORDS or len(stripped) < 2:
                continue
            result.append(stripped)
            if "." in stripped:
                result.append(stripped.replace(".", ""))

    return list(dict.fromkeys(result))


# =========================
# 그래프 조회 Cypher
# =========================

EXCLUDED_RELATIONS = QUERY_EXCLUDED_RELATIONS
EXCLUDED_LABELS = QUERY_EXCLUDED_LABELS

# =========================
# 질문 포커스 기반 그래프 정리
# =========================
# 문제: "T1 주요 선수" 처럼 특정 팀을 물으면, 그 팀 노드에 붙은 수백 개의
# 경기(Match/Game) 및 전술(USED_STRATEGY) 관계가 LIMIT을 다 차지해서
# 정작 보고 싶은 선수(MEMBER_OF)·감독(COACHES)이 그래프에서 묻힌다.
#
# 해결: (1) 경기 스케줄 노드(Match/Game/Day)는 "경기/일정"을 물을 때만 포함하고
#       평소엔 제외해 그래프를 비운다.
#       (2) 질문 의도에 맞는 관계를 LIMIT보다 먼저 채우도록 우선순위를 준다.

SCHEDULE_LABELS = ["Match", "Game", "Day"]

ROSTER_RELATIONS = ["MEMBER_OF", "COACHES", "PLAYS_ROLE", "DIRECTED", "MADE_CALL"]
# 전략 질문 우선순위. 앞쪽은 현재 정규화 DB의 경기·통계 관계이고,
# 뒤쪽은 예전 기사 추출형 DB를 위한 호환 관계다.
STRATEGY_RELATIONS = [
    "TEAM_GAME_STAT", "WON_GAME", "PLAYED_GAME", "PLAYED_IN",
    "PART_OF_MATCH", "PART_OF_TOURNAMENT", "PLAYED_MATCH",
    "BY_TEAM", "EXECUTED_BY", "LED_TO", "USED_STRATEGY", "DIRECTED", "MADE_CALL",
]
SCHEDULE_RELATIONS = [
    "PLAYED_MATCH", "PLAYED_GAME", "WON_GAME", "WON_MATCH",
    "PLAYED_IN", "PICKED_IN", "PART_OF_MATCH", "PLAYED_ON",
]

# 원본 질문에 이 단어들이 있으면 해당 포커스로 판단 (부분 문자열 매칭).
ROSTER_TERMS = [
    "선수", "로스터", "멤버", "명단", "주전", "주요", "감독", "코치",
    "라인업", "lineup", "roster", "player", "coach",
]
STRATEGY_TERMS = [
    "전술", "운영", "전략", "조합", "스타일", "밴픽", "픽밴", "스와프",
    "주도권", "스노우볼", "한타", "교전", "시야", "오브젝트", "승리 요인",
    "어떻게 이겼", "strategy",
]
SCHEDULE_TERMS = [
    "경기", "매치", "게임", "일정", "스케줄", "날짜", "며칠", "언제",
    "결과", "스코어", "대진", "match", "game", "schedule", "score",
]


# =========================
# 전략 개요 노이즈 게이트
# =========================
# 실측(2026-07): Strategy 노드 1351개 중 853개가 팀 연결 0, 192개가 팀 1개(대부분 1회성/노이즈).
# "젠지 전략" 질문 시 이 잡동사니가 USED_STRATEGY로 우르르 붙어 그래프가 헤어볼이 된다.
# 개요(overview)에서는 N개 이상 팀과 연결된 '재사용 가능한' 전략 개념만 노출한다.
# ※ 노드 상세(get_node_detail)에는 이 게이트를 적용하지 않아 클릭 시 전부 다시 보인다.
MIN_STRATEGY_TEAMS_FOR_OVERVIEW = 2
# 확대(TacticalMove 수백 개) 후에는 개별 전술 사건이 개요를 뒤덮으므로 캡을 낮게 잡고,
# 인과 사슬(LED_TO)에 속한 대표 사건을 우선 노출한다. 나머지는 노드 클릭(상세)에서 본다.
STRATEGY_OVERVIEW_LIMIT = 15


def _is_strategy_focus(raw_keywords):
    """질문이 전략/운영/조합 등 '전략' 포커스인지 판단."""
    if isinstance(raw_keywords, (list, tuple)):
        text = " ".join(str(k) for k in raw_keywords)
    else:
        text = str(raw_keywords or "")
    text = text.lower()
    return any(t in text for t in STRATEGY_TERMS)


def _query_focus(raw_keywords):
    """
    원본 키워드(조사 제거 전)에서 질문 포커스를 판단해
    (제외할 라벨, 우선순위 관계)를 정한다.
    """
    if isinstance(raw_keywords, (list, tuple)):
        text = " ".join(str(k) for k in raw_keywords)
    else:
        text = str(raw_keywords or "")
    text = text.lower()

    has_schedule = any(t in text for t in SCHEDULE_TERMS)
    has_roster = any(t in text for t in ROSTER_TERMS)
    has_strategy = any(t in text for t in STRATEGY_TERMS)

    priority = []
    if has_roster:
        priority += ROSTER_RELATIONS
    if has_strategy:
        priority += STRATEGY_RELATIONS
    if has_schedule:
        priority += SCHEDULE_RELATIONS
    priority = list(dict.fromkeys(priority))

    excluded_labels = list(EXCLUDED_LABELS)
    if has_strategy:
        # 새 정규화 그래프의 전략 근거는 Team→Game→Match 경로에 있으므로
        # 경기라는 단어가 질문에 없어도 Game/Match를 브리지로 허용한다.
        # 전략 답변에는 챔피언 노드를 연결하지 않는다.
        excluded_labels.append("Champion")
    elif not has_schedule:
        # 경기/일정 질문이 아니면 스케줄 허브 노드를 그래프에서 뺀다.
        excluded_labels += SCHEDULE_LABELS

    return list(dict.fromkeys(excluded_labels)), priority


# =========================
# 시즌 날짜 필터 (시즌후 이적 등 노이즈 제거)
# =========================
# 2025 LCK 시즌: 2025-04-02 ~ 2025-09-28.
# 그래프의 추출관계(r)는 r.article_id(문자열)를 갖고, Article 노드는 article_id(정수)+
# published_at(문자열 "YYYY-MM-DD HH:MM:SS")을 갖는다. 두 id는 타입이 달라 toString()으로
# 맞춰 조인한다. published_at은 ISO형이라 문자열 사전순 비교 = 시간순 비교.
#
# season_scoped 질문(경기/전략/우승/챔피언 등 '시즌 중' 사실)일 때만 이 필터를 켠다.
# - r.article_id IS NULL: RDB에서 온 구조 관계(경기 스탯 등)는 기사 출처가 없으므로 통과.
# - 추출관계는 출처 기사 published_at이 시즌 범위 안일 때만 통과 → 시즌후 기사발 관계 제외.
SEASON_START = "2025-04-02 00:00:00"
SEASON_END = "2025-09-28 23:59:59"

_SEASON_FILTER_TOKEN = "// __SEASON_FILTER__"
_SEASON_FILTER_CLAUSE = """AND (
    r['article_id'] IS NULL
    OR EXISTS {
        MATCH (art)
        WHERE "Article" IN labels(art)
          AND toString(art['article_id']) = toString(r['article_id'])
          AND art['published_at'] >= $season_start
          AND art['published_at'] <= $season_end
    }
)"""


def _apply_season_filter(cypher, season_scoped):
    """Cypher의 __SEASON_FILTER__ 토큰을 실제 날짜 필터(또는 빈 문자열)로 치환한다."""
    return cypher.replace(
        _SEASON_FILTER_TOKEN,
        _SEASON_FILTER_CLAUSE if season_scoped else "",
    )


KEYWORD_CYPHER = """
MATCH (n)
WHERE none(label IN labels(n) WHERE label IN $excluded_labels)
AND any(kw IN $keywords WHERE
    toLower(coalesce(n.name, '')) CONTAINS kw
    OR toLower(coalesce(n.team_name, '')) CONTAINS kw
    OR toLower(coalesce(n.player_name, '')) CONTAINS kw
    OR toLower(coalesce(n.champion_name, '')) CONTAINS kw
    OR toLower(coalesce(n.league_name, '')) CONTAINS kw
    OR toLower(coalesce(n.tournament_name, '')) CONTAINS kw
)
MATCH (n)-[r]-(m)
WHERE NOT type(r) IN $excluded_relations
AND none(label IN labels(m) WHERE label IN $excluded_labels)
// __SEASON_FILTER__
// 질문 의도에 맞는 관계를 먼저 정렬하되 분석 범위의 관계는 모두 반환한다.
WITH n, r, m, startNode(r) AS source, endNode(r) AS target,
     CASE WHEN type(r) IN $priority_relations THEN 0 ELSE 1 END AS _pri
ORDER BY _pri
RETURN DISTINCT n, r, m, source, target
"""


# 현재 정규화 DB용 전략 경로. 가변 길이 ``[*1..3]``는 Team 한 개에서도
# 수십만 경로로 폭증하므로 사용하지 않는다. 대신 질문 범위에 속한 모든 Match를
# Team→Game→Match/Tournament 명시 경로로 읽고, 응답 단계에서 집계·일반화한다.
STRATEGY_MULTIHOP_CYPHER = """
MATCH (seed)
WHERE ("Team" IN labels(seed) OR "Player" IN labels(seed))
AND any(kw IN $keywords WHERE
    toLower(coalesce(seed.name, '')) CONTAINS kw
    OR toLower(coalesce(seed.team_name, '')) CONTAINS kw
    OR toLower(coalesce(seed.player_name, '')) CONTAINS kw
)
OPTIONAL MATCH (seed)-[seed_team:PLAYS_FOR]->(linked_team:Team)
WITH seed, seed_team,
     CASE WHEN "Team" IN labels(seed) THEN seed ELSE linked_team END AS team
WHERE team IS NOT NULL
CALL (team) {
    MATCH (team)-[:TEAM_GAME_STAT]->(:Game)-[:PART_OF_MATCH]->(candidate:Match)
    WHERE ($scope_start IS NULL OR
           substring(toString(candidate.match_date), 0, 10) >= $scope_start)
      AND ($scope_end IS NULL OR
           substring(toString(candidate.match_date), 0, 10) <= $scope_end)
    WITH DISTINCT candidate
    RETURN candidate AS match
}
MATCH (team)-[stat:TEAM_GAME_STAT]->(game:Game)-[part:PART_OF_MATCH]->(match)
OPTIONAL MATCH (winner:Team)-[won:WON_GAME]->(game)
OPTIONAL MATCH (match_winner:Team)-[won_match:WON_MATCH]->(match)
WITH DISTINCT seed, seed_team, team, stat, game, part, match,
              winner, won, match_winner, won_match
OPTIONAL MATCH (opponent:Team)-[opponent_played:PLAYED_GAME]->(game)
WHERE opponent <> team
OPTIONAL MATCH (player:Player)-[participation:PLAYED_IN]->(game)
WHERE participation.team_id = team.team_id
OPTIONAL MATCH (match)-[in_tournament:PART_OF_TOURNAMENT]->(tournament:Tournament)
OPTIONAL MATCH (tournament)-[in_league:BELONGS_TO_LEAGUE]->(league:League)
UNWIND [
    {r: seed_team, source: seed, target: team,
     depth: CASE WHEN seed_team IS NULL THEN 0 ELSE 1 END},
    {r: stat, source: team, target: game,
     depth: CASE WHEN seed_team IS NULL THEN 1 ELSE 2 END},
    {r: part, source: game, target: match,
     depth: CASE WHEN seed_team IS NULL THEN 2 ELSE 3 END},
    {r: won, source: winner, target: game,
     depth: CASE WHEN seed_team IS NULL THEN 2 ELSE 3 END},
    {r: won_match, source: match_winner, target: match,
     depth: CASE WHEN seed_team IS NULL THEN 2 ELSE 3 END},
    {r: opponent_played, source: opponent, target: game,
     depth: CASE WHEN seed_team IS NULL THEN 2 ELSE 3 END},
    {r: participation, source: player, target: game,
     depth: CASE WHEN seed_team IS NULL THEN 2 ELSE 3 END},
    {r: in_tournament, source: match, target: tournament,
     depth: CASE WHEN seed_team IS NULL THEN 3 ELSE 4 END},
    {r: in_league, source: tournament, target: league,
     depth: CASE WHEN seed_team IS NULL THEN 4 ELSE 5 END}
] AS edge
WITH team AS n, edge
WHERE edge.r IS NOT NULL
  AND NOT "Champion" IN labels(edge.source)
  AND NOT "Champion" IN labels(edge.target)
RETURN DISTINCT n, edge.r AS r, edge.target AS m,
       edge.source AS source, edge.target AS target, edge.depth AS depth
"""


# 일반 관계형 질문도 질문 엔티티와 연결된 전체 1·2-hop 관계를 분석 근거로 읽는다.
# 화면은 별도로 가지치기하지만 상세 해설용 analysis_facts는 여기서 자르지 않는다.
RELATIONAL_SECOND_HOP_CYPHER = """
UNWIND $mid_ids AS mid_id
MATCH (mid) WHERE elementId(mid) = mid_id
MATCH (mid)-[r]-(m)
WHERE NOT type(r) IN $excluded_relations
  AND none(label IN labels(m) WHERE label IN $excluded_labels)
  AND NOT elementId(m) IN $center_ids
  AND coalesce(r.traversal_allowed, true) <> false
// __SEASON_FILTER__
RETURN DISTINCT mid AS n, r, m, startNode(r) AS source,
       endNode(r) AS target, 2 AS depth
"""


# 전략 개요 전용: KEYWORD_CYPHER 와 같되, 이웃 m 이 Strategy 노드일 때는
# '팀 $min_strategy_teams개 이상과 연결된 재사용 전략'만 통과시켜 1회성 노이즈를 걷어낸다.
STRATEGY_OVERVIEW_CYPHER = """
MATCH (n)
WHERE none(label IN labels(n) WHERE label IN $excluded_labels)
AND any(kw IN $keywords WHERE
    toLower(coalesce(n.name, '')) CONTAINS kw
    OR toLower(coalesce(n.team_name, '')) CONTAINS kw
    OR toLower(coalesce(n.player_name, '')) CONTAINS kw
    OR toLower(coalesce(n.champion_name, '')) CONTAINS kw
    OR toLower(coalesce(n.league_name, '')) CONTAINS kw
    OR toLower(coalesce(n.tournament_name, '')) CONTAINS kw
)
MATCH (n)-[r]-(m)
WHERE NOT type(r) IN $excluded_relations
AND none(label IN labels(m) WHERE label IN $excluded_labels)
// __SEASON_FILTER__
AND (
    // 노이즈 게이트: USED_STRATEGY로 붙은 전술 타깃은 라벨(Strategy/Entity/Node/Patch)과 무관하게
    // '서로 다른 팀 $min_strategy_teams개 이상'이 쓴 재사용 개념만 통과시킨다.
    type(r) <> "USED_STRATEGY"
    OR COUNT { MATCH (m)--(t:Team) RETURN DISTINCT t } >= $min_strategy_teams
)
WITH n, r, m, startNode(r) AS source, endNode(r) AS target,
     CASE WHEN type(r) IN $priority_relations THEN 0 ELSE 1 END AS _pri,
     // 인과 사슬에 속한 전술 사건(LED_TO 보유)을 먼저 노출
     CASE WHEN "TacticalMove" IN labels(m) AND COUNT { (m)-[:LED_TO]-() } > 0 THEN 0 ELSE 1 END AS _chain
ORDER BY _pri, _chain
RETURN n, r, m, source, target
LIMIT $limit
"""


CHAMPION_USAGE_CYPHER = """
MATCH (p)-[r]-(c)
WHERE none(label IN labels(p) WHERE label IN $excluded_labels)
AND none(label IN labels(c) WHERE label IN $excluded_labels)
AND "Champion" IN labels(c)
AND any(kw IN $player_keywords WHERE
    toLower(coalesce(p.name, '')) CONTAINS kw
    OR toLower(coalesce(p.player_name, '')) CONTAINS kw
)
AND NOT type(r) IN $excluded_relations
// __SEASON_FILTER__
RETURN p AS n, r, c AS m, startNode(r) AS source, endNode(r) AS target
LIMIT $limit
"""


CHAMPION_USAGE_TERMS = {
    "사용", "사용한", "쓴", "플레이", "픽", "고른", "선택",
    "챔피언", "챔프", "champion", "pick", "picked", "played", "used",
}


def _strip_korean_particle(token):
    return re.sub(r"(에서|에게|한테|으로|로|은|는|이|가|을|를|과|와|도|만|의)$", "", token)


def _has_explicit_strategy_keyword(raw_keywords):
    """검색 키워드에 전략/strategy가 명시된 경우에만 Champion을 숨긴다."""
    if isinstance(raw_keywords, (list, tuple, set)):
        values = raw_keywords
    else:
        values = [raw_keywords]
    return any(
        "전략" in str(value or "").lower() or "strategy" in str(value or "").lower()
        for value in values
    )


def _looks_like_champion_usage_query(keywords):
    text = " ".join(str(kw).lower() for kw in keywords)
    return (
        any(term in text for term in ["챔피언", "챔프", "champion"])
        and any(term in text for term in ["사용", "쓴", "플레이", "픽", "고른", "선택", "played", "used", "pick"])
    )


def _player_keywords_for_champion_usage(keywords):
    candidates = []
    for kw in keywords:
        normalized = _normalize_text(kw)
        for token in [normalized, *normalized.split()]:
            token = _strip_korean_particle(token.strip().strip("."))
            if len(token) < 2:
                continue
            if token in STOPWORDS or token in CHAMPION_USAGE_TERMS:
                continue
            if any(term in token for term in CHAMPION_USAGE_TERMS):
                # "페이커가 사용한 챔피언" 같은 문장은 토큰으로 다시 분해해서 선수명만 남긴다.
                continue
            candidates.append(token)

    return list(dict.fromkeys(candidates))


# =========================
# 결과 변환
# =========================

def _add_node(nodes, node):
    # 별칭을 대표명으로 병합: 대표명 자체를 노드 식별자로 사용한다.
    # (Faker / 페이커 / 이상혁 → 모두 "페이커" 노드 하나로 합쳐짐)
    node_id = _canonical(_node_name(node))

    if node_id not in nodes:
        label = _node_label(node)
        nodes[node_id] = {
            "id": node_id,
            "name": _nounize_node_name(node_id, label),
            "raw_name": node_id,
            "label": label,
        }

    # 전략 개요에서 Game을 Match로 접을 때 필요한 최소 메타데이터만 보존한다.
    # 일반 그래프의 표시명과 상세 조회용 id는 기존 값을 그대로 사용한다.
    label = nodes[node_id]["label"]
    if label == "Match":
        for key in ("match_id", "match_date"):
            if node.get(key) is not None:
                nodes[node_id][key] = node.get(key)
    elif label == "Game":
        for key in ("game_id", "game_no"):
            if node.get(key) is not None:
                nodes[node_id][key] = node.get(key)

    return node_id


RELATION_CONTEXT_FIELDS = {
    "TEAM_GAME_STAT": (
        "total_kills", "total_deaths", "dragons", "barons", "towers",
        "total_gold", "void_grubs", "atakhans", "heralds", "inhibitors",
        "first_dragon", "first_baron", "first_tower",
        "pick1", "pick2", "pick3", "pick4", "pick5",
        "ban1", "ban2", "ban3", "ban4", "ban5",
    ),
    "PLAYED_IN": (
        "position", "kills", "deaths", "assists", "gold",
        "damage_to_champions", "vision_score",
    ),
    "PLAYS_FOR": ("role", "season", "first_seen", "last_seen"),
    "PLAYED_GAME": ("side",),
    "PLAYED_MATCH": ("side",),
    "PICKED_CHAMPION": ("games", "wins"),
    "HAS_STRATEGY_EVIDENCE": ("detail",),
    "HAS_INFERRED_STRATEGY": ("detail", "confidence", "basis"),
    "USES_INFERRED_STRATEGY": ("detail", "confidence", "basis"),
    "SUPPORTED_BY_MATCH": ("detail",),
}

RELATION_CONTEXT_LABELS = {
    "total_kills": "킬", "total_deaths": "데스", "dragons": "드래곤",
    "barons": "바론", "towers": "타워", "total_gold": "총 골드",
    "void_grubs": "공허 유충", "atakhans": "아타칸", "heralds": "전령",
    "inhibitors": "억제기", "first_dragon": "첫 드래곤",
    "first_baron": "첫 바론", "first_tower": "첫 타워", "position": "포지션",
    "kills": "킬", "deaths": "데스", "assists": "어시스트", "gold": "골드",
    "damage_to_champions": "챔피언 피해량", "vision_score": "시야 점수",
    "role": "역할", "season": "시즌", "first_seen": "첫 확인",
    "last_seen": "마지막 확인", "side": "진영",
    "games": "사용 경기", "wins": "승리",
    "detail": "근거",
    "confidence": "신뢰도", "basis": "근거 구성",
    "pick1": "픽1", "pick2": "픽2", "pick3": "픽3", "pick4": "픽4",
    "pick5": "픽5", "ban1": "밴1", "ban2": "밴2", "ban3": "밴3",
    "ban4": "밴4", "ban5": "밴5",
}


def _match_date_parts(value):
    """Neo4j의 Match 날짜 문자열에서 표시용 연·월·일을 안전하게 꺼낸다."""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _collapse_strategy_games_to_matches(graph):
    """전략 개요의 Game을 Match로 접고 정형 통계를 전략 근거 노드로 만든다.

    원본 Neo4j 노드나 관계는 변경하지 않는다. 화면에 반환하는 전략 subgraph만
    Team/Player → Match → 정형 근거 형태로 재구성한다.
    """
    nodes = list(graph.get("nodes") or [])
    links = list(graph.get("links") or [])
    node_by_id = {str(node["id"]): node for node in nodes}
    game_ids = {
        str(node["id"]) for node in nodes if node.get("label") == "Game"
    }
    if not game_ids:
        return graph

    game_to_match = {}
    for link in links:
        source = str(link.get("source"))
        target = str(link.get("target"))
        if link.get("type") != "PART_OF_MATCH":
            continue
        if source in game_ids:
            game_to_match[source] = target
        elif target in game_ids:
            game_to_match[target] = source

    if not game_to_match:
        return graph

    match_teams = {}
    stat_rows = {}
    for link in links:
        source = str(link.get("source"))
        target = str(link.get("target"))
        game_id = source if source in game_to_match else target if target in game_to_match else None
        if not game_id:
            continue
        match_id = game_to_match[game_id]
        other_id = target if source == game_id else source
        other = node_by_id.get(other_id)
        if other and other.get("label") == "Team":
            match_teams.setdefault(match_id, set()).add(other_id)
        if link.get("type") == "TEAM_GAME_STAT" and source not in game_ids:
            game_node = node_by_id.get(game_id) or {}
            stat_rows.setdefault((match_id, source), []).append({
                "game_no": game_node.get("game_no"),
                "properties": dict(link.get("properties") or {}),
            })

    # Match 라벨은 날짜만, hover에는 연도까지 포함한 날짜와 대진을 제공한다.
    for match_id in set(game_to_match.values()):
        match_node = node_by_id.get(match_id)
        if not match_node:
            continue
        date_parts = _match_date_parts(match_node.get("match_date"))
        if date_parts:
            year, month, day = date_parts
            match_node["name"] = f"{month}월 {day}일 경기"
            full_date = f"{year}년 {month}월 {day}일"
        else:
            full_date = "날짜 정보 없음"
        team_names = sorted(
            node_by_id[team_id].get("name") or team_id
            for team_id in match_teams.get(match_id, set())
            if team_id in node_by_id
        )
        matchup = " vs ".join(team_names) if team_names else "대진 정보 없음"
        match_node["title"] = f"{full_date} · {matchup}"

    rebuilt_links = []
    seen_links = set()

    def add_link(source, target, relation, properties=None, depth=None):
        if not source or not target or source == target:
            return
        key = (str(source), str(target), relation)
        if key in seen_links:
            return
        seen_links.add(key)
        item = {"source": source, "target": target, "type": relation}
        if properties:
            item["properties"] = properties
        if depth is not None:
            item["depth"] = depth
        rebuilt_links.append(item)

    for link in links:
        relation = link.get("type") or "RELATED_TO"
        if relation == "PART_OF_MATCH":
            continue
        if relation == "WON_GAME" and (
            str(link.get("source")) in game_to_match
            or str(link.get("target")) in game_to_match
        ):
            # 세트 승리를 Match 승리로 승격하지 않는다. 조회에 포함된 실제
            # WON_MATCH 관계만 사용해야 한 매치에 승자가 둘 생기지 않는다.
            continue
        if relation == "PLAYED_IN" and (
            str(link.get("source")) in game_to_match
            or str(link.get("target")) in game_to_match
        ):
            # 세트 단위 선수 기록은 Game을 없앤 뒤 Match에 옮기면 여러 경기와
            # 교차 연결된 것처럼 보인다. 전략 개요에서는 제외하고 상세 조회에 남긴다.
            continue
        source = game_to_match.get(str(link.get("source")), link.get("source"))
        target = game_to_match.get(str(link.get("target")), link.get("target"))
        properties = dict(link.get("properties") or {})
        if relation == "TEAM_GAME_STAT":
            relation, properties = "PLAYED_MATCH", {}
        elif relation == "PLAYED_GAME":
            relation, properties = "PLAYED_MATCH", {}
        add_link(source, target, relation, properties, link.get("depth"))

    evidence_nodes = []
    for (match_id, team_id), rows in stat_rows.items():
        rows.sort(key=lambda row: (row.get("game_no") is None, row.get("game_no") or 0))
        played_sets = len(rows)
        totals = {
            key: sum(int(row["properties"].get(key) or 0) for row in rows)
            for key in ("dragons", "barons", "towers")
        }
        firsts = {
            key: sum(bool(row["properties"].get(key)) for row in rows)
            for key in ("first_dragon", "first_baron", "first_tower")
        }
        prefix = f"strategy-evidence:{match_id}:{team_id}"
        match_node = node_by_id.get(match_id) or {}
        team_node = node_by_id.get(team_id) or {}

        first_detail = (
            f"조회된 {played_sets}개 세트 중 첫 드래곤 {firsts['first_dragon']}회, "
            f"첫 바론 {firsts['first_baron']}회, 첫 타워 {firsts['first_tower']}회"
        )
        objective_detail = (
            f"조회된 {played_sets}개 세트 합계: 드래곤 {totals['dragons']}, "
            f"바론 {totals['barons']}, 타워 {totals['towers']}"
        )
        pick_lines = []
        for index, row in enumerate(rows, start=1):
            picks = [
                str(row["properties"].get(f"pick{i}") or "").strip()
                for i in range(1, 6)
            ]
            picks = [pick for pick in picks if pick]
            if picks:
                set_no = row.get("game_no") or index
                pick_lines.append(f"{set_no}세트 {'·'.join(picks)}")

        evidence_specs = [
            (
                "first",
                f"선취 용{firsts['first_dragon']}·바론{firsts['first_baron']}·타워{firsts['first_tower']}",
                first_detail,
            ),
            (
                "objectives",
                f"확보 용{totals['dragons']}·바론{totals['barons']}·타워{totals['towers']}",
                objective_detail,
            ),
        ]
        if pick_lines:
            evidence_specs.append((
                "picks",
                f"픽 조합 {played_sets}세트",
                "; ".join(pick_lines),
            ))

        for kind, name, detail in evidence_specs:
            evidence_id = f"{prefix}:{kind}"
            evidence_nodes.append({
                "id": evidence_id,
                "name": name,
                "raw_name": name,
                "label": "StrategyEvidence",
                "title": detail,
                "evidence_source": "rdb",
                "match_node_id": match_id,
                "match_id": match_node.get("match_id") or match_id,
                "team_node_id": team_id,
                "team": team_node.get("name") or team_id,
            })
            add_link(
                match_id,
                evidence_id,
                "HAS_STRATEGY_EVIDENCE",
                {"detail": detail},
                3,
            )

    connected_ids = {
        str(endpoint)
        for link in rebuilt_links
        for endpoint in (link.get("source"), link.get("target"))
    }
    visible_nodes = [
        node for node in nodes
        if str(node["id"]) not in game_ids
        and (node.get("label") != "Player" or str(node["id"]) in connected_ids)
    ]
    visible_nodes.extend(evidence_nodes)
    visible_ids = {str(node["id"]) for node in visible_nodes}
    return {
        "nodes": visible_nodes,
        "links": [
            link for link in rebuilt_links
            if str(link["source"]) in visible_ids and str(link["target"]) in visible_ids
        ],
    }


def _normalize_strategy_name(name):
    """같은 Match 내부의 전략명 중복 제거에 쓸 안정적인 비교 키를 만든다."""
    value = unicodedata.normalize("NFKC", str(name or "")).casefold().strip()
    value = re.sub(r"[^0-9a-z가-힣]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _select_strategy_team_id(kg_result, node_by_id, links, preferred_ids=None):
    """질문 키워드와 근거 메타데이터로 전략 그래프의 대상 Team을 고른다."""
    preferred = [
        str(value) for value in (preferred_ids or [])
        if node_by_id.get(str(value), {}).get("label") == "Team"
    ]
    if preferred:
        return preferred[0]

    keyword_names = {
        str(_canonical(_strip_korean_particle(value))).casefold().strip()
        for value in (kg_result.get("keywords") or [])
        if str(value or "").strip()
    }
    for node_id, node in node_by_id.items():
        if node.get("label") != "Team":
            continue
        names = {
            str(_canonical(value)).casefold().strip()
            for value in (node_id, node.get("name"), node.get("raw_name"))
            if str(value or "").strip()
        }
        if names.intersection(keyword_names):
            return node_id

    match_counts = {}
    for link in links:
        source = str(link.get("source"))
        target = str(link.get("target"))
        if node_by_id.get(source, {}).get("label") != "Team":
            continue
        if node_by_id.get(target, {}).get("label") != "Match":
            continue
        if link.get("type") not in {"PLAYED_MATCH", "WON_MATCH"}:
            continue
        match_counts[source] = match_counts.get(source, 0) + 1
    return max(match_counts, key=match_counts.get) if match_counts else None


def apply_strategy_insights(kg_result, insights):
    """응답 그래프를 Team → Match → InferredStrategy → Evidence로 재구성한다.

    InferredStrategy는 Neo4j에 MERGE하지 않는 응답 전용 노드다. 식별자는
    ``match_id:normalized_strategy_name``이며, 한 노드가 여러 Match에 연결되는
    입력은 버린다.
    """
    graph = kg_result.get("graph") or {"nodes": [], "links": []}
    nodes = list(graph.get("nodes") or [])
    links = list(graph.get("links") or [])
    node_by_id = {str(node.get("id")): node for node in nodes}

    def team_only_result(preferred_ids=None):
        team_id = _select_strategy_team_id(
            kg_result, node_by_id, links, preferred_ids=preferred_ids or []
        )
        updated_graph = {
            "nodes": [node_by_id[team_id]] if team_id in node_by_id else [],
            "links": [],
        }
        facts = _graph_to_facts(updated_graph)
        return {
            **kg_result,
            "graph": updated_graph,
            "analysis_graph": updated_graph,
            "facts": facts,
            "analysis_facts": facts,
            "generalized_facts": facts,
            "context": format_graph_facts(facts, max_facts=None),
        }

    # 신뢰도 낮음은 방어적으로 한 번 더 제거한다. 이 전략만 연결된 Match와
    # StrategyEvidence도 아래 재구성 과정에서 자연스럽게 표시 대상에서 빠진다.
    insights = [
        item for item in (insights or [])
        if str(item.get("confidence") or "보통").strip() != "낮음"
    ]
    if not insights:
        return team_only_result()

    evidence_ids = {
        str(node.get("id")) for node in nodes
        if node.get("label") == "StrategyEvidence"
    }
    if not evidence_ids:
        return team_only_result()

    evidence_by_match = {}
    for link in links:
        if link.get("type") != "HAS_STRATEGY_EVIDENCE":
            continue
        source = str(link.get("source"))
        target = str(link.get("target"))
        if node_by_id.get(source, {}).get("label") == "Match" and target in evidence_ids:
            evidence_by_match.setdefault(source, []).append(target)
        elif node_by_id.get(target, {}).get("label") == "Match" and source in evidence_ids:
            evidence_by_match.setdefault(target, []).append(source)

    prepared = {}
    preferred_team_ids = []
    confidence_rank = {"낮음": 0, "보통": 1, "높음": 2}
    for insight in insights:
        confidence = str(insight.get("confidence") or "보통").strip()
        if confidence == "낮음":
            continue
        name = str(insight.get("name") or "").strip()
        explanation = str(insight.get("explanation") or "").strip()
        if not name or not explanation:
            continue
        match_node_ids = list(dict.fromkeys(
            str(value) for value in (
                insight.get("match_node_ids")
                or ([insight.get("match_node_id")] if insight.get("match_node_id") else [])
            )
            if node_by_id.get(str(value), {}).get("label") == "Match"
        ))
        if len(match_node_ids) != 1:
            continue
        match_node_id = match_node_ids[0]
        match_node = node_by_id[match_node_id]
        match_identity = str(
            match_node.get("match_id")
            or insight.get("match_id")
            or match_node_id
        ).strip()
        normalized_name = _normalize_strategy_name(name)
        if not normalized_name:
            continue
        strategy_key = f"{match_identity}:{normalized_name}"
        key = (match_node_id, normalized_name)
        team_node_id = str(insight.get("team_node_id") or "").strip()
        if team_node_id:
            preferred_team_ids.append(team_node_id)

        if key not in prepared:
            prepared[key] = {
                "name": name,
                "explanation": explanation,
                "confidence": confidence,
                "match_node_id": match_node_id,
                "match_id": match_identity,
                "strategy_key": strategy_key,
                "normalized_name": normalized_name,
                "team_node_id": team_node_id or None,
                "article_ids": list(dict.fromkeys(insight.get("article_ids") or [])),
                "article_evidence": [
                    dict(value) for value in (insight.get("article_evidence") or [])
                    if isinstance(value, dict)
                ],
                "structured_ids": list(dict.fromkeys(insight.get("structured_ids") or [])),
                "evidence_node_ids": list(dict.fromkeys(
                    str(value) for value in (insight.get("evidence_node_ids") or [])
                    if str(value) in evidence_ids
                )),
            }
            continue

        current = prepared[key]
        for field in ("article_ids", "structured_ids", "evidence_node_ids"):
            current[field] = list(dict.fromkeys([
                *current[field],
                *(
                    str(value) for value in (insight.get(field) or [])
                    if field != "evidence_node_ids" or str(value) in evidence_ids
                ),
            ]))
        seen_article_evidence = {
            str(value.get("evidence_id") or value.get("article_id") or value.get("title"))
            for value in current.get("article_evidence", [])
        }
        for value in insight.get("article_evidence") or []:
            if not isinstance(value, dict):
                continue
            evidence_key = str(
                value.get("evidence_id") or value.get("article_id") or value.get("title")
            )
            if evidence_key in seen_article_evidence:
                continue
            current.setdefault("article_evidence", []).append(dict(value))
            seen_article_evidence.add(evidence_key)
        incoming_confidence = str(insight.get("confidence") or "보통")
        if confidence_rank.get(incoming_confidence, 1) > confidence_rank.get(current["confidence"], 1):
            current["confidence"] = incoming_confidence

    if not prepared:
        return team_only_result(preferred_team_ids)

    team_id = _select_strategy_team_id(
        kg_result, node_by_id, links, preferred_ids=preferred_team_ids
    )
    if not team_id:
        return team_only_result(preferred_team_ids)

    selected_match_ids = list(dict.fromkeys(
        item["match_node_id"] for item in prepared.values()
        if not item.get("team_node_id") or item.get("team_node_id") == team_id
    ))
    if not selected_match_ids:
        return team_only_result(preferred_team_ids)

    focus_team_node = dict(node_by_id[team_id])
    focus_team_node["graph_role"] = "focus_team"
    visible_nodes = [focus_team_node] + [
        node_by_id[match_id] for match_id in selected_match_ids
    ]
    visible_links = [
        {
            "source": team_id,
            "target": match_id,
            "type": "PLAYED_MATCH",
            "depth": 1,
        }
        for match_id in selected_match_ids
    ]
    visible_node_ids = {team_id, *selected_match_ids}
    seen_links = {
        (str(link["source"]), str(link["target"]), link["type"])
        for link in visible_links
    }

    # 각 Match의 상대 Team도 대진 맥락으로 남긴다. 분석 대상 Team과 혼동되지
    # 않도록 역할을 명시하고, 최종 렌더링 단계에서 작은 보조 노드로 표시한다.
    opponents_by_match = defaultdict(list)
    for link in links:
        if link.get("type") not in {"PLAYED_MATCH", "WON_MATCH"}:
            continue
        source = str(link.get("source"))
        target = str(link.get("target"))
        if source in selected_match_ids and node_by_id.get(target, {}).get("label") == "Team":
            match_id, candidate_team_id = source, target
        elif target in selected_match_ids and node_by_id.get(source, {}).get("label") == "Team":
            match_id, candidate_team_id = target, source
        else:
            continue
        if candidate_team_id != team_id and candidate_team_id not in opponents_by_match[match_id]:
            opponents_by_match[match_id].append(candidate_team_id)

    for match_id in selected_match_ids:
        for opponent_id in opponents_by_match.get(match_id, [])[:1]:
            if opponent_id not in visible_node_ids:
                opponent_node = dict(node_by_id[opponent_id])
                opponent_node["graph_role"] = "opponent_team"
                visible_nodes.append(opponent_node)
                visible_node_ids.add(opponent_id)
            opponent_link = (opponent_id, match_id, "PLAYED_MATCH")
            if opponent_link in seen_links:
                continue
            visible_links.append({
                "source": opponent_id,
                "target": match_id,
                "type": "PLAYED_MATCH",
                "properties": {"role": "opponent"},
                "depth": 1,
            })
            seen_links.add(opponent_link)

    # 실제 KG의 Match → Tournament → League 소속 경로를 전략 그래프에도 보존한다.
    selected_tournament_ids = set()
    for link in links:
        if link.get("type") != "PART_OF_TOURNAMENT":
            continue
        source = str(link.get("source"))
        target = str(link.get("target"))
        if source in selected_match_ids and node_by_id.get(target, {}).get("label") == "Tournament":
            match_id, tournament_id = source, target
        elif target in selected_match_ids and node_by_id.get(source, {}).get("label") == "Tournament":
            match_id, tournament_id = target, source
        else:
            continue
        if tournament_id not in visible_node_ids:
            tournament_node = dict(node_by_id[tournament_id])
            tournament_node["graph_role"] = "match_tournament"
            visible_nodes.append(tournament_node)
            visible_node_ids.add(tournament_id)
        selected_tournament_ids.add(tournament_id)
        tournament_link = (match_id, tournament_id, "PART_OF_TOURNAMENT")
        if tournament_link not in seen_links:
            visible_links.append({
                "source": match_id,
                "target": tournament_id,
                "type": "PART_OF_TOURNAMENT",
                "depth": 3,
            })
            seen_links.add(tournament_link)

    for link in links:
        if link.get("type") != "BELONGS_TO_LEAGUE":
            continue
        source = str(link.get("source"))
        target = str(link.get("target"))
        if source in selected_tournament_ids and node_by_id.get(target, {}).get("label") == "League":
            tournament_id, league_id = source, target
        elif target in selected_tournament_ids and node_by_id.get(source, {}).get("label") == "League":
            tournament_id, league_id = target, source
        else:
            continue
        if league_id not in visible_node_ids:
            league_node = dict(node_by_id[league_id])
            league_node["graph_role"] = "match_league"
            visible_nodes.append(league_node)
            visible_node_ids.add(league_id)
        league_link = (tournament_id, league_id, "BELONGS_TO_LEAGUE")
        if league_link not in seen_links:
            visible_links.append({
                "source": tournament_id,
                "target": league_id,
                "type": "BELONGS_TO_LEAGUE",
                "depth": 4,
            })
            seen_links.add(league_link)

    for item in prepared.values():
        if item["match_node_id"] not in selected_match_ids:
            continue
        if item.get("team_node_id") and item.get("team_node_id") != team_id:
            continue
        confidence = item["confidence"]
        article_count = len(item["article_ids"])
        structured_count = len(item["structured_ids"])
        basis = f"기사 {article_count}건 · 정형 근거 {structured_count}건"
        insight_id = f"inferred-strategy:{item['strategy_key']}"
        available_evidence_ids = list(dict.fromkeys([
            *item["evidence_node_ids"],
            *evidence_by_match.get(item["match_node_id"], []),
        ]))
        available_rdb_evidence = [
            dict(node_by_id[evidence_id]) for evidence_id in available_evidence_ids
            if evidence_id in node_by_id
        ]
        visible_nodes.append({
            "id": insight_id,
            "name": item["name"],
            "raw_name": item["name"],
            "label": "InferredStrategy",
            "title": f"{item['explanation']}\n신뢰도: {confidence} · {basis}",
            "strategy_key": item["strategy_key"],
            "normalized_strategy_name": item["normalized_name"],
            "match_id": item["match_id"],
            "confidence": confidence,
            "article_ids": item["article_ids"],
            "article_evidence": item.get("article_evidence", []),
            "structured_ids": item["structured_ids"],
            "rdb_evidence": available_rdb_evidence,
        })
        visible_node_ids.add(insight_id)
        visible_links.append({
            "source": item["match_node_id"],
            "target": insight_id,
            "type": "HAS_INFERRED_STRATEGY",
            "properties": {
                "detail": item["explanation"],
                "confidence": confidence,
                "basis": basis,
            },
            "depth": 2,
        })

        cited_evidence_ids = item["evidence_node_ids"] or evidence_by_match.get(
            item["match_node_id"], []
        )
        for evidence_id in cited_evidence_ids:
            if evidence_id not in node_by_id:
                continue
            if evidence_id not in visible_node_ids:
                visible_nodes.append(node_by_id[evidence_id])
                visible_node_ids.add(evidence_id)
            evidence_link = (insight_id, evidence_id, "HAS_STRATEGY_EVIDENCE")
            if evidence_link in seen_links:
                continue
            evidence_detail = str(node_by_id[evidence_id].get("title") or "")
            visible_links.append({
                "source": insight_id,
                "target": evidence_id,
                "type": "HAS_STRATEGY_EVIDENCE",
                "properties": {"detail": evidence_detail},
                "depth": 3,
            })
            seen_links.add(evidence_link)

    updated_graph = {"nodes": visible_nodes, "links": visible_links}
    facts = _graph_to_facts(updated_graph)
    return {
        **kg_result,
        "graph": updated_graph,
        "analysis_graph": updated_graph,
        "facts": facts,
        "analysis_facts": facts,
        "generalized_facts": facts,
        "context": format_graph_facts(facts, max_facts=None),
    }


def apply_rdb_champion_usage(kg_result, player_name, usage_rows):
    """RDB 전체 경기 챔피언 집계를 응답 그래프에 보강한다."""
    if not player_name or not usage_rows:
        return kg_result
    graph = kg_result.get("graph") or {"nodes": [], "links": []}
    nodes = list(graph.get("nodes") or [])
    links = list(graph.get("links") or [])
    player_id = _canonical(player_name)
    node_by_id = {str(node.get("id")): node for node in nodes}
    if player_id not in node_by_id:
        nodes.append({
            "id": player_id, "name": player_id, "raw_name": player_id,
            "label": "Player",
        })

    link_by_key = {
        (str(link.get("source")), str(link.get("target")), link.get("type")): link
        for link in links
    }
    for row in usage_rows:
        champion = _canonical(row.get("champion_name"))
        if not champion:
            continue
        if champion not in node_by_id:
            node = {
                "id": champion, "name": champion, "raw_name": champion,
                "label": "Champion",
            }
            nodes.append(node)
            node_by_id[champion] = node
        properties = {
            "games": int(row.get("games") or 0),
            "wins": int(row.get("wins") or 0),
        }
        node_by_id[champion]["title"] = (
            f"{player_id} 전체 기록: {properties['games']}회 사용 · {properties['wins']}승"
        )
        key = (player_id, champion, "PICKED_CHAMPION")
        if key in link_by_key:
            link_by_key[key]["properties"] = properties
        else:
            link = {
                "source": player_id,
                "target": champion,
                "type": "PICKED_CHAMPION",
                "properties": properties,
            }
            links.append(link)
            link_by_key[key] = link

    updated_graph = {"nodes": nodes, "links": links}
    facts = _graph_to_facts(updated_graph)
    return {
        **kg_result,
        "graph": updated_graph,
        "analysis_graph": updated_graph,
        "facts": facts,
        "analysis_facts": facts,
        "generalized_facts": facts,
        "context": format_graph_facts(facts, max_facts=None),
    }


def _relation_context_properties(relation, relation_name=None):
    """LLM 근거에 필요한 관계 속성만 화이트리스트로 보존한다."""
    relation_name = relation_name or _relation_name(relation)
    fields = RELATION_CONTEXT_FIELDS.get(relation_name, ())
    if not fields:
        return {}
    try:
        raw = dict(relation)
    except (TypeError, ValueError):
        raw = {}
    return {key: raw[key] for key in fields if raw.get(key) is not None}


def _format_relation_detail(relation_name, properties):
    if not properties:
        return ""
    parts = []
    for key in RELATION_CONTEXT_FIELDS.get(relation_name, properties.keys()):
        if key not in properties:
            continue
        if key.startswith(("pick", "ban")):
            continue
        label = RELATION_CONTEXT_LABELS.get(key, key)
        value = properties[key]
        if key in {"first_dragon", "first_baron", "first_tower"}:
            value = "확보" if bool(value) else "미확보"
        parts.append(f"{label} {value}")
    if relation_name == "TEAM_GAME_STAT":
        picks = [str(properties.get(f"pick{i}") or "").strip() for i in range(1, 6)]
        bans = [str(properties.get(f"ban{i}") or "").strip() for i in range(1, 6)]
        picks = [name for name in picks if name]
        bans = [name for name in bans if name]
        if picks:
            parts.append("픽 조합 " + "·".join(picks))
        if bans:
            parts.append("밴 " + "·".join(bans))
    return ", ".join(parts)


def _add_link(
    links, seen_links, source_id, target_id, relation_name, directed=False,
    properties=None, depth=None,
):
    # 기본은 방향 무시한 노드쌍 + 관계로 중복 제거
    # → "A-소속->B" 와 "B-소속->A"(정규화 후 동일) 같은 이중 엣지를 하나로 합친다.
    # directed=True(전략 뷰)면 방향을 보존해 인과 흐름(A→B)이 화살표로 읽히게 한다.
    if directed:
        link_key = ((source_id, target_id), relation_name)
    else:
        link_key = (frozenset((source_id, target_id)), relation_name)

    if link_key in seen_links:
        return

    seen_links.add(link_key)

    link = {
        "source": source_id,
        "target": target_id,
        "type": relation_name,
    }
    if properties:
        link["properties"] = properties
    if depth is not None:
        link["depth"] = int(depth)
    links.append(link)


def _record_relation_nodes(record):
    source = record.get("source")
    target = record.get("target")

    if source is None or target is None:
        return record["n"], record["m"]

    return source, target


def _records_to_graph(records, directed=False):
    nodes = {}
    links = []
    seen_links = set()

    for record in records:
        r = record["r"]
        source, target = _record_relation_nodes(record)

        source_id = _add_node(nodes, source)
        target_id = _add_node(nodes, target)

        relation_name, invert = _norm_relation(_relation_name(r))
        if invert:
            source_id, target_id = target_id, source_id

        # 두 별칭이 같은 대표명으로 병합되어 생긴 자기참조 엣지는 제거
        if source_id == target_id:
            continue

        _add_link(
            links=links,
            seen_links=seen_links,
            source_id=source_id,
            target_id=target_id,
            relation_name=relation_name,
            directed=directed,
            properties=_relation_context_properties(r, relation_name),
            depth=record.get("depth"),
        )

    return {
        "nodes": list(nodes.values()),
        "links": links,
    }


def _records_to_facts(records):
    facts = []
    seen_facts = set()

    for record in records:
        r = record["r"]
        source, target = _record_relation_nodes(record)

        s_name = _canonical(_node_name(source))
        s_label = _node_label(source)
        t_name = _canonical(_node_name(target))
        t_label = _node_label(target)

        relation, invert = _norm_relation(_relation_name(r))
        if invert:
            s_name, t_name = t_name, s_name
            s_label, t_label = t_label, s_label

        # 병합으로 생긴 자기참조 제거
        if s_name == t_name:
            continue

        fact = {
            "source": s_name,
            "source_label": s_label,
            "relation": relation,
            "target": t_name,
            "target_label": t_label,
        }

        # 방향 무시 노드쌍 + 관계로 중복 제거
        fact_key = (frozenset((s_name, t_name)), relation)

        if fact_key in seen_facts:
            continue

        seen_facts.add(fact_key)
        facts.append(fact)

    return facts


def format_graph_facts(facts, max_facts=50):
    selected_facts = facts if max_facts is None else facts[:max_facts]

    return "\n".join(
        [
            (
                f"{idx}. ({fact['source_label']}) {fact['source']} "
                f"-[{fact['relation']}]-> "
                f"({fact['target_label']}) {fact['target']}"
                + (f" | {fact['detail']}" if fact.get("detail") else "")
            )
            for idx, fact in enumerate(selected_facts, start=1)
        ]
    )


def _generalize_graph_facts(facts, raw_threshold=300, detail_group_limit=12):
    """전체 관계를 읽은 뒤 큰 관계군을 결정적으로 집계해 LLM 입력을 압축한다.

    최근 N개를 고르는 방식이 아니라 모든 행이 개수·고유 노드·수치·픽/밴 빈도에
    반영된다. 작은 관계군은 원문 fact를 그대로 유지해 희소한 구체 관계도 보존한다.
    """
    facts = list(facts or [])
    if len(facts) <= raw_threshold:
        return facts

    grouped = defaultdict(list)
    for fact in facts:
        grouped[
            (
                fact.get("source_label") or "Node",
                fact.get("relation") or "RELATED_TO",
                fact.get("target_label") or "Node",
            )
        ].append(fact)

    generalized = []
    for (source_label, relation, target_label), items in grouped.items():
        if len(items) <= detail_group_limit:
            generalized.extend(items)
            continue

        source_counts = Counter(str(item.get("source") or "-") for item in items)
        target_counts = Counter(str(item.get("target") or "-") for item in items)
        numeric_values = defaultdict(list)
        selection_counts = Counter()
        for item in items:
            for key, value in (item.get("properties") or {}).items():
                if isinstance(value, bool):
                    numeric_values[key].append(int(value))
                elif isinstance(value, (int, float)):
                    numeric_values[key].append(float(value))
                elif key.startswith(("pick", "ban")) and str(value or "").strip():
                    selection_counts[str(value).strip()] += 1

        detail_parts = [
            f"전체 {len(items)}개 관계",
            f"고유 {source_label} {len(source_counts)}개",
            f"고유 {target_label} {len(target_counts)}개",
        ]
        top_sources = source_counts.most_common(5)
        top_targets = target_counts.most_common(5)
        if top_sources:
            detail_parts.append(
                "주요 출발 " + "·".join(f"{name} {count}회" for name, count in top_sources)
            )
        if top_targets:
            detail_parts.append(
                "주요 도착 " + "·".join(f"{name} {count}회" for name, count in top_targets)
            )
        for key, values in numeric_values.items():
            average = sum(values) / len(values)
            detail_parts.append(
                f"{key} 평균 {average:.2f} (최소 {min(values):g}, 최대 {max(values):g}, {len(values)}건)"
            )
        if selection_counts:
            detail_parts.append(
                "픽·밴 빈도 "
                + "·".join(
                    f"{name} {count}회" for name, count in selection_counts.most_common(12)
                )
            )

        generalized.append({
            "source": f"전체 {source_label}",
            "source_label": source_label,
            "relation": relation,
            "target": f"전체 {target_label}",
            "target_label": target_label,
            "hop": min(int(item.get("hop") or 1) for item in items),
            "detail": "; ".join(detail_parts),
            "properties": {
                "raw_fact_count": len(items),
                "distinct_source_count": len(source_counts),
                "distinct_target_count": len(target_counts),
                "generalized": True,
            },
        })

    return generalized


def _graph_to_facts(graph):
    """현재 화면에 남은 노드·엣지만 LLM/근거용 triple로 변환한다.

    개요 그래프를 pruning한 뒤에도 예전에는 잘려 나간 관계가 LLM context에 남아
    화면과 답변이 어긋났다. 이 변환을 단일 진실 공급원으로 사용해 둘을 맞춘다.
    """
    node_by_id = {str(node["id"]): node for node in graph.get("nodes", [])}
    facts = []
    seen = set()
    for link in graph.get("links", []):
        source = node_by_id.get(str(link.get("source")))
        target = node_by_id.get(str(link.get("target")))
        if not source or not target:
            continue
        relation = link.get("type") or "RELATED_TO"
        key = (str(source["id"]), str(target["id"]), relation)
        if key in seen:
            continue
        seen.add(key)
        fact = {
            "source": source.get("name") or source["id"],
            "source_id": source["id"],
            "source_label": source.get("label") or "Node",
            "relation": relation,
            "target": target.get("name") or target["id"],
            "target_id": target["id"],
            "target_label": target.get("label") or "Node",
        }
        detail = _format_relation_detail(relation, link.get("properties") or {})
        if detail:
            fact["detail"] = detail
        if link.get("properties"):
            fact["properties"] = dict(link["properties"])
        if link.get("depth") is not None:
            fact["hop"] = int(link["depth"])
        facts.append(fact)
    return facts


def _filter_graph_labels(graph, excluded_labels):
    """제외 라벨 노드와 그 노드에 닿는 링크를 반환 직전에도 방어적으로 제거한다."""
    excluded = set(excluded_labels or [])
    if not excluded:
        return graph
    keep_ids = {
        node["id"] for node in graph.get("nodes", [])
        if node.get("label") not in excluded
    }
    return {
        "nodes": [node for node in graph.get("nodes", []) if node["id"] in keep_ids],
        "links": [
            link for link in graph.get("links", [])
            if link.get("source") in keep_ids and link.get("target") in keep_ids
        ],
    }


# =========================
# 노드 중요도 랭킹 (일반 개요용)
# =========================
# 개요 그래프가 매칭 노드의 1-hop 이웃을 전부 끌어와 헤어볼이 되는 걸 막는다.
# 원칙: 중심(질문이 매칭한) 노드는 항상 남기고, 이웃은 '중요도 점수' 상위 N개만 남긴다.
# 나머지 노드는 사라지는 게 아니라, 노드 클릭 시 상세(get_node_detail)에서 전부 볼 수 있다.
# ※ 답변과 그림이 어긋나지 않도록 facts/context도 최종 화면 그래프와 함께 자른다.

# 라벨별 도메인 중요도 (핵심 엔티티 높게, 잡동사니 낮게)
LABEL_WEIGHT = {
    "Team": 5, "Player": 5, "Champion": 4, "League": 4, "Tournament": 4,
    "Coach": 4, "TacticalMove": 4, "Match": 3, "Game": 3, "Position": 3,
    "Objective": 3, "PlayerSub": 2, "Strategy": 2, "Patch": 2, "Day": 2,
    "Structure": 1, "Spell": 1, "Role": 1, "Group": 1, "Item": 1,
    "Entity": 1, "Node": 1,
}
OVERVIEW_NODE_CAP = 20

# 전략 질문 전용: 비선수(전략/상황) 노드는 '기사 빈도' 상위 이 개수만 화면 그래프에 남긴다.
# (로스터 선수 노드와 질문 중심 노드는 이 cap과 무관하게 항상 유지)
STRATEGY_NODE_CAP = 5

# 답변 생성 후 화면 그래프를 다시 고를 때 사용하는 표시 한도다. 분석용
# analysis_graph/analysis_facts에는 적용하지 않는다.
ANSWER_GRAPH_NODE_CAP = 18
ANSWER_GRAPH_EDGE_CAP = 24
STRATEGY_DEFAULT_DISPLAY_COUNT = 2

# 노드별 '서로 다른 기사 등장 수'(=자주 등장한 주요 전략일수록 큼)를 elementId로 정확히 집계.
NODE_ARTICLE_FREQ_CYPHER = """
MATCH (n) WHERE elementId(n) IN $eids
OPTIONAL MATCH (n)-[r]-() WHERE r.article_id IS NOT NULL
RETURN elementId(n) AS eid, count(DISTINCT r.article_id) AS freq
"""


def _node_article_freq(session, records):
    """
    레코드에 등장한 노드들의 '서로 다른 기사 수'를 canonical id별로 반환한다.
    일회성 상황 노드(기사 1건)는 낮게, 여러 기사에 반복 등장한 주요 전략은 높게 나온다.
    별칭이 대표명으로 병합된 경우 각 원본 노드(elementId)의 기사 수를 합산한다(근사).
    """
    elemids_by_id = {}
    for rec in records:
        for key in ("n", "m", "source", "target"):
            node = rec.get(key)
            if node is None:
                continue
            try:
                eid = node.element_id
            except AttributeError:
                continue
            gid = _canonical(_node_name(node))
            elemids_by_id.setdefault(gid, set()).add(eid)

    all_eids = list({e for eids in elemids_by_id.values() for e in eids})
    if not all_eids:
        return {}

    rows = session.run(NODE_ARTICLE_FREQ_CYPHER, eids=all_eids).data()
    freq_by_eid = {row["eid"]: row["freq"] for row in rows}
    return {
        gid: sum(freq_by_eid.get(e, 0) for e in eids)
        for gid, eids in elemids_by_id.items()
    }


def _order_facts_by_freq(facts, freq_map):
    """facts를 관련 노드의 기사 빈도 순으로 정렬(자주 쓴 주요 전략이 앞에 오게). 잘라내진 않는다."""
    return sorted(
        facts,
        key=lambda f: max(freq_map.get(f["source"], 0), freq_map.get(f["target"], 0)),
        reverse=True,
    )

# RDB(espo)의 실존 선수/팀/챔피언 이름 화이트리스트.
# CLOVA가 기사에서 뽑은 노이즈(윤민섭 기자·아나운서·이름변형 등)와, 정형 데이터에 실재하는
# 엔티티를 구분하는 '정답 기준'으로 쓴다. 실존 엔티티에 큰 가중치를 줘 개요에 우선 노출한다.
# lazy 로드 + 캐시, 실패해도(가중치만 생략) 전체가 죽지 않게 한다.
_RDB_WHITELIST = None
RDB_WHITELIST_BONUS = 10


def _rdb_whitelist():
    global _RDB_WHITELIST
    if _RDB_WHITELIST is not None:
        return _RDB_WHITELIST
    names = set()
    try:
        from mysql_connect import get_connection
        conn = get_connection()
        with conn.cursor() as cur:
            for tbl, col in (("player", "player_name"), ("team", "team_name"), ("champion", "champion_name")):
                cur.execute(f"SELECT {col} AS nm FROM {tbl}")
                for row in cur.fetchall():
                    raw = (row.get("nm") or "").strip()
                    if not raw:
                        continue
                    # 원 이름 + 대표명(alias 정규화 후)을 함께 등록해
                    # 노드가 대표명(예: 젠지→GenG)으로 저장돼 있어도 매칭되게 한다.
                    names.add(raw.lower())
                    names.add(str(_canonical(raw)).strip().lower())
        conn.close()
    except Exception as e:
        print("DEBUG RDB 화이트리스트 로드 실패(가중치 생략):", e)
    _RDB_WHITELIST = names
    print("DEBUG RDB 화이트리스트 크기:", len(_RDB_WHITELIST))
    return _RDB_WHITELIST


# =========================
# 특정 팀 질문 → RDB 로스터 강제 주입
# =========================
# 팀 질문(예: "T1 우승전략", "한화생명 밴픽")이면 그 팀의 로스터를 RDB(teamroster)에서
# 그대로 꺼내 그래프에 무조건 그린다. 기사 추출 결과에 안 잡힌 선수(예: 오너)도 반드시 나온다.
_RDB_TEAM_LOOKUP = None


def _rdb_team_lookup():
    """({별칭/팀명(lower): team_id}, {team_id: 대표 team_name}) 반환. lazy 캐시."""
    global _RDB_TEAM_LOOKUP
    if _RDB_TEAM_LOOKUP is not None:
        return _RDB_TEAM_LOOKUP
    alias_to_id, id_to_name = {}, {}
    try:
        from mysql_connect import get_connection
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT team_id, team_name FROM team")
            for row in cur.fetchall():
                tid = row["team_id"]
                nm = (row["team_name"] or "").strip()
                if nm:
                    id_to_name[tid] = nm
                    alias_to_id[nm.lower()] = tid
            cur.execute("SELECT team_id, alias_name FROM teamalias")
            for row in cur.fetchall():
                al = (row["alias_name"] or "").strip().lower()
                # 너무 짧은 별칭은 오탐 위험 → 2자 이상만. 대표명이 이미 있으면 덮지 않음.
                if len(al) >= 2:
                    alias_to_id.setdefault(al, row["team_id"])
        conn.close()
    except Exception as e:
        print("DEBUG 팀 lookup 로드 실패:", e)
    _RDB_TEAM_LOOKUP = (alias_to_id, id_to_name)
    return _RDB_TEAM_LOOKUP


def _detect_team_ids(texts):
    """키워드에 팀명·별칭이 있으면 [(team_id, team_name)] 반환(중복 팀 제거).

    라틴 별칭(kt, t1, hle…)은 단어 경계로 매칭해 'skt'에 'kt'가 걸리는 오탐을 막고,
    한글 별칭(티원, 한화…)은 조사 결합('티원의')을 고려해 포함 매칭한다.
    """
    alias_to_id, id_to_name = _rdb_team_lookup()
    if not alias_to_id:
        return []
    blob = " ".join(str(t).lower().strip() for t in texts if str(t).strip())
    hits = {}
    for alias, tid in alias_to_id.items():
        if alias.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", blob):
                hits[tid] = id_to_name.get(tid, alias)
        elif alias in blob:
            hits[tid] = id_to_name.get(tid, alias)
    return list(hits.items())


def _team_roster(team_id, scope=None):
    """질문 기간과 겹치는 teamroster 선수만 반환한다."""
    rows = []
    try:
        from mysql_connect import get_connection
        conn = get_connection()
        with conn.cursor() as cur:
            start_date = (scope or {}).get("start_date")
            end_date = (scope or {}).get("end_date")
            if not start_date:
                cur.execute(
                    """SELECT DATE(MAX(match_date)) AS latest_date FROM `match`
                       WHERE team_a_id = %s OR team_b_id = %s""",
                    (team_id, team_id),
                )
                row = cur.fetchone() or {}
                start_date = end_date = row.get("latest_date")
            if start_date:
                cur.execute(
                    """SELECT DISTINCT p.player_name AS name, p.position AS position, tr.role AS role
                       FROM teamroster tr
                       JOIN player p ON p.player_id = tr.player_id
                       WHERE tr.team_id = %s
                         AND tr.start_date <= %s
                         AND (tr.end_date IS NULL OR tr.end_date >= %s)""",
                    (team_id, end_date, start_date),
                )
                rows = cur.fetchall()
        conn.close()
    except Exception as e:
        print("DEBUG 로스터 조회 실패:", e)
    return rows


def _inject_team_rosters(graph, center_ids, team_hits, roster_scope=None):
    """
    팀 질문이면 RDB 로스터 선수 노드를 그래프에 강제로 추가하고(항상 표시),
    팀-[MEMBER_OF]-선수 엣지도 넣는다. 추가한 로스터 fact 리스트를 반환한다.
    center_ids 에 팀·선수 id를 넣어 이후 가지치기(prune)에서 절대 잘리지 않게 한다.
    """
    existing_nodes = {n["id"] for n in graph["nodes"]}
    existing_links = {(l["source"], l["target"], l["type"]) for l in graph["links"]}
    roster_facts = []

    for team_id, team_name in team_hits:
        tcanon = _canonical(team_name)
        if tcanon not in existing_nodes:
            graph["nodes"].append({"id": tcanon, "name": tcanon, "label": "Team"})
            existing_nodes.add(tcanon)
        center_ids.add(tcanon)  # 팀 노드 항상 유지

        roster = _team_roster(team_id, roster_scope)
        print(f"DEBUG 로스터 주입 team={team_name}({team_id}) 선수 {len(roster)}명:",
              [r["name"] for r in roster])
        for p in roster:
            pcanon = _canonical((p.get("name") or "").strip())
            if not pcanon:
                continue
            if pcanon not in existing_nodes:
                # RDB 실제 출전 로스터 → 무조건 정규 Player 로 표시
                graph["nodes"].append({"id": pcanon, "name": pcanon, "label": "Player"})
                existing_nodes.add(pcanon)
            center_ids.add(pcanon)  # 로스터 선수 항상 유지(가지치기 면제)

            link = (pcanon, tcanon, "MEMBER_OF")
            if link not in existing_links:
                graph["links"].append({"source": pcanon, "target": tcanon, "type": "MEMBER_OF"})
                existing_links.add(link)

            roster_facts.append({
                "source": pcanon, "source_label": "Player",
                "relation": "MEMBER_OF",
                "target": tcanon, "target_label": "Team",
            })
    return roster_facts


def _rank_and_prune_graph(graph, center_ids, cap=OVERVIEW_NODE_CAP,
                          freq_map=None, strategy_focus=False,
                          strategy_cap=STRATEGY_NODE_CAP):
    """중심 노드는 모두 유지하고, 이웃은 중요도 상위 (cap-중심수)개만 남긴다.

    strategy_focus=True(전략 질문)이면 특별 규칙을 쓴다:
      - 질문 중심 노드 + 로스터 선수(label=="Player")는 cap과 무관하게 항상 유지.
      - 나머지 비선수(전략/상황) 이웃은 '기사 빈도(freq_map)' 상위 strategy_cap개만 남긴다.
        → 특정 경기의 일회성 상황 노드가 화면을 뒤덮지 않고, 자주 쓴 주요 전략만 보인다.
      (facts/context는 여기서 자르지 않으므로 답변 자체는 풍부하게 유지된다.)
    """
    nodes = graph["nodes"]
    links = graph["links"]

    # 서브그래프 내 연결도(여러 핵심 노드와 이어질수록 큼)
    deg = {}
    for l in links:
        deg[l["source"]] = deg.get(l["source"], 0) + 1
        deg[l["target"]] = deg.get(l["target"], 0) + 1

    whitelist = _rdb_whitelist()

    def score(node):
        s = LABEL_WEIGHT.get(node["label"], 1) + 0.5 * min(deg.get(node["id"], 0), 8)
        # RDB 실존 엔티티면 크게 가중 → CLOVA 노이즈(윤민섭 등)를 개요에서 밀어낸다.
        name = str(node.get("name") or node.get("id") or "").strip().lower()
        if name in whitelist:
            s += RDB_WHITELIST_BONUS
        return s

    if strategy_focus:
        # 중심 + 로스터 선수는 항상 유지. 비선수 이웃만 기사 빈도 상위 strategy_cap개.
        fmap = freq_map or {}
        keep_ids = set()
        others = []
        for n in nodes:
            if n["id"] in center_ids or n["label"] == "Player":
                keep_ids.add(n["id"])
            else:
                others.append(n)
        # 1순위: 기사 빈도(자주 등장), 2순위: 기존 중요도 점수
        others.sort(key=lambda n: (fmap.get(n["id"], 0), score(n)), reverse=True)
        for n in others[:strategy_cap]:
            keep_ids.add(n["id"])
        return {
            "nodes": [n for n in nodes if n["id"] in keep_ids],
            "links": [l for l in links if l["source"] in keep_ids and l["target"] in keep_ids],
        }

    if len(nodes) <= cap:
        return graph

    keep_ids = {n["id"] for n in nodes if n["id"] in center_ids}
    others = sorted(
        (n for n in nodes if n["id"] not in center_ids),
        key=score, reverse=True,
    )
    for n in others:
        if len(keep_ids) >= cap:
            break
        keep_ids.add(n["id"])

    return {
        "nodes": [n for n in nodes if n["id"] in keep_ids],
        # 양쪽 끝이 살아남은 엣지만 유지
        "links": [l for l in links if l["source"] in keep_ids and l["target"] in keep_ids],
    }


_ANSWER_WEIGHT_STOPWORDS = {
    "경기", "전략", "전술", "근거", "요약", "상세", "해설", "데이터",
    "기반", "해당", "통해", "사용", "확인", "분석", "결과", "팀", "선수",
    "그리고", "하지만", "때문", "있습니다", "했습니다", "보여줍니다",
}


def resolve_strategy_display_request(question, max_count=STRATEGY_NODE_CAP):
    """전략 질문의 화면 표시 개수를 해석한다.

    개수 미지정은 2개, 숫자 지정은 그 수, 모든/전체 요청은 최대치로 처리한다.
    이 값은 화면 그래프에만 적용되고 전략 추론 및 상세 해설 범위는 줄이지 않는다.
    """
    maximum = max(1, int(max_count or STRATEGY_NODE_CAP))
    text = unicodedata.normalize("NFKC", str(question or "")).lower()
    numeric = re.search(r"(?<!\d)(\d{1,3})\s*(?:개|가지|건|경기|전략)", text)
    if numeric:
        requested = max(1, int(numeric.group(1)))
        return {
            "mode": "explicit_count", "requested": requested,
            "applied": min(requested, maximum), "max": maximum,
        }

    korean_counts = {
        "한 개": 1, "한개": 1, "한 경기": 1, "한경기": 1,
        "두 개": 2, "두개": 2, "두 경기": 2, "두경기": 2,
        "세 개": 3, "세개": 3, "세 경기": 3, "세경기": 3,
        "네 개": 4, "네개": 4, "네 경기": 4, "네경기": 4,
        "다섯 개": 5, "다섯개": 5, "다섯 경기": 5, "다섯경기": 5,
    }
    for phrase, requested in korean_counts.items():
        if phrase in text:
            return {
                "mode": "explicit_count", "requested": requested,
                "applied": min(requested, maximum), "max": maximum,
            }

    all_terms = (
        "모든 경기", "전체 경기", "전 경기", "전경기", "경기 전부",
        "전부 보여", "모두 보여", "가능한 모든", "전체를 보여",
    )
    if any(term in text for term in all_terms):
        return {
            "mode": "all", "requested": "all",
            "applied": maximum, "max": maximum,
        }

    default_count = min(STRATEGY_DEFAULT_DISPLAY_COUNT, maximum)
    return {
        "mode": "default", "requested": None,
        "applied": default_count, "max": maximum,
    }


def _answer_weight_text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return " ".join(re.findall(r"[0-9a-z가-힣]+", value))


def _answer_weight_tokens(value):
    return {
        token for token in _answer_weight_text(value).split()
        if len(token) >= 2 and token not in _ANSWER_WEIGHT_STOPWORDS
    }


def _answer_mentions(text, name):
    haystack = _answer_weight_text(text)
    needle = _answer_weight_text(name)
    if len(needle.replace(" ", "")) < 2 or needle in _ANSWER_WEIGHT_STOPWORDS:
        return False
    return needle in haystack or needle.replace(" ", "") in haystack.replace(" ", "")


def _answer_overlap(candidate, answer_tokens):
    overlap = _answer_weight_tokens(candidate) & set(answer_tokens or ())
    return sum(2 if len(token) >= 4 else 1 for token in overlap)


def _copy_graph(graph):
    return {
        "nodes": [dict(node) for node in (graph or {}).get("nodes", [])],
        "links": [dict(link) for link in (graph or {}).get("links", [])],
    }


def _shortest_answer_path(start_id, keep_ids, adjacency):
    if start_id in keep_ids:
        return [start_id], []
    queue = deque([start_id])
    previous = {start_id: None}
    previous_edge = {}
    destination = None
    while queue:
        current = queue.popleft()
        for neighbor, edge_index in adjacency.get(current, []):
            if neighbor in previous:
                continue
            previous[neighbor] = current
            previous_edge[neighbor] = edge_index
            if neighbor in keep_ids:
                destination = neighbor
                queue.clear()
                break
            queue.append(neighbor)
    if destination is None:
        return [start_id], []
    reversed_nodes = [destination]
    reversed_edges = []
    current = destination
    while current != start_id:
        reversed_edges.append(previous_edge[current])
        current = previous[current]
        reversed_nodes.append(current)
    return list(reversed(reversed_nodes)), list(reversed(reversed_edges))


def _score_answer_graph(candidate, summary, detail, focus_terms):
    nodes = candidate["nodes"]
    links = candidate["links"]
    node_by_id = {str(node.get("id")): node for node in nodes}
    summary_text = str(summary or "")
    detail_text = str(detail or "")
    summary_tokens = _answer_weight_tokens(summary_text)
    detail_tokens = _answer_weight_tokens(detail_text)
    focus_values = focus_terms if isinstance(focus_terms, (list, tuple, set)) else [focus_terms]
    focus_text = " ".join(str(value) for value in focus_values if value)

    degree = Counter()
    adjacency = defaultdict(list)
    for index, link in enumerate(links):
        source = str(link.get("source"))
        target = str(link.get("target"))
        if source not in node_by_id or target not in node_by_id:
            continue
        degree[source] += 1
        degree[target] += 1
        adjacency[source].append((target, index))
        adjacency[target].append((source, index))

    node_scores = {}
    node_relevance = {}
    summary_mentions = set()
    detail_mentions = set()
    focus_matches = set()
    for node_id, node in node_by_id.items():
        name = node.get("name") or node_id
        content = " ".join(str(node.get(key) or "") for key in ("name", "title"))
        relevance = 0.0
        if _answer_mentions(summary_text, name):
            relevance += 120
            summary_mentions.add(node_id)
        if _answer_mentions(detail_text, name):
            relevance += 70
            detail_mentions.add(node_id)
        if focus_text and _answer_mentions(focus_text, name):
            relevance += 100
            focus_matches.add(node_id)
        relevance += min(36, _answer_overlap(content, summary_tokens) * 6)
        relevance += min(24, _answer_overlap(content, detail_tokens) * 3)
        base = LABEL_WEIGHT.get(node.get("label"), 1) + min(degree[node_id], 8) * 0.4
        if node.get("label") == "InferredStrategy":
            base += 8
            if str(node.get("confidence") or "") == "높음":
                base += 4
            base += min(len(node.get("article_ids") or []), 4)
            base += min(len(node.get("structured_ids") or []), 4)
        node_relevance[node_id] = relevance
        node_scores[node_id] = base + relevance

    edge_scores = {}
    edge_relevance = {}
    for index, link in enumerate(links):
        source_id = str(link.get("source"))
        target_id = str(link.get("target"))
        if source_id not in node_by_id or target_id not in node_by_id:
            continue
        source_name = node_by_id[source_id].get("name") or source_id
        target_name = node_by_id[target_id].get("name") or target_id
        property_text = " ".join(str(value) for value in (link.get("properties") or {}).values())
        content = f"{source_name} {link.get('type') or ''} {target_name} {property_text}"
        relevance = min(60, _answer_overlap(content, summary_tokens) * 8)
        relevance += min(42, _answer_overlap(content, detail_tokens) * 4)
        if source_id in summary_mentions or target_id in summary_mentions:
            relevance += 30
        if source_id in detail_mentions or target_id in detail_mentions:
            relevance += 15
        if source_id in focus_matches or target_id in focus_matches:
            relevance += 20
        if link.get("type") in {"HAS_INFERRED_STRATEGY", "HAS_STRATEGY_EVIDENCE"}:
            relevance += 12
        edge_relevance[index] = relevance
        edge_scores[index] = 1 + relevance + 0.12 * (
            node_scores[source_id] + node_scores[target_id]
        )
        node_scores[source_id] += relevance * 0.18
        node_scores[target_id] += relevance * 0.18

    return {
        "node_by_id": node_by_id, "adjacency": adjacency,
        "node_scores": node_scores, "node_relevance": node_relevance,
        "edge_scores": edge_scores, "edge_relevance": edge_relevance,
        "summary_mentions": summary_mentions, "detail_mentions": detail_mentions,
        "focus_matches": focus_matches,
    }


def _expand_strategy_display_evidence(candidate):
    """전략 노드의 기사 근거를 우선 추가하고 RDB 근거 후보를 표시 그래프에 보강한다."""
    nodes = candidate["nodes"]
    links = candidate["links"]
    node_by_id = {str(node.get("id")): node for node in nodes}
    link_keys = {
        (str(link.get("source")), str(link.get("target")), link.get("type"))
        for link in links
    }
    for strategy_node in list(nodes):
        if strategy_node.get("label") != "InferredStrategy":
            continue
        strategy_id = str(strategy_node.get("id"))
        match_id = str(strategy_node.get("match_id") or "")

        for index, article in enumerate(strategy_node.get("article_evidence") or [], start=1):
            if not isinstance(article, dict):
                continue
            article_key = str(
                article.get("article_id") or article.get("evidence_id") or index
            )
            evidence_id = f"strategy-article-evidence:{strategy_id}:{article_key}"
            title = re.sub(r"\s+", " ", str(article.get("title") or "기사 원문")).strip()
            short_title = title if len(title) <= 22 else f"{title[:22]}…"
            if evidence_id not in node_by_id:
                evidence_node = {
                    "id": evidence_id,
                    "name": f"기사 · {short_title}",
                    "raw_name": title,
                    "label": "StrategyEvidence",
                    "title": str(article.get("text") or title),
                    "evidence_source": "article",
                    "article_id": article.get("article_id"),
                    "published_at": article.get("published_at") or "",
                    "match_id": match_id,
                }
                nodes.append(evidence_node)
                node_by_id[evidence_id] = evidence_node
            key = (strategy_id, evidence_id, "HAS_STRATEGY_EVIDENCE")
            if key not in link_keys:
                links.append({
                    "source": strategy_id,
                    "target": evidence_id,
                    "type": "HAS_STRATEGY_EVIDENCE",
                    "properties": {
                        "source": "article",
                        "detail": str(article.get("text") or title),
                        "article_id": article.get("article_id"),
                    },
                    "depth": 3,
                })
                link_keys.add(key)

        for evidence in strategy_node.get("rdb_evidence") or []:
            if not isinstance(evidence, dict) or evidence.get("id") is None:
                continue
            evidence_id = str(evidence["id"])
            if evidence_id not in node_by_id:
                evidence_node = dict(evidence)
                evidence_node["evidence_source"] = "rdb"
                nodes.append(evidence_node)
                node_by_id[evidence_id] = evidence_node
            else:
                node_by_id[evidence_id].setdefault("evidence_source", "rdb")
            key = (strategy_id, evidence_id, "HAS_STRATEGY_EVIDENCE")
            if key not in link_keys:
                links.append({
                    "source": strategy_id,
                    "target": evidence_id,
                    "type": "HAS_STRATEGY_EVIDENCE",
                    "properties": {
                        "source": "rdb",
                        "detail": str(evidence.get("title") or ""),
                    },
                    "depth": 3,
                })
                link_keys.add(key)
    return candidate


def _select_strategy_answer_graph(candidate, scores, request):
    nodes = candidate["nodes"]
    links = candidate["links"]
    node_by_id = scores["node_by_id"]
    node_scores = scores["node_scores"]
    edge_scores = scores["edge_scores"]
    strategy_ids = {
        node_id for node_id, node in node_by_id.items()
        if node.get("label") == "InferredStrategy"
    }
    strategy_limit = max(1, min(int(request.get("applied") or 1), STRATEGY_NODE_CAP))
    match_by_strategy = {}
    strategy_link_index = {}
    for index, link in enumerate(links):
        if link.get("type") != "HAS_INFERRED_STRATEGY":
            continue
        source_id = str(link.get("source"))
        target_id = str(link.get("target"))
        if target_id in strategy_ids and node_by_id.get(source_id, {}).get("label") == "Match":
            match_by_strategy[target_id] = source_id
            strategy_link_index[target_id] = index
        elif source_id in strategy_ids and node_by_id.get(target_id, {}).get("label") == "Match":
            match_by_strategy[source_id] = target_id
            strategy_link_index[source_id] = index

    ranked = sorted(strategy_ids, key=lambda value: node_scores[value], reverse=True)
    selected = []
    used_matches = set()
    # 같은 경기의 여러 문구가 기본 두 칸을 모두 차지하지 않도록 경기 다양성을 우선한다.
    for strategy_id in ranked:
        match_id = match_by_strategy.get(strategy_id)
        if match_id and match_id in used_matches:
            continue
        selected.append(strategy_id)
        if match_id:
            used_matches.add(match_id)
        if len(selected) >= strategy_limit:
            break
    for strategy_id in ranked:
        if len(selected) >= strategy_limit:
            break
        if strategy_id not in selected:
            selected.append(strategy_id)

    keep_ids = set()
    selected_edges = set()
    for strategy_id in selected:
        keep_ids.add(strategy_id)
        match_id = match_by_strategy.get(strategy_id)
        if match_id:
            keep_ids.add(match_id)
            selected_edges.add(strategy_link_index[strategy_id])
        evidence_edges = []
        for index, link in enumerate(links):
            if link.get("type") != "HAS_STRATEGY_EVIDENCE":
                continue
            source_id = str(link.get("source"))
            target_id = str(link.get("target"))
            if source_id == strategy_id and target_id in node_by_id:
                evidence_edges.append((index, target_id))
            elif target_id == strategy_id and source_id in node_by_id:
                evidence_edges.append((index, source_id))
        if evidence_edges:
            ranked_evidence = sorted(
                evidence_edges,
                key=lambda pair: (
                    node_by_id[pair[1]].get("evidence_source") == "article",
                    edge_scores.get(pair[0], 0) + node_scores.get(pair[1], 0),
                ),
                reverse=True,
            )
            for evidence_index, evidence_id in ranked_evidence[:3]:
                keep_ids.add(evidence_id)
                selected_edges.add(evidence_index)

    selected_matches = {
        match_by_strategy[strategy_id] for strategy_id in selected
        if strategy_id in match_by_strategy
    }
    for index, link in enumerate(links):
        source_id = str(link.get("source"))
        target_id = str(link.get("target"))
        if link.get("type") not in {"PLAYED_MATCH", "WON_MATCH"}:
            continue
        if source_id in selected_matches and node_by_id.get(target_id, {}).get("label") == "Team":
            keep_ids.add(target_id)
            selected_edges.add(index)
        elif target_id in selected_matches and node_by_id.get(source_id, {}).get("label") == "Team":
            keep_ids.add(source_id)
            selected_edges.add(index)

    selected_tournaments = set()
    for index, link in enumerate(links):
        if link.get("type") != "PART_OF_TOURNAMENT":
            continue
        source_id = str(link.get("source"))
        target_id = str(link.get("target"))
        if source_id in selected_matches and node_by_id.get(target_id, {}).get("label") == "Tournament":
            keep_ids.add(target_id)
            selected_tournaments.add(target_id)
            selected_edges.add(index)
        elif target_id in selected_matches and node_by_id.get(source_id, {}).get("label") == "Tournament":
            keep_ids.add(source_id)
            selected_tournaments.add(source_id)
            selected_edges.add(index)

    for index, link in enumerate(links):
        if link.get("type") != "BELONGS_TO_LEAGUE":
            continue
        source_id = str(link.get("source"))
        target_id = str(link.get("target"))
        if source_id in selected_tournaments and node_by_id.get(target_id, {}).get("label") == "League":
            keep_ids.add(target_id)
            selected_edges.add(index)
        elif target_id in selected_tournaments and node_by_id.get(source_id, {}).get("label") == "League":
            keep_ids.add(source_id)
            selected_edges.add(index)
    if not any(node_by_id[node_id].get("label") == "Team" for node_id in keep_ids):
        team_ids = [
            node_id for node_id, node in node_by_id.items()
            if node.get("label") in {"Team", "Player"}
        ]
        if team_ids:
            keep_ids.add(max(team_ids, key=lambda value: node_scores[value]))
    return keep_ids, selected_edges, {**request, "applied": len(selected)}


def _select_general_answer_graph(candidate, scores, node_cap, edge_cap):
    node_by_id = scores["node_by_id"]
    node_scores = scores["node_scores"]
    node_relevance = scores["node_relevance"]
    edge_scores = scores["edge_scores"]
    edge_relevance = scores["edge_relevance"]
    roots = sorted(
        scores["focus_matches"],
        key=lambda value: (
            node_by_id[value].get("label") in {"Team", "Player"},
            node_scores[value],
        ),
        reverse=True,
    )[:3]
    if not roots:
        answer_roots = scores["summary_mentions"] or scores["detail_mentions"]
        roots = sorted(answer_roots, key=lambda value: node_scores[value], reverse=True)[:2]
    if not roots:
        roots = [max(node_by_id, key=lambda value: node_scores[value])]
    keep_ids = set(roots)
    selected_edges = set()
    ranked_targets = sorted(
        (
            node_id for node_id in node_by_id
            if node_id not in keep_ids and node_relevance[node_id] > 0
        ),
        key=lambda value: node_scores[value],
        reverse=True,
    )
    for target_id in ranked_targets:
        path_nodes, path_edges = _shortest_answer_path(
            target_id, keep_ids, scores["adjacency"]
        )
        additions = set(path_nodes) - keep_ids
        if len(keep_ids) + len(additions) > max(2, int(node_cap)):
            continue
        keep_ids.update(path_nodes)
        selected_edges.update(path_edges)
    if len(keep_ids) < 2 and candidate["links"]:
        best_index = max(edge_scores, key=edge_scores.get)
        best_link = candidate["links"][best_index]
        keep_ids.update((str(best_link.get("source")), str(best_link.get("target"))))
        selected_edges.add(best_index)
    for index in sorted(edge_scores, key=edge_scores.get, reverse=True):
        if len(selected_edges) >= max(1, int(edge_cap)):
            break
        link = candidate["links"][index]
        if (
            str(link.get("source")) in keep_ids
            and str(link.get("target")) in keep_ids
            and edge_relevance.get(index, 0) > 0
        ):
            selected_edges.add(index)
    return keep_ids, selected_edges


def prioritize_answer_graph(
    kg_result, summary, detail, *, focus_terms=None, strategy_request=None,
    node_cap=ANSWER_GRAPH_NODE_CAP, edge_cap=ANSWER_GRAPH_EDGE_CAP,
):
    """상세 해설/요약과 맞닿은 근거 경로만 화면 그래프로 다시 선택한다."""
    candidate = _copy_graph(kg_result.get("analysis_graph") or kg_result.get("graph"))
    candidate = _expand_strategy_display_evidence(candidate)
    if not candidate["nodes"]:
        return {
            **kg_result, "graph": candidate, "facts": [],
            "display_graph_meta": {
                "node_count": 0, "link_count": 0, "strategy": strategy_request or {},
            },
        }
    scores = _score_answer_graph(candidate, summary, detail, focus_terms)
    strategy_ids = [
        node_id for node_id, node in scores["node_by_id"].items()
        if node.get("label") == "InferredStrategy"
    ]
    if strategy_ids:
        request = strategy_request or resolve_strategy_display_request("")
        keep_ids, selected_edges, strategy_meta = _select_strategy_answer_graph(
            candidate, scores, request
        )
    else:
        keep_ids, selected_edges = _select_general_answer_graph(
            candidate, scores, node_cap, edge_cap
        )
        strategy_meta = (
            {**strategy_request, "applied": 0, "available": 0}
            if strategy_request else {}
        )

    selected_nodes = [
        node for node in candidate["nodes"] if str(node.get("id")) in keep_ids
    ]
    selected_links = [
        link for index, link in enumerate(candidate["links"])
        if index in selected_edges
        and str(link.get("source")) in keep_ids
        and str(link.get("target")) in keep_ids
    ]
    node_values = [scores["node_scores"][str(node.get("id"))] for node in selected_nodes]
    low_node = min(node_values) if node_values else 0
    node_span = max((max(node_values) if node_values else 1) - low_node, 1)
    for node in selected_nodes:
        node_id = str(node.get("id"))
        node["importance"] = round(
            1 + 9 * (scores["node_scores"][node_id] - low_node) / node_span, 2
        )
        if node.get("graph_role") == "focus_team":
            node["importance"] = max(node["importance"], 8.0)
        elif node.get("graph_role") == "opponent_team":
            node["importance"] = min(node["importance"], 2.2)
        node["answer_relevant"] = bool(scores["node_relevance"][node_id] > 0)

    edge_values = [scores["edge_scores"][index] for index in selected_edges]
    low_edge = min(edge_values) if edge_values else 0
    edge_span = max((max(edge_values) if edge_values else 1) - low_edge, 1)
    for index in selected_edges:
        candidate["links"][index]["weight"] = round(
            1 + 4 * (scores["edge_scores"][index] - low_edge) / edge_span, 2
        )

    display_graph = {"nodes": selected_nodes, "links": selected_links}
    facts = _graph_to_facts(display_graph)
    return {
        **kg_result,
        "graph": display_graph,
        "facts": facts,
        "display_graph_meta": {
            "node_count": len(selected_nodes),
            "link_count": len(selected_links),
            "candidate_node_count": len(candidate["nodes"]),
            "candidate_link_count": len(candidate["links"]),
            "strategy": strategy_meta,
        },
    }


def _empty_graph_result(keywords=None):
    return {
        "keywords": keywords or [],
        "graph": {
            "nodes": [],
            "links": [],
        },
        "analysis_graph": {
            "nodes": [],
            "links": [],
        },
        "facts": [],
        "analysis_facts": [],
        "generalized_facts": [],
        "context": "",
    }


def search_graph_knowledge(
    keywords, limit=100, season_scoped=False, roster_scope=None,
    question=None, qtype="relational",
):
    """
    질문 키워드 기반으로 Neo4j subgraph와 LLM 답변용 triple context를 함께 반환한다.

    season_scoped=True 면 '시즌 중' 사실만 답하도록, 추출관계 중 출처 기사(published_at)가
    2025 LCK 시즌 범위(SEASON_START~SEASON_END) 밖인 것을 쿼리 단계에서 제외한다.
    (시즌후 이적 등 노이즈 방지. 날짜 판정은 LLM이 아니라 Cypher에서 결정적으로 수행)
    """

    original_keywords = keywords
    keywords = _extract_keywords(keywords)
    if not keywords and question:
        keywords = _extract_keywords(question)

    if not keywords:
        print("DEBUG no valid graph keywords")
        return _empty_graph_result()

    # 별칭까지 확장(Faker/페이커/이상혁 어떤 표기로 저장돼 있어도 매칭)
    search_keywords = _expand_with_aliases(keywords)
    print("DEBUG graph keywords:", keywords, "→ expanded:", search_keywords)

    # 검색용 키워드와 질문 포커스를 분리한다. LLM이 keywords에서 "전략"을 빼더라도
    # 원문 질문으로 전략 모드를 안정적으로 감지해야 한다.
    focus_input = question if question is not None else original_keywords
    focus_keywords = _extract_keywords(focus_input)
    excluded_labels, priority_relations = _query_focus(focus_input)
    strategy_focus = _is_strategy_focus(focus_input)
    hide_champion_nodes = _has_explicit_strategy_keyword(original_keywords)
    excluded_labels = [label for label in excluded_labels if label != "Champion"]
    if hide_champion_nodes:
        excluded_labels.append("Champion")
    relational_focus = str(qtype or "relational").lower() != "numeric"
    print("DEBUG focus excluded_labels:", excluded_labels,
          "priority:", priority_relations, "strategy_focus:", strategy_focus)

    driver = get_driver()

    with driver.session(database=get_database()) as session:
        # 정규화 DB에는 Article 노드가 없으므로 기사 published_at 필터를 붙이지 않는다.
        # 구형 기사 그래프에 Article이 실제 존재할 때만 관계 출처 날짜 필터를 활성화한다.
        article_season_filter = False
        if season_scoped:
            row = session.run(
                'MATCH (art) WHERE "Article" IN labels(art) '
                'RETURN count(art) > 0 AS enabled'
            ).single()
            article_season_filter = bool(row and row["enabled"])
        records = []
        query_center_ids = set()
        strategy_multihop_used = False
        freq_by_id = None

        # 전략 질문은 현재 정규화 스키마에서 질문 범위의 모든 경기를 명시 경로로 확장한다.
        # Champion 사용 질문과 전략 단어가 섞여도 전략 규칙(Champion 제외)이 우선한다.
        if strategy_focus:
            # 전략용 Match 범위는 MySQL과 동일한 질문 기간을 사용한다. 날짜 질문이
            # 아니면 고정된 '최근 시즌'으로 자르지 않고 저장된 전체 Match를 읽는다.
            strategy_scope = roster_scope or {}
            records = list(session.run(
                STRATEGY_MULTIHOP_CYPHER,
                keywords=search_keywords,
                scope_start=strategy_scope.get("start_date"),
                scope_end=strategy_scope.get("end_date"),
            ))
            strategy_multihop_used = bool(records)
            print("DEBUG normalized strategy multihop records:", len(records))

            # 예전 Strategy/TacticalMove DB를 계속 읽을 수 있도록 호환 폴백은 남긴다.
            if not records:
                records = list(session.run(
                    _apply_season_filter(STRATEGY_OVERVIEW_CYPHER, article_season_filter),
                    keywords=search_keywords,
                    excluded_relations=EXCLUDED_RELATIONS,
                    excluded_labels=excluded_labels,
                    priority_relations=priority_relations,
                    min_strategy_teams=MIN_STRATEGY_TEAMS_FOR_OVERVIEW,
                    limit=min(limit, STRATEGY_OVERVIEW_LIMIT),
                    season_start=SEASON_START,
                    season_end=SEASON_END,
                ))
                print("DEBUG legacy strategy overview records:", len(records))

        champion_usage_query = (
            not hide_champion_nodes
            and _looks_like_champion_usage_query(focus_keywords or keywords)
        )
        if champion_usage_query:
            player_keywords = _player_keywords_for_champion_usage(focus_keywords or keywords)
            player_keywords = _expand_with_aliases(player_keywords)
            print("DEBUG champion usage player keywords:", player_keywords)
            if player_keywords:
                records = list(session.run(
                    _apply_season_filter(CHAMPION_USAGE_CYPHER, article_season_filter),
                    player_keywords=player_keywords,
                    excluded_relations=EXCLUDED_RELATIONS,
                    excluded_labels=excluded_labels,
                    limit=limit,
                    season_start=SEASON_START,
                    season_end=SEASON_END,
                ))
                if not records:
                    print(f"DEBUG no champion usage graph match for player keywords {player_keywords}")
                    return _empty_graph_result(keywords)

        if not records:
            seed_records = list(session.run(
                _apply_season_filter(KEYWORD_CYPHER, article_season_filter),
                keywords=search_keywords,
                excluded_relations=EXCLUDED_RELATIONS,
                excluded_labels=excluded_labels,
                priority_relations=priority_relations,
                season_start=SEASON_START,
                season_end=SEASON_END,
            ))
            records = list(seed_records)

            query_center_ids = {
                _canonical(_node_name(row["n"]))
                for row in seed_records if row.get("n") is not None
            }

            # 일반 관계형 질문도 선택된 1-hop 이웃의 전체 2-hop 관계를 읽는다.
            # 표시 그래프만 아래에서 가지치고, 분석 근거는 전부 유지한다.
            if relational_focus and seed_records:
                center_element_ids = {
                    row["n"].element_id
                    for row in seed_records if row.get("n") is not None
                }
                mid_ids = list(dict.fromkeys(
                    row["m"].element_id
                    for row in seed_records
                    if row.get("m") is not None
                    and row["m"].element_id not in center_element_ids
                ))
                strategy_multihop_used = False
                if mid_ids:
                    second_hop = list(session.run(
                        _apply_season_filter(
                            RELATIONAL_SECOND_HOP_CYPHER, article_season_filter
                        ),
                        mid_ids=mid_ids,
                        center_ids=list(center_element_ids),
                        excluded_relations=EXCLUDED_RELATIONS,
                        excluded_labels=excluded_labels,
                        season_start=SEASON_START,
                        season_end=SEASON_END,
                    ))
                    records.extend(second_hop)
                    print("DEBUG controlled second-hop records:", len(second_hop))

        if not query_center_ids:
            query_center_ids = {
                _canonical(_node_name(row["n"]))
                for row in records if row.get("n") is not None
            }

        # 구형 기사 전략 그래프에서만 기사 빈도 랭킹을 쓴다.
        if strategy_focus and records and not strategy_multihop_used:
            freq_by_id = _node_article_freq(session, records)

    # 질문에 특정 팀이 있으면(예: T1 우승전략, 한화생명 밴픽) 그 팀 RDB 로스터를 탐지한다.
    # 별칭 확장(search_keywords)은 'skt t1' 같은 파생 토큰으로 오탐을 만들어 제외하고,
    # 사용자 질문에서 뽑힌 keywords 로만 팀을 판별한다.
    team_detection_terms = list(keywords)
    if question:
        team_detection_terms.insert(0, question)
    team_hits = _detect_team_ids(team_detection_terms)

    if not records:
        # 전략 질문을 로스터 1-hop 별로 대체하지 않는다. 근거 경로가 없으면 비었다고 알린다.
        if strategy_focus or not team_hits:
            print(f"DEBUG no graph match for keywords {keywords}")
            return _empty_graph_result(keywords)
        graph = {"nodes": [], "links": []}
        center_ids = set()
        roster_facts = _inject_team_rosters(
            graph, center_ids, team_hits, roster_scope=roster_scope
        )
        visible_facts = _graph_to_facts(graph)
        context = format_graph_facts(visible_facts)
        print("DEBUG records 없음 → 로스터만 그래프 구성:", len(graph["nodes"]), "노드")
        return {
            "keywords": keywords,
            "graph": graph,
            "analysis_graph": _copy_graph(graph),
            "facts": visible_facts,
            "analysis_facts": visible_facts,
            "generalized_facts": visible_facts,
            "context": context,
        }

    # 전략 뷰는 방향을 보존해 인과 흐름이 화살표로 읽히게 한다.
    graph = _records_to_graph(records, directed=strategy_focus)
    # 중요도 랭킹: 중심(매칭) 노드는 유지, 이웃은 상위 N개만 개요에 남긴다.
    # (나머지는 노드 클릭 시 상세 뷰에서 전부 확인 가능. facts는 자르지 않음.)
    center_ids = set(query_center_ids)

    # ★ 팀 질문이면 RDB 로스터 선수를 그래프에 강제 주입(prune 전에 → center_ids로 항상 유지).
    roster_facts = (
        _inject_team_rosters(graph, center_ids, team_hits, roster_scope=roster_scope)
        if team_hits and not strategy_focus else []
    )

    if hide_champion_nodes:
        graph = _filter_graph_labels(graph, {"Champion"})
    if strategy_multihop_used:
        graph = _collapse_strategy_games_to_matches(graph)

    # 분석 LLM은 질문 범위의 전체 관계를 보고, 화면은 가독성을 위해 별도로 가지치기한다.
    # 전략 그래프는 Match별 구조 자체가 분석 범위이므로 전부 표시한다.
    analysis_graph = _copy_graph(graph)
    analysis_facts = _graph_to_facts(analysis_graph)
    generalized_facts = _generalize_graph_facts(analysis_facts)
    if not strategy_multihop_used:
        graph = _rank_and_prune_graph(
            graph, center_ids, freq_map=freq_by_id, strategy_focus=strategy_focus
        )
    facts = _graph_to_facts(graph)
    context = format_graph_facts(generalized_facts, max_facts=None)

    if strategy_focus and not strategy_multihop_used:
        print("DEBUG strategy node freq (top):",
              sorted((freq_by_id or {}).items(), key=lambda kv: kv[1], reverse=True)[:8])
    print("DEBUG graph nodes:", len(graph["nodes"]))
    print("DEBUG graph links:", len(graph["links"]))
    print("DEBUG graph facts:", len(facts))
    print("DEBUG graph analysis facts:", len(analysis_facts))
    print("DEBUG graph generalized facts:", len(generalized_facts))
    print("DEBUG sample nodes:", graph["nodes"][:5])
    print("DEBUG sample links:", graph["links"][:5])

    return {
        "keywords": keywords,
        "graph": graph,
        "analysis_graph": analysis_graph,
        "facts": facts,
        "analysis_facts": analysis_facts,
        "generalized_facts": generalized_facts,
        "context": context,
    }


# =========================
# 메인 함수
# =========================

# =========================
# 노드 상세(드릴다운) 조회
# =========================
# 프론트에서 그래프 노드를 클릭하면, 그 노드의 "전체 이웃"을 다시 조회한다.
# 메인 그래프는 핵심만 보여주도록 걸러져 있으므로(경기/전술 등 제외),
# 상세 뷰에서는 걸렀던 것까지 포함해 관계별로 묶어 보여준다.

# 표준관계/원본관계 → 한국어 라벨 (카드 제목용)
REL_KO = {
    "MEMBER_OF": "소속 선수",
    "PLAYS_FOR": "소속 선수",
    "COACHES": "감독·코치",
    "PLAYS_ROLE": "포지션",
    "PICKED": "사용 챔피언",
    "PICKED_CHAMPION": "사용 챔피언",
    "PICKED_IN": "픽된 게임",
    "USED_STRATEGY": "전술·운영",
    "DIRECTED": "감독 지시",
    "MADE_CALL": "선수 판단",
    "WON": "우승",
    "WON_GAME": "승리한 게임",
    "WON_MATCH": "승리한 매치",
    "BEAT": "상대 전적",
    "COMPETES_WITH": "경쟁 상대",
    "PLAYED_IN": "참가·출전",
    "PLAYED_GAME": "출전 게임",
    "PLAYED_MATCH": "출전 매치",
    "TEAM_GAME_STAT": "게임 스탯",
    "PART_OF_MATCH": "소속 매치",
    "PART_OF_TOURNAMENT": "소속 대회",
    "BELONGS_TO_LEAGUE": "소속 리그",
    "PLAYED_ON": "경기일",
    "CAPTURED": "오브젝트 획득",
    "HAS_STRATEGY_EVIDENCE": "전략 근거",
    "HAS_INFERRED_STRATEGY": "경기별 추론 전략",
    "USES_INFERRED_STRATEGY": "추론 전략",
    "SUPPORTED_BY_MATCH": "대표 근거 경기",
}

# 카드 정렬 + 미니그래프 우선순위 (작을수록 위/우선)
_REL_ORDER = {
    "MEMBER_OF": 0, "PLAYS_FOR": 0, "COACHES": 1, "PLAYS_ROLE": 2,
    "DIRECTED": 3, "MADE_CALL": 4, "PICKED": 5, "PICKED_CHAMPION": 5,
    "USED_STRATEGY": 6, "WON": 7, "BEAT": 8, "COMPETES_WITH": 9,
    "PART_OF_TOURNAMENT": 10, "BELONGS_TO_LEAGUE": 10,
}

# 미니 서브그래프는 가독성을 위해 핵심 관계 위주로 이 개수만큼만 그린다.
DETAIL_GRAPH_CAP = 60


DETAIL_CYPHER = """
MATCH (n)
WHERE none(label IN labels(n) WHERE label IN $excluded_labels)
AND any(kw IN $names WHERE
    toLower(coalesce(n.name, '')) = kw
    OR toLower(coalesce(n.team_name, '')) = kw
    OR toLower(coalesce(n.player_name, '')) = kw
)
MATCH (n)-[r]-(m)
WHERE NOT type(r) IN $excluded_relations
AND none(label IN labels(m) WHERE label IN $excluded_labels)
RETURN n, r, m, startNode(r) AS source, endNode(r) AS target
LIMIT $limit
"""


def get_node_detail(name, limit=400):
    """
    특정 노드의 전체 이웃을 관계별 그룹 + 미니 서브그래프로 반환한다.
    (프론트 노드 클릭 시 드릴다운 상세 대시보드에 사용)
    """
    raw = str(name or "").strip()
    empty = {"id": raw, "name": raw, "label": None, "groups": [], "graph": {"nodes": [], "links": []}}
    if not raw:
        return empty

    # 클릭된 이름(대표명)을 별칭까지 확장해서 어떤 표기로 저장돼 있어도 매칭
    names = _expand_with_aliases([raw])
    names = [str(x).strip().lower() for x in names if str(x).strip()]

    driver = get_driver()
    with driver.session(database=get_database()) as session:
        records = list(session.run(
            DETAIL_CYPHER,
            names=names,
            excluded_relations=EXCLUDED_RELATIONS,
            excluded_labels=EXCLUDED_LABELS,
            limit=limit,
        ))

    if not records:
        print(f"DEBUG no node detail for {raw!r} (names={names})")
        return empty

    center_node = records[0]["n"]
    center_id = _canonical(_node_name(center_node))
    center_label = _node_label(center_node)
    center_name = _nounize_node_name(center_id, center_label)

    # 관계별로 이웃을 묶는다 (관계는 표준관계로 정규화해서 같은 의미끼리 합침)
    grouped = {}  # canonical_rel -> {neighbor_id: {id, name, label}}
    for rec in records:
        rel, _invert = _norm_relation(_relation_name(rec["r"]))
        neighbor = rec["m"]
        neighbor_id = _canonical(_node_name(neighbor))
        neighbor_label = _node_label(neighbor)
        if not neighbor_id or neighbor_id == center_id:
            continue
        grouped.setdefault(rel, {})[neighbor_id] = {
            "id": neighbor_id,
            "name": _nounize_node_name(neighbor_id, neighbor_label),
            "label": neighbor_label,
        }

    groups = []
    for rel, members in grouped.items():
        items = sorted(
            members.values(),
            key=lambda x: x["name"],
        )
        groups.append({
            "relation": rel,
            "relation_ko": REL_KO.get(rel, rel),
            "count": len(items),
            "items": items,
        })

    # 소속선수·감독 같은 핵심 관계를 위로, 그다음 개수 많은 순
    groups.sort(key=lambda g: (_REL_ORDER.get(g["relation"], 50), -g["count"]))

    # 미니 그래프: 전체(수백 개)를 그리면 헤어볼이 되므로 핵심 관계 위주로 잘라 그린다.
    graph_records = sorted(
        records,
        key=lambda rec: _REL_ORDER.get(_norm_relation(_relation_name(rec["r"]))[0], 50),
    )[:DETAIL_GRAPH_CAP]
    graph = _records_to_graph(graph_records)

    print(f"DEBUG node detail {center_name!r}: groups={len(groups)}, graph_nodes={len(graph['nodes'])}")
    return {
        "id": center_id,
        "name": center_name,
        "label": center_label,
        "groups": groups,
        "graph": graph,
    }


def search_graph_by_keyword(keywords, limit=100):
    """
    질문 키워드 기반 그래프 시각화용 함수.

    - 질문에서 키워드 추출
    - Neo4j 노드 이름 속성과 매칭
    - 매칭된 노드 주변 1-hop 관계 반환
    - 매칭 실패 시 빈 그래프 반환
    """

    return search_graph_knowledge(keywords, limit=limit)["graph"]


# =========================
# 직접 테스트용
# =========================

if __name__ == "__main__":
    result = search_graph_by_keyword("젠지 우승 전략 알려줘")

    print("nodes:", len(result["nodes"]))
    print("links:", len(result["links"]))
    print(result)
