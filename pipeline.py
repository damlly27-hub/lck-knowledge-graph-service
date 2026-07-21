"""
질의응답 파이프라인 오케스트레이터.

흐름:
  질문
   └→ [LLM #1] 의도 분석 → {의도, 키워드}
        ├→ MySQL 기사 의미검색 → [LLM #2] 기사 기반 답변
        ├→ Neo4j 관계검색       → [LLM #3] 그래프 기반 답변 + 그래프 데이터
        └→ MySQL(espo) 통계     → [LLM #4] RDB 통계 기반 답변
             └→ [LLM #5] 세 답변 통합 → 최종 답변 + 그래프
"""

import re

from clova_extract import (
    analyze_question_intent,
    call_hyperclova_summary,
    call_hyperclova_kg_answer,
    call_hyperclova_rdb_answer,
    call_hyperclova_presentation,
    call_hyperclova_strategy_insights,
)
from neo4j_query import (
    apply_rdb_champion_usage,
    apply_strategy_insights,
    prioritize_answer_graph,
    resolve_strategy_display_request,
    search_graph_knowledge,
)


_STRATEGY_QUESTION_TERMS = (
    "전략", "전술", "운영", "밴픽", "픽밴", "조합", "스와프", "주도권",
    "스노우볼", "한타", "교전", "시야", "오브젝트", "승리 요인", "어떻게 이겼",
    "strategy",
)


def _is_strategy_question(question):
    text = str(question or "").lower()
    return any(term in text for term in _STRATEGY_QUESTION_TERMS)


def _is_champion_usage_question(question):
    text = str(question or "").lower()
    return (
        any(token in text for token in ("챔피언", "챔프", "champion"))
        and any(token in text for token in ("사용", "쓴", "플레이", "픽", "고른", "선택", "used", "played"))
    )


_ROSTER_QUESTION_TERMS = ("로스터", "라인업", "멤버", "선수 구성", "선수 명단")
_TEAM_RECORD_TERMS = ("승률", "전적", "몇 승", "몇승", "승패")


def _is_match_lineup_question(question):
    text = str(question or "").lower()
    if any(token in text for token in ("기사", "보도", "뉴스", "article", "news")):
        return False
    return bool(
        re.search(r"(?:0?[1-9]|1[0-2])\s*월", text)
        and re.search(r"(?:0?[1-9]|[12]\d|3[01])\s*일", text)
        and any(token in text for token in ("매치", "경기", "대진", "match"))
    )


def _structured_rdb_kind(question):
    """LLM이 다시 쓸 필요가 없는 대표 정형 질문 유형을 판별한다."""
    text = str(question or "").lower()
    if _is_match_lineup_question(text):
        return "match_lineup"
    if _is_strategy_question(text):
        return None
    kinds = {
        "champion_usage": _is_champion_usage_question(text),
        "roster": any(term in text for term in _ROSTER_QUESTION_TERMS),
        "player_kda": "kda" in text and not any(
            term in text
            for term in ("월별", "월간", "달", "비교", "추이", "순위", "랭킹", "가장", "최고")
        ),
        "team_record": any(term in text for term in _TEAM_RECORD_TERMS),
    }
    matched = [kind for kind, enabled in kinds.items() if enabled]
    if len(matched) != 1:
        return None
    if matched[0] == "champion_usage":
        return "champion_usage"
    if matched[0] == "roster":
        return "roster"
    if matched[0] == "player_kda":
        return "player_kda"
    return "team_record"


def _requested_champion_count(question, default=6):
    match = re.search(r"(?<!\d)(\d{1,2})\s*(?:개|종)", str(question or ""))
    if not match:
        return default
    return max(1, min(int(match.group(1)), 20))


def _parse_roster_fact(fact):
    match = re.match(
        r"^\(Roster\)\s*\[([^\]]+)\]\s*(.+?)\(team_id=\d+\)\s*"
        r"(?:유효 로스터|소속 선수):\s*(.+)$",
        str(fact or "").strip(),
    )
    if not match:
        return None
    members = []
    for member in re.finditer(
        r"(?:^|,\s*)(.+?)\(([^,()]+),\s*player_id=(\d+)\)", match.group(3)
    ):
        members.append({
            "name": member.group(1).strip(),
            "role": member.group(2).strip(),
            "player_id": int(member.group(3)),
        })
    if not members:
        return None
    return {
        "label": match.group(1).strip(),
        "team": match.group(2).strip(),
        "members": members,
    }


