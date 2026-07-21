"""
Neo4j/GraphRAG schema rules used by analysis and query code.

이 파일은 DB를 직접 수정하지 않는다. 현재 그래프를 어떤 레이어로 볼지,
어떤 라벨/관계를 정규화 대상으로 볼지 정의하는 기준표다.
"""

SOURCE_LABELS = {
    "Article",
    "ArticleChunk",
    "SourceEvidence",
    "Alias",
}

DOMAIN_LABELS = {
    "League",
    "Tournament",
    "Match",
    "Game",
    "Team",
    "Player",
    "Champion",
}

EXTRACTION_ENTITY_LABELS = {
    "Entity",
    "Coach",
    "Event",
    "GameEntity",
    "Group",
    "Item",
    "Object",
    "Objective",
    "Patch",
    "Position",
    "Role",
    "Spell",
    "Stage",
    "Strategy",
    "Structure",
    "Venue",
}

COMMUNITY_LABELS = {
    "Community",
    "CommunitySummary",
}

EVIDENCE_RELATIONS = {
    "HAS_CHUNK",
    "HAS_EVIDENCE",
    "SUPPORTS",
    "HAS_ALIAS",
}

GENERIC_RELATIONS = {
    "MENTIONS",
    "MENTIONED_WITH",
    "RELATED_TO",
    "SAME_AS",
    "HAS_EXTRACTED_RELATION",
}

LOW_SIGNAL_RELATIONS = EVIDENCE_RELATIONS | GENERIC_RELATIONS

QUERY_EXCLUDED_LABELS = sorted(SOURCE_LABELS)
QUERY_EXCLUDED_RELATIONS = sorted(LOW_SIGNAL_RELATIONS)

NORMALIZATION_TARGET_LABELS = {
    "team": "Team",
    "player": "Player",
    "champion": "Champion",
    "league": "League",
    "tournament": "Tournament",
    "match": "Match",
    "game": "Game",
    "coach": "Coach",
    "objective": "Objective",
    "role": "Role",
    "position": "Position",
    "patch": "Patch",
    "strategy": "Strategy",
}

NORMALIZATION_RELATION_HINTS = {
    "MEMBER_OF": {"source": "Player", "target": "Team"},
    "COACHES": {"source": "Coach", "target": "Team"},
    "PLAYS_ROLE": {"source": "Player", "target": "Position"},
    "PICKED_CHAMPION": {"source": "Player", "target": "Champion"},
    "PICKED_IN": {"source": "Champion", "target": "Game"},
    "PLAYED_IN": {"source": "Player", "target": "Game"},
    "PLAYED_GAME": {"source": "Team", "target": "Game"},
    "PLAYED_MATCH": {"source": "Team", "target": "Match"},
    "WON_GAME": {"source": "Team", "target": "Game"},
    "WON_MATCH": {"source": "Team", "target": "Match"},
    "PART_OF_MATCH": {"source": "Game", "target": "Match"},
    "PART_OF_TOURNAMENT": {"source": "Match", "target": "Tournament"},
    "BELONGS_TO_LEAGUE": {"source": "Tournament", "target": "League"},
    # 전술/운영 (Concept 쓰레기통을 대체하는 전략 관계)
    "USED_STRATEGY": {"source": "Team", "target": "Strategy"},
    "DIRECTED": {"source": "Coach", "target": "Player"},
    "MADE_CALL": {"source": "Player", "target": "Strategy"},
}

ARTICLE_GRAPH_LABELS = {
    "Article",
    "ArticleChunk",
    "SourceEvidence",
}

GLOBAL_GRAPH_LABELS = DOMAIN_LABELS | EXTRACTION_ENTITY_LABELS

# =========================
# 관계명 정규화 규칙
# =========================
# 그래프의 관계는 CLOVA가 뽑은 자유 텍스트(주로 한국어)라 같은 의미가 여러 이름으로,
# 심지어 인버스 방향("페이커-소속->T1" vs "T1-보유->페이커")으로 흩어진다.
# 아래 규칙으로 (표준관계 + 방향)을 통일해 중복 엣지를 하나로 합친다.
#
# 각 항목: (매칭키워드, 표준관계, invert)
#   - 매칭키워드가 관계명에 포함되면 표준관계로 치환
#   - invert=True 면 소스/타깃을 뒤집어 방향을 통일한다
#     (예: '보유/영입'은 팀→선수이므로 뒤집어 선수→팀 MEMBER_OF 로 맞춘다)
# canonical 값은 추출 프롬프트의 관계 enum과 동일하게 유지한다:
#   MEMBER_OF, COACHES, PICKED, WON, BEAT, PLAYED_IN
RELATION_SYNONYMS = [
    # 선수 ↔ 팀 소속
    ("소속팀", "MEMBER_OF", False),
    ("소속", "MEMBER_OF", False),
    ("멤버", "MEMBER_OF", False),
    ("보유", "MEMBER_OF", True),
    ("영입", "MEMBER_OF", True),
    # 코치 / 감독 → 팀
    ("코치", "COACHES", False),
    ("감독", "COACHES", False),
    # 포지션 / 역할 (선수 → 포지션)
    ("포지션", "PLAYS_ROLE", False),
    ("역할", "PLAYS_ROLE", False),
    # 챔피언 사용 / 픽
    ("사용", "PICKED", False),
    ("픽", "PICKED", False),
    ("플레이", "PICKED", False),
    # 우승 (팀 → 대회/경기)
    ("우승", "WON", False),
    # 팀 → 팀 승리
    ("이겼", "BEAT", False),
    ("꺾", "BEAT", False),
    ("제압", "BEAT", False),
    ("승리", "BEAT", False),
    # 참가 / 출전
    ("맞대결", "PLAYED_IN", False),
    ("출전", "PLAYED_IN", False),
    ("참가", "PLAYED_IN", False),
    # 전술 / 운영 (팀·선수 → 전술 개념)
    ("운영", "USED_STRATEGY", False),
    ("전술", "USED_STRATEGY", False),
    ("전략", "USED_STRATEGY", False),
    ("조합", "USED_STRATEGY", False),
    # 감독 지시
    ("지시", "DIRECTED", False),
    ("주문", "DIRECTED", False),
    # 선수 판단
    ("판단", "MADE_CALL", False),
    ("결정", "MADE_CALL", False),
]

COMMUNITY_GRAPH_SCHEMA = {
    "node_label": "Community",
    "summary_label": "CommunitySummary",
    "properties": [
        "community_id",
        "title",
        "summary",
        "level",
        "entity_count",
        "article_count",
        "created_at",
    ],
    "relations": [
        "CONTAINS_ENTITY",
        "SUPPORTED_BY",
        "SUMMARIZES",
        "PART_OF_COMMUNITY",
    ],
}