def _structured_rdb_presentation(question, rdb_result, kind=None):
    """검증된 RDB fact를 그대로 사용자 문장으로 바꾼다.

    반환값이 있으면 이후 기사/KG/LLM 생성 단계를 건너뛴다. 필요한 fact가 없으면
    ``None``을 반환해 기존 혼합 파이프라인이 부족한 근거를 설명하도록 둔다.
    """
    kind = kind or _structured_rdb_kind(question)
    facts = list(rdb_result.get("facts") or [])

    if kind == "player_kda":
        answers = []
        pattern = re.compile(
            r"^\(Player\)\s*(?:\[([^\]]+)\]\s*)?(.+?)\[([^\]]*)\]\s*—\s*"
            r"(\d+)게임,\s*평균 KDA\s*([^\s]+)\s*"
            r"\(K\s*([^\s]+)\s*/\s*D\s*([^\s]+)\s*/\s*A\s*([^\s\)]+)\)"
        )
        for fact in facts:
            match = pattern.match(str(fact).strip())
            if not match:
                continue
            period, player, _position, games, kda, kills, deaths, assists = match.groups()
            prefix = f"{period} " if period else ""
            answers.append(
                f"{prefix}{player}는 {games}게임 기준 평균 KDA {kda}입니다. "
                f"평균 K/D/A는 {kills}/{deaths}/{assists}입니다."
            )
        if answers:
            text = " ".join(answers)
            return {
                "kind": kind,
                "summary": text,
                "detail": text,
                "graph_champion_usage": list(rdb_result.get("champion_usage") or [])[:6],
            }

    if kind == "roster":
        rosters = [parsed for fact in facts if (parsed := _parse_roster_fact(fact))]
        if rosters:
            answers = []
            for roster in rosters:
                members = ", ".join(
                    f"{member['name']}({member['role']})"
                    for member in roster["members"]
                )
                if roster["label"] == "전체 소속 이력":
                    answers.append(
                        f"{roster['team']} 소속 이력이 확인된 선수는 {members}입니다."
                    )
                else:
                    answers.append(
                        f"{roster['label']} {roster['team']} 로스터는 {members}입니다."
                    )
            text = " ".join(answers)
            return {
                "kind": kind,
                "summary": text,
                "detail": text,
                "rosters": rosters,
            }

    if kind == "champion_usage":
        usage_rows = list(rdb_result.get("champion_usage") or [])
        if usage_rows:
            count = _requested_champion_count(question)
            selected = usage_rows[:count]
            graph_selected = usage_rows[:max(6, count)]
            player = selected[0]["player_name"]
            items = ", ".join(
                f"{row['champion_name']} {row['games']}회({row['wins']}승)"
                for row in selected
            )
            summary = f"{player}의 사용 횟수 상위 {len(selected)}개 챔피언은 {items}입니다."
            pool = next(
                (str(fact) for fact in facts if str(fact).startswith("(ChampionPool)")),
                "",
            )
            pool_match = re.search(r"전체\s+(\d+)경기에서\s+(\d+)종", pool)
            detail = summary
            if pool_match:
                detail += (
                    f" 전체 {pool_match.group(1)}경기에서 "
                    f"{pool_match.group(2)}종의 챔피언을 사용했습니다."
                )
            return {
                "kind": kind,
                "summary": summary,
                "detail": detail,
                "champion_usage": selected,
                "graph_champion_usage": graph_selected,
            }

    if kind == "match_lineup":
        lineups = list(rdb_result.get("match_lineups") or [])
        scope_label = str((rdb_result.get("scope") or {}).get("label") or "해당 날짜")
        if not lineups:
            text = f"{scope_label} 진행된 매치를 RDB에서 찾지 못했습니다."
            return {
                "kind": kind,
                "summary": text,
                "detail": text,
                "match_lineups": [],
            }
        match_summaries = []
        detail_parts = []
        for match in lineups:
            score = ""
            if match.get("score_a") is not None and match.get("score_b") is not None:
                score = f" {match['score_a']}:{match['score_b']}"
            match_summaries.append(f"{match['label']}{score}")
            team_parts = []
            for team in match.get("teams") or []:
                players = []
                for player in team.get("players") or []:
                    champions = "·".join(
                        usage["champion_name"]
                        for usage in player.get("champions") or []
                    ) or "?"
                    players.append(
                        f"{player['player_name']}({player['position']}: {champions})"
                    )
                team_parts.append(f"{team['team_name']} — " + ", ".join(players))
            detail_parts.append(f"{match['label']}{score}. " + " / ".join(team_parts))
        summary = (
            f"{scope_label} 진행된 매치는 " + ", ".join(match_summaries)
            + "입니다. 그래프에는 참가 팀, 실제 출전 선수, 사용 챔피언을 연결했습니다."
        )
        return {
            "kind": kind,
            "summary": summary,
            "detail": " ".join(detail_parts),
            "match_lineups": lineups,
        }

    if kind == "team_record":
        answers = []
        pattern = re.compile(
            r"^\(Team\)\s*(?:\[([^\]]+)\]\s*)?(.+?)\s*—\s*"
            r"매치\s*(\d+)전\s*(\d+)승\s*(\d+)패,\s*승률\s*([^%\s]+)%"
        )
        for fact in facts:
            match = pattern.match(str(fact).strip())
            if not match:
                continue
            period, team, matches, wins, losses, win_rate = match.groups()
            prefix = f"{period} " if period else ""
            answers.append(
                f"{prefix}{team}은 {matches}전 {wins}승 {losses}패로 승률 {win_rate}%입니다."
            )
        if answers:
            text = " ".join(answers)
            return {"kind": kind, "summary": text, "detail": text}

    return None


def _empty_kg_result(keywords=None):
    return {
        "keywords": keywords if isinstance(keywords, list) else [],
        "graph": {"nodes": [], "links": []},
        "analysis_graph": {"nodes": [], "links": []},
        "facts": [],
        "analysis_facts": [],
        "generalized_facts": [],
        "context": "",
    }


def _structured_rdb_graph(structured, keywords=None):
    """정형 질문에 맞는 관계만 RDB 결과에서 결정적으로 구성한다."""
    result = _empty_kg_result(keywords)
    if not structured:
        return result

    if structured["kind"] in {"player_kda", "champion_usage"}:
        rows = structured.get("graph_champion_usage") or []
        player = rows[0]["player_name"] if rows else ""
        return apply_rdb_champion_usage(result, player, rows)

    if structured["kind"] == "match_lineup":
        nodes = []
        links = []
        facts = []
        node_ids = set()

        def add_node(node):
            node_id = str(node["id"])
            if node_id not in node_ids:
                nodes.append(node)
                node_ids.add(node_id)

        for match in structured.get("match_lineups") or []:
            match_id = f"match:{match['match_id']}"
            add_node({
                "id": match_id,
                "name": match["label"],
                "label": "Match",
                "title": match["label"],
            })
            for team in match.get("teams") or []:
                team_name = team["team_name"]
                add_node({"id": team_name, "name": team_name, "label": "Team"})
                match_properties = {
                    "match_date": match["match_date"],
                    "score_a": match.get("score_a"),
                    "score_b": match.get("score_b"),
                }
                links.append({
                    "source": team_name,
                    "target": match_id,
                    "type": "PLAYED_MATCH",
                    "properties": match_properties,
                })
                facts.append({
                    "source": team_name,
                    "source_label": "Team",
                    "relation": "PLAYED_MATCH",
                    "target": match["label"],
                    "target_label": "Match",
                    "hop": 1,
                    "detail": match["match_date"],
                    "properties": match_properties,
                })
                for player in team.get("players") or []:
                    player_name = player["player_name"]
                    add_node({
                        "id": player_name,
                        "name": player_name,
                        "label": "Player",
                    })
                    membership_properties = {
                        "role": player["position"],
                        "match_id": match["match_id"],
                    }
                    links.append({
                        "source": player_name,
                        "target": team_name,
                        "type": "MEMBER_OF",
                        "properties": membership_properties,
                    })
                    facts.append({
                        "source": player_name,
                        "source_label": "Player",
                        "relation": "MEMBER_OF",
                        "target": team_name,
                        "target_label": "Team",
                        "hop": 2,
                        "detail": f"역할 {player['position']}, 실제 출전",
                        "properties": membership_properties,
                    })
                    for usage in player.get("champions") or []:
                        champion = usage["champion_name"]
                        add_node({
                            "id": champion,
                            "name": champion,
                            "label": "Champion",
                        })
                        usage_properties = {
                            "games": int(usage.get("games") or 0),
                            "game_nos": list(usage.get("game_nos") or []),
                            "match_id": match["match_id"],
                        }
                        links.append({
                            "source": player_name,
                            "target": champion,
                            "type": "PICKED_CHAMPION",
                            "properties": usage_properties,
                        })
                        facts.append({
                            "source": player_name,
                            "source_label": "Player",
                            "relation": "PICKED_CHAMPION",
                            "target": champion,
                            "target_label": "Champion",
                            "hop": 3,
                            "detail": "세트 " + ", ".join(
                                str(value) for value in usage_properties["game_nos"]
                            ),
                            "properties": usage_properties,
                        })
        graph = {"nodes": nodes, "links": links}
        return {
            **result,
            "graph": graph,
            "analysis_graph": graph,
            "facts": facts,
            "analysis_facts": facts,
            "generalized_facts": facts,
            "context": "\n".join(
                f"{fact['source']} -[{fact['relation']}]-> {fact['target']} | {fact['detail']}"
                for fact in facts
            ),
        }

    if structured["kind"] != "roster":
        return result

    nodes = []
    links = []
    facts = []
    node_ids = set()
    for roster in structured.get("rosters") or []:
        team = roster["team"]
        if team not in node_ids:
            nodes.append({"id": team, "name": team, "label": "Team"})
            node_ids.add(team)
        for member in roster["members"]:
            player = member["name"]
            role = member["role"]
            if player not in node_ids:
                nodes.append({"id": player, "name": player, "label": "Player"})
                node_ids.add(player)
            links.append({
                "source": player,
                "target": team,
                "type": "MEMBER_OF",
                "properties": {"role": role, "as_of": roster["label"]},
            })
            facts.append({
                "source": player,
                "source_label": "Player",
                "relation": "MEMBER_OF",
                "target": team,
                "target_label": "Team",
                "hop": 1,
                "detail": f"역할 {role}, {roster['label']}",
                "properties": {"role": role, "as_of": roster["label"]},
            })
    graph = {"nodes": nodes, "links": links}
    return {
        **result,
        "graph": graph,
        "analysis_graph": graph,
        "facts": facts,
        "analysis_facts": facts,
        "generalized_facts": facts,
        "context": "\n".join(
            f"{fact['source']} -[MEMBER_OF]-> {fact['target']} | {fact['detail']}"
            for fact in facts
        ),
    }


def _entities_from_rdb(rdb_result):
    """
    RDB 결과에서 '답이 된 엔티티' 이름을 뽑는다.
    - 질문에서 해소된 엔티티(entities.teams/players)
    - 집계/랭킹 답 문자열의 team_name=?, player_name=? (예: "가장 승률 낮은 팀"→DN 숲)
    질문에 엔티티가 없어 그래프가 비었을 때, 이 이름들로 그래프를 다시 그리기 위한 용도.
    """
    names = []
    ents = rdb_result.get("entities") or {}
    names += ents.get("teams") or []
    names += ents.get("players") or []
    for fact in rdb_result.get("facts") or []:
        for m in re.findall(r"(?:team_name|player_name)\s*=\s*([^,]+)", str(fact)):
            names.append(m.strip())
    # 중복 제거(순서 유지), 빈 값 제거
    return list(dict.fromkeys(n for n in names if n))


def _search_articles_safe(question, top_k=3, season_scoped=False):
    """
    기사 의미기반 검색. season_scoped 면 시즌 범위 밖 기사(시즌후 이적 등)를 제외한다.
    import/조회 실패가 전체 답변을 죽이지 않도록 감싼다.
    """
    try:
        from article_search import search_articles
        return search_articles(question, top_k=top_k, season_scoped=season_scoped)
    except Exception as e:
        print("DEBUG article search failed:", e)
        return []


def _search_article_evidence_safe(
    question,
    top_k=3,
    season_scoped=False,
    entity_names=None,
    rdb_scope=None,
):
    """분석/UI용 기사 근거. 엔티티·질문 기간 범위를 우선 적용한다."""
    try:
        from article_search import search_article_evidence
        scope = rdb_scope or {}
        return search_article_evidence(
            question,
            top_k=top_k,
            season_scoped=season_scoped,
            entity_names=entity_names,
            start_date=scope.get("start_date"),
            end_date=scope.get("end_date"),
        )
    except Exception as e:
        print("DEBUG article evidence search failed:", e)
        return []


def _search_rdb_safe(question, keywords, *, allow_text_to_sql=True):
    """
    RDB(espo) 통계 검색. 연결/조회 실패가 전체 답변을 죽이지 않도록 감싼다.
    (pymysql 등은 모듈 import 시점 부담이 있어 지연 import)
    """
    try:
        from rdb_query import search_rdb_knowledge
        return search_rdb_knowledge(
            question,
            keywords,
            max_facts=None,
            allow_text_to_sql=allow_text_to_sql,
        )
    except Exception as e:
        print("DEBUG RDB search failed:", e)
        return {
            "keywords": [], "entities": {"players": [], "teams": []},
            "entity_ids": {"players": [], "teams": []},
            "facts": [], "context": "", "sql": None, "scope": {},
            "evidence": [], "total_facts": 0, "truncated": False,
            "champion_usage": [],
            "match_lineups": [],
        }


def _article_scope_from_rdb(rdb_result):
    """명시 기간이 없으면 조회된 전체 경기의 실제 최소·최대 날짜를 기사 범위로 쓴다."""
    scope = dict(rdb_result.get("scope") or {})
    if scope.get("start_date") or scope.get("end_date"):
        return scope
    dates = []
    for fact in rdb_result.get("facts") or []:
        if not str(fact).startswith(("(Match)", "(Game)")):
            continue
        dates.extend(re.findall(r"20\d{2}-\d{2}-\d{2}", str(fact)))
    if dates:
        scope["start_date"] = min(dates)
        scope["end_date"] = max(dates)
    return scope


def _select_summary_facts(
    question, rdb_items, kg_items, article_items, qtype="numeric"
):
    def unique(items):
        seen = set()
        result = []
        for item in items:
            key = item.get("id") or item.get("text") or repr(item)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    q = str(question or "").lower()
    selected = []

    if any(token in q for token in _ROSTER_QUESTION_TERMS):
        roster = [item for item in rdb_items if "(Roster)" in item["text"]]
        if roster:
            return unique(roster)

    if _is_champion_usage_question(question):
        champion_pool = [
            item for item in rdb_items
            if str(item.get("text") or "").startswith("(ChampionPool)")
        ]
        champion_usage = [
            item for item in rdb_items
            if str(item.get("text") or "").startswith("(ChampionUsage)")
        ]
        if champion_pool:
            return unique([*champion_pool, *champion_usage])

    # 관계형·전략 질문은 실제 화면 그래프의 연결을 최종 요약 근거로 먼저 쓴다.
    # 예전에는 RDB fact가 하나라도 있으면 KG가 summary_facts에서 완전히 빠져,
    # 지식그래프를 조회하고도 최종 답은 1-hop 명단/정형 통계만으로 생성됐다.
    strategy_focus = _is_strategy_question(question)
    if kg_items and (qtype == "relational" or strategy_focus):
        multi_hop = [item for item in kg_items if int(item.get("hop") or 1) > 1]
        inferred_strategies = [
            item for item in kg_items
            if item.get("relation") == "HAS_INFERRED_STRATEGY"
        ]
        strategy_evidence = [
            item for item in kg_items
            if item.get("relation") == "HAS_STRATEGY_EVIDENCE"
        ]
        if strategy_focus and inferred_strategies:
            match_names = {item.get("source") for item in inferred_strategies}
            inferred_ids = {item.get("target") for item in inferred_strategies}
            match_connectors = [
                item for item in kg_items
                if item.get("relation") == "PLAYED_MATCH"
                and item.get("target") in match_names
            ]
            evidence_edges = [
                item for item in kg_items
                if item.get("relation") == "HAS_STRATEGY_EVIDENCE"
                and item.get("source") in inferred_ids
            ]
            selected = [
                *match_connectors,
                *inferred_strategies,
                *evidence_edges,
            ]
        elif strategy_focus and strategy_evidence:
            match_names = {item.get("source") for item in strategy_evidence}
            connectors = [
                item for item in kg_items
                if item.get("relation") in {"WON_MATCH", "PLAYED_MATCH"}
                and item.get("target") in match_names
            ]
            selected = [*connectors, *strategy_evidence]
        elif strategy_focus and multi_hop:
            first_path_edge = multi_hop[0]
            path_names = {first_path_edge.get("source"), first_path_edge.get("target")}
            connector = next(
                (
                    item for item in kg_items
                    if int(item.get("hop") or 1) == 1
                    and path_names.intersection({item.get("source"), item.get("target")})
                ),
                kg_items[0],
            )
            # 질문 범위의 전체 KG 경로를 상세 해설에 전달한다. 요약은 이 전체 상세
            # 해설을 별도 LLM이 압축하므로 여기서 임의의 2~3개 엣지만 고르지 않는다.
            selected = [connector, *multi_hop]
        else:
            selected = list(kg_items)
        # 전략 요약은 KG의 경기·픽 관계를 중심으로 두되, 집계된 RDB 전략지표 한 줄도
        # 함께 허용해 상세 해설과 요약의 수치가 서로 어긋나지 않게 한다.
        if strategy_focus:
            team_overview = [
                item for item in rdb_items
                if str(item.get("text") or "").startswith("(Team)")
            ]
            strategy_stats = [
                item for item in rdb_items
                if str(item.get("text") or "").startswith("(전략지표)")
            ]
            selected.extend(team_overview)
            selected.extend(strategy_stats)
            selected.extend(article_items)
        return unique(selected)

    if rdb_items:
        comparisons = [item for item in rdb_items if "(월별 비교 기준)" in item["text"]]
        if comparisons:
            selected.extend(comparisons[:1])
            if comparisons:
                match = re.search(r"(20\d{2}년\s*\d{1,2}월)", comparisons[0]["text"])
                if match:
                    selected.extend([
                        item for item in rdb_items
                        if "(월별 " in item["text"]
                        and "(월별 비교 기준)" not in item["text"]
                        and match.group(1) in item["text"]
                    ][:1])
        else:
            selected = [
                item for item in rdb_items
                if item["text"].startswith(("(Team)", "(Player)", "(전략지표)", "(RDB)"))
            ]
        if not selected:
            selected = list(rdb_items)

    if not selected and kg_items:
        selected = list(kg_items)
    if not selected and article_items:
        selected = list(article_items)
    return unique(selected)


def _build_evidence_bundle(
    question, article_evidence, kg_result, rdb_result, season_scoped, qtype
):
    articles = []
    for i, item in enumerate(article_evidence or [], start=1):
        text = (
            item.get("analysis_text")
            or item.get("matched_chunk")
            or item.get("excerpt")
            or ""
        )
        if not text:
            continue
        articles.append({
            "id": f"A{i}",
            "article_id": item.get("article_id"),
            "chunk_id": item.get("chunk_id"),
            "title": item.get("title") or "기사 원문",
            "text": text,
        })

    kg = []
    kg_analysis_facts = (
        kg_result.get("generalized_facts")
        if kg_result.get("generalized_facts") is not None
        else kg_result.get("analysis_facts", kg_result.get("facts"))
    ) or []
    for i, fact in enumerate(kg_analysis_facts, start=1):
        detail = str(fact.get("detail") or "").strip()
        text = (
            f"{fact.get('source', '-')} -[{fact.get('relation', 'RELATED_TO')}]-> "
            f"{fact.get('target', '-')}"
        )
        if detail:
            text += f" | {detail}"
        kg.append({
            "id": f"K{i}",
            "source": fact.get("source"),
            "source_label": fact.get("source_label"),
            "relation": fact.get("relation"),
            "target": fact.get("target"),
            "target_label": fact.get("target_label"),
            "hop": fact.get("hop", 1),
            "detail": detail,
            "properties": fact.get("properties") or {},
            "text": text,
        })

    rdb = rdb_result.get("evidence") or [
        {"id": f"R{i}", "text": fact}
        for i, fact in enumerate(rdb_result.get("facts") or [], start=1)
    ]
    match_names = {
        item.get("target")
        for item in kg
        if item.get("relation") in {"PLAYED_MATCH", "WON_MATCH"}
        and item.get("target_label") == "Match"
    }
    return {
        "scope": {
            "season_scoped": bool(season_scoped),
            "strategy_focus": _is_strategy_question(question),
            "rdb": rdb_result.get("scope") or {},
            "resolved_entities": rdb_result.get("entity_ids") or {},
            "coverage": {
                "article_count": len(articles),
                "kg_fact_count": len(kg),
                "kg_raw_fact_count": len(
                    kg_result.get("analysis_facts", kg_result.get("facts") or [])
                ),
                "kg_generalized_fact_count": len(kg),
                "rdb_fact_count": len(rdb),
                "rdb_total_facts": rdb_result.get("total_facts", len(rdb)),
                "rdb_truncated": bool(rdb_result.get("truncated")),
                "match_count": len(match_names),
            },
        },
        "article": articles,
        "kg": kg,
        "rdb": rdb,
        "summary_facts": _select_summary_facts(
            question, rdb, kg, articles, qtype=qtype
        ),
    }


def run_pipeline(question, top_k=5, graph_limit=100):
    # 1) 의도 분석 (LLM #1) — 질문 유형(qtype)으로 DB 가중치를 정한다.
    intent_result = analyze_question_intent(question)
    keywords = intent_result.get("keywords") or question
    qtype = intent_result.get("qtype") or "relational"
    structured_kind = _structured_rdb_kind(question)
    # season_scoped: '시즌 중' 사실만 필요한 질문이면 시즌후 기사/관계를 검색에서 제외.
    season_scoped = intent_result.get("season_scoped", True)
    print("DEBUG intent:", intent_result.get("intent"))
    print("DEBUG keywords:", keywords)
    print("DEBUG qtype:", qtype)
    print("DEBUG season_scoped:", season_scoped)
    print("DEBUG structured RDB kind:", structured_kind)

    # 2) RDB를 먼저 읽어 기사 검색에 사용할 canonical 팀·선수와 기간을 확정한다.
    # 고정 템플릿이 질문을 완전히 처리하면 rdb_query의 커버리지 게이트가
    # Text-to-SQL 폴백을 생략한다. 엔티티가 해소되지 않으면 기존 폴백은 유지한다.
    rdb_result = _search_rdb_safe(
        question,
        keywords,
        allow_text_to_sql=True,
    )
    structured = _structured_rdb_presentation(
        question, rdb_result, kind=structured_kind
    )
    rdb_answer = (
        structured["detail"]
        if structured
        else call_hyperclova_rdb_answer(question, rdb_result["context"])
    )

    article_entity_names = list(dict.fromkeys([
        *(rdb_result.get("entities", {}).get("teams") or []),
        *(rdb_result.get("entities", {}).get("players") or []),
    ]))
    if structured:
        # KDA·로스터·챔피언 사용량처럼 RDB가 답을 완성한 질문은 기사 LLM을 생략한다.
        analysis_article_evidence = []
        article_evidence = []
        analysis_docs = []
        docs = []
        article_context = ""
        article_answer = "정형 RDB 질문이므로 기사 검색을 사용하지 않았습니다."
    else:
        # 3) 분석에는 엔티티·기간 범위의 전체 고유 기사를 사용한다. UI/응답 표기는
        # 의미 유사도 상위 top_k개(기본 5개)만 별도로 잘라 전달한다.
        analysis_article_evidence = _search_article_evidence_safe(
            question,
            top_k=None,
            season_scoped=season_scoped,
            entity_names=article_entity_names,
            rdb_scope=_article_scope_from_rdb(rdb_result),
        )
        article_evidence = analysis_article_evidence[:top_k]
        analysis_docs = [
            item.get("analysis_text") or item.get("matched_chunk") or item.get("excerpt") or ""
            for item in analysis_article_evidence
        ]
        analysis_docs = [text for text in analysis_docs if text]
        docs = [
            item.get("matched_chunk") or item.get("excerpt") or ""
            for item in article_evidence
        ]
        docs = [text for text in docs if text]
        print(
            "DEBUG article evidence: analysis=",
            len(analysis_article_evidence),
            "display=",
            len(article_evidence),
            "entities=",
            article_entity_names,
        )
        if analysis_docs:
            article_context = "\n\n".join(
                f"[참고자료 {i + 1}]\n{doc}" for i, doc in enumerate(analysis_docs)
            )
            article_answer = call_hyperclova_summary(question, article_context)
        else:
            article_context = ""
            article_answer = "관련 기사를 찾지 못했습니다."

    # 4) 관계 검색(Neo4j) → 그래프 기반 답변
    #    질문 키워드로 검색하되, 결과가 비면(예: "가장 승률 낮은 팀"처럼 질문에 엔티티가 없음)
    #    RDB가 찾아낸 답 엔티티(예: DN 숲)로 다시 검색해 '답을 그래프로' 보여준다.
    if structured:
        kg_result = _structured_rdb_graph(structured, keywords=keywords)
        strategy_inference = {"insights": [], "warnings": []}
        graph_answer = structured["summary"]
    else:
        kg_result = search_graph_knowledge(
            keywords,
            limit=graph_limit,
            season_scoped=season_scoped,
            roster_scope=rdb_result.get("scope"),
            question=question,
            qtype=qtype,
        )
        if not kg_result["graph"]["nodes"]:
            rdb_names = _entities_from_rdb(rdb_result)
            if rdb_names:
                print("DEBUG KG re-search with RDB entities:", rdb_names)
                kg_result = search_graph_knowledge(
                    rdb_names,
                    limit=graph_limit,
                    season_scoped=season_scoped,
                    roster_scope=rdb_result.get("scope"),
                    question=question,
                    qtype=qtype,
                )
        if _is_champion_usage_question(question):
            usage_rows = rdb_result.get("champion_usage") or []
            player_name = (
                usage_rows[0].get("player_name") if usage_rows
                else next(iter(rdb_result.get("entities", {}).get("players", [])), "")
            )
            kg_result = apply_rdb_champion_usage(kg_result, player_name, usage_rows)
        strategy_inference = {"insights": [], "warnings": []}
        if _is_strategy_question(question):
            strategy_inference = call_hyperclova_strategy_insights(
                question, analysis_article_evidence, kg_result.get("graph") or {}
            )
            kg_result = apply_strategy_insights(
                kg_result, strategy_inference.get("insights") or []
            )
        graph_answer = call_hyperclova_kg_answer(question, kg_result["context"])

    # 5) 전체 검증 근거로 상세 해설을 먼저 생성하고, 별도 LLM이 그 상세 해설만
    # 압축해 채팅용 요약을 만든다. 요약이 근거와 다른 결론으로 새지 않게 한다.
    evidence_bundle = _build_evidence_bundle(
        question,
        analysis_article_evidence,
        kg_result,
        rdb_result,
        season_scoped,
        qtype,
    )
    if structured:
        presentation = {
            "summary": structured["summary"],
            "detail": structured["detail"],
            "sources_used": ["rdb"],
            "warnings": [],
        }
    else:
        presentation = call_hyperclova_presentation(
            question,
            evidence_bundle,
            qtype=qtype,
            fallback_answers={
                "article": article_answer,
                "graph": graph_answer,
                "rdb": rdb_answer,
            },
        )
    final_answer = presentation["summary"]
    detailed_answer = presentation["detail"]

    # 상세 해설과 요약을 만든 뒤에만 화면 그래프를 다시 고른다. 따라서 전체
    # evidence_bundle을 사용한 답변 내용은 바뀌지 않고, 표시 노드/링크만 답변에
    # 실제로 언급된 근거 중심으로 줄어든다.
    strategy_display_request = (
        resolve_strategy_display_request(question)
        if _is_strategy_question(question) else None
    )
    focus_terms = [question]
    focus_terms.extend(keywords if isinstance(keywords, list) else [keywords])
    focus_terms.extend(article_entity_names)
    if structured:
        # 결정적으로 만든 정형 그래프는 답변 본문에 모든 노드명이 없어도 그대로 표시한다.
        display_graph = kg_result.get("analysis_graph") or kg_result.get("graph") or {
            "nodes": [], "links": []
        }
        kg_result = {
            **kg_result,
            "graph": display_graph,
            "display_graph_meta": {
                "node_count": len(display_graph.get("nodes") or []),
                "link_count": len(display_graph.get("links") or []),
                "candidate_node_count": len(display_graph.get("nodes") or []),
                "candidate_link_count": len(display_graph.get("links") or []),
                "strategy": {},
            },
        }
    else:
        kg_result = prioritize_answer_graph(
            kg_result,
            final_answer,
            detailed_answer,
            focus_terms=focus_terms,
            strategy_request=strategy_display_request,
        )
    print("DEBUG display graph meta:", kg_result.get("display_graph_meta"))

    return {
        "question": question,
        "intent": intent_result.get("intent", ""),
        "qtype": qtype,
        "season_scoped": season_scoped,
        "keywords": keywords,
        "answer": final_answer,
        "summary_answer": final_answer,
        "detailed_answer": detailed_answer,
        "article_answer": article_answer,
        "graph_answer": graph_answer,
        "rdb_answer": rdb_answer,
        "article_chunks": docs,
        "article_evidence": article_evidence,
        "analysis_article_count": len(analysis_article_evidence),
        "graph_facts": kg_result["facts"],
        "graph_context": kg_result["context"],
        "kg_keywords": kg_result["keywords"],
        "graph": kg_result["graph"],
        "display_graph_meta": kg_result.get("display_graph_meta", {}),
        "rdb_facts": rdb_result["facts"],
        "rdb_context": rdb_result["context"],
        "rdb_entities": rdb_result["entities"],
        "rdb_entity_ids": rdb_result.get("entity_ids", {}),
        "rdb_scope": rdb_result.get("scope", {}),
        "rdb_evidence": rdb_result.get("evidence", []),
        "rdb_total_facts": rdb_result.get("total_facts", len(rdb_result["facts"])),
        "rdb_truncated": rdb_result.get("truncated", False),
        "rdb_sql": rdb_result["sql"],
        "evidence_bundle": evidence_bundle,
        "sources_used": presentation.get("sources_used", []),
        "warnings": [
            *strategy_inference.get("warnings", []),
            *presentation.get("warnings", []),
        ],
        "strategy_insights": strategy_inference.get("insights", []),
    }


if __name__ == "__main__":
    result = run_pipeline("젠지 우승 전략을 알려줘")
    print("\n=== 최종 답변 ===")
    print(result["answer"])
