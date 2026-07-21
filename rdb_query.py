"""
RDB(espo) 기반 정형 데이터 검색 - 하이브리드 방식.

흐름:
  키워드
   └→ 1) 엔티티 확정: player/team + 별칭(playeralias/teamalias) 매칭
        ├→ 2) 정형 질문: 미리 정의한 고정 통계 쿼리 실행 (안전·결정적)
        └→ 3) 그 외/집계 질문: HyperCLOVA Text-to-SQL 폴백 (SELECT 전용 검증)
             └→ facts(사실 문자열) + context(LLM 답변용 텍스트) 반환

정형 데이터는 그래프/기사가 답하기 어려운 수치·집계 질문
(평균 KDA, 팀 전적, 최다 킬 등)의 근거가 된다.
"""

import calendar
import re
from datetime import date, timedelta

from mysql_connect import get_connection


# =========================
# 키워드 정리
# =========================

# 엔티티(선수/팀)명이 아닌 통계·질의 어휘. LIKE 부분매칭 노이즈를 막는다.
# (예: 'KDA' 가 'deokdam' 에, '전적' 이 엉뚱한 이름에 substring 매칭되는 것 방지)
_RESOLVE_STOPWORDS = {
    "평균", "kda", "전적", "우승", "경기", "게임", "통계", "선수", "정보", "기록",
    "성적", "승률", "순위", "랭킹", "스탯", "최다", "가장", "비교", "관련",
    "챔피언", "대회", "리그", "포지션", "알려줘", "보여줘", "분석",
    "매치", "대진", "로스터", "라인업", "멤버", "참가", "참가 팀", "소속",
}


def _as_keyword_list(keywords):
    if isinstance(keywords, str):
        items = [keywords]
    elif isinstance(keywords, list):
        items = keywords
    else:
        items = []

    # 구절 키워드("T1 전적")는 팀/선수명("T1")과 LIKE 매칭이 안 되므로
    # 구절 자체 + 공백 단위 토큰을 모두 후보로 풀어준다.
    expanded = []
    for kw in items:
        kw = str(kw).strip()
        if (
            len(kw) >= 2
            and kw.lower() not in _RESOLVE_STOPWORDS
            and not re.fullmatch(r"(?:20\d{2}년|(?:0?[1-9]|1[0-2])월|(?:0?[1-9]|[12]\d|3[01])일)", kw)
        ):
            expanded.append(kw)
        for tok in kw.split():
            tok = tok.strip()
            if (
                len(tok) >= 2
                and tok.lower() not in _RESOLVE_STOPWORDS
                and not re.fullmatch(r"(?:20\d{2}년|(?:0?[1-9]|1[0-2])월|(?:0?[1-9]|[12]\d|3[01])일)", tok)
            ):
                expanded.append(tok)

    # 중복 제거(순서 유지)
    return list(dict.fromkeys(expanded))


# =========================
# 질문 기간 해석
# =========================

def _extract_period(question):
    """질문의 ``2025년``/``5월``/``21일``을 검증된 날짜 범위로 추출한다."""
    text = str(question or "")
    year_match = re.search(r"(?<!\d)(20\d{2})\s*년", text)
    month_match = re.search(r"(?<!\d)(1[0-2]|0?[1-9])\s*월", text)
    day_match = re.search(r"(?<!\d)(3[01]|[12]?\d)\s*일", text)
    year = int(year_match.group(1)) if year_match else None
    month = int(month_match.group(1)) if month_match else None
    day = int(day_match.group(1)) if day_match and month else None
    if day is not None and year is not None:
        try:
            date(year, month, day)
        except ValueError:
            day = None
    return {
        "year": year,
        "month": month,
        "day": day,
        "explicit_year": bool(year_match),
        "explicit_month": bool(month_match),
        "explicit_day": bool(day_match and month),
        "inferred_year": False,
    }


def _resolve_period_year(cur, period):
    """월만 지정되면 데이터에 존재하는 최신 연도를 붙여 월간 범위를 확정한다."""
    resolved = dict(period or {})
    if resolved.get("month") and not resolved.get("year"):
        cur.execute("SELECT MAX(YEAR(match_date)) AS year FROM `match` WHERE match_date IS NOT NULL")
        row = cur.fetchone() or {}
        if row.get("year"):
            resolved["year"] = int(row["year"])
            resolved["inferred_year"] = True
    if resolved.get("year") and resolved.get("month") and resolved.get("day"):
        try:
            date(resolved["year"], resolved["month"], resolved["day"])
        except ValueError:
            resolved["day"] = None
            resolved["explicit_day"] = False
    return resolved


def _period_bounds(period):
    year = (period or {}).get("year")
    month = (period or {}).get("month")
    day = (period or {}).get("day")
    if not year:
        return None, None
    if month and day:
        exact = date(year, month, day)
        return exact, exact
    if month:
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)
    return date(year, 1, 1), date(year, 12, 31)


def _period_label(period):
    year = (period or {}).get("year")
    month = (period or {}).get("month")
    day = (period or {}).get("day")
    if year and month and day:
        return f"{year}년 {month}월 {day}일 기준"
    if year and month:
        return f"{year}년 {month}월"
    if year:
        return f"{year}년"
    return "전체 기간"


def _period_sql(column, period):
    """인덱스를 사용할 수 있는 반개구간 날짜 조건을 반환한다."""
    start_date, inclusive_end = _period_bounds(period)
    if not start_date:
        return "", []
    exclusive_end = inclusive_end + timedelta(days=1)
    return f" AND {column} >= %s AND {column} < %s", [start_date, exclusive_end]


def _id_filter_sql(column, values):
    ids = [int(value) for value in (values or [])]
    if not ids:
        return "", []
    placeholders = ", ".join(["%s"] * len(ids))
    return f" AND {column} IN ({placeholders})", ids


def _scope_prefix(period):
    label = _period_label(period)
    return "" if label == "전체 기간" else f"[{label}] "


# =========================
# 1) 엔티티 확정 (별칭 해소)
# =========================

def _best_entity_matches(found):
    """정확한 이름/별칭(등급 0~1)이 있으면 부분 일치 후보를 버린다."""
    rows = list(found.values())
    if not rows:
        return []
    exact = [row for row in rows if row.get("match_rank", 99) <= 1]
    if exact:
        return exact
    best_rank = min(row.get("match_rank", 99) for row in rows)
    return [row for row in rows if row.get("match_rank", 99) == best_rank]


def _resolve_players(cur, keywords):
    found = {}
    for kw in keywords:
        like = f"%{kw}%"
        starts = f"{kw}%"
        cur.execute(
            """
            SELECT p.player_id, p.player_name, p.position,
                   MIN(CASE
                     WHEN LOWER(p.player_name) = LOWER(%s) THEN 0
                     WHEN LOWER(pa.alias_name) = LOWER(%s) THEN 1
                     WHEN LOWER(p.real_name) = LOWER(%s) THEN 1
                     WHEN p.player_name LIKE %s OR pa.alias_name LIKE %s THEN 2
                     ELSE 3
                   END) AS match_rank
            FROM player p
            LEFT JOIN playeralias pa ON pa.player_id = p.player_id
            WHERE p.player_name LIKE %s
               OR p.real_name LIKE %s
               OR pa.alias_name LIKE %s
            GROUP BY p.player_id, p.player_name, p.position
            ORDER BY match_rank, CHAR_LENGTH(p.player_name)
            LIMIT 10
            """,
            (kw, kw, kw, starts, starts, like, like, like),
        )
        for row in cur.fetchall():
            current = found.get(row["player_id"])
            if current is None or row["match_rank"] < current["match_rank"]:
                found[row["player_id"]] = row
    return _best_entity_matches(found)


def _resolve_teams(cur, keywords):
    found = {}
    for kw in keywords:
        like = f"%{kw}%"
        starts = f"{kw}%"
        cur.execute(
            """
            SELECT t.team_id, t.team_name,
                   MIN(CASE
                     WHEN LOWER(t.team_name) = LOWER(%s) THEN 0
                     WHEN LOWER(ta.alias_name) = LOWER(%s) THEN 1
                     WHEN t.team_name LIKE %s OR ta.alias_name LIKE %s THEN 2
                     ELSE 3
                   END) AS match_rank
            FROM team t
            LEFT JOIN teamalias ta ON ta.team_id = t.team_id
            WHERE t.team_name LIKE %s
               OR ta.alias_name LIKE %s
            GROUP BY t.team_id, t.team_name
            ORDER BY match_rank, CHAR_LENGTH(t.team_name)
            LIMIT 10
            """,
            (kw, kw, starts, starts, like, like),
        )
        for row in cur.fetchall():
            current = found.get(row["team_id"])
            if current is None or row["match_rank"] < current["match_rank"]:
                found[row["team_id"]] = row
    return _best_entity_matches(found)


# =========================
# 2) 고정 통계 쿼리 (정형 질문)
# =========================

def _player_overall_facts(cur, player, period=None, team_ids=None):
    period_sql, period_params = _period_sql("m.match_date", period)
    team_sql, team_params = _id_filter_sql("s.team_id", team_ids)
    sql = """
        SELECT COUNT(*) AS games,
               ROUND(AVG(kills), 1)   AS avg_k,
               ROUND(AVG(deaths), 1)  AS avg_d,
               ROUND(AVG(assists), 1) AS avg_a,
               ROUND(AVG((kills + assists) / NULLIF(deaths, 0)), 2) AS kda,
               ROUND(AVG(cs), 0)   AS avg_cs,
               ROUND(AVG(gold), 0) AS avg_gold
        FROM playergamestat s
        JOIN game g ON g.game_id = s.game_id
        JOIN `match` m ON m.match_id = g.match_id
        WHERE s.player_id = %s
    """ + team_sql + period_sql
    cur.execute(sql, (player["player_id"], *team_params, *period_params))
    row = cur.fetchone()
    if not row or not row["games"]:
        if (period or {}).get("year"):
            return [
                f"(Player) {_scope_prefix(period)}{player['player_name']} — 조건에 맞는 게임 0건"
            ]
        return []

    name = player["player_name"]
    pos = player.get("position") or "-"
    return [
        f"(Player) {_scope_prefix(period)}{name}[{pos}] — {row['games']}게임, 평균 KDA {row['kda']} "
        f"(K {row['avg_k']} / D {row['avg_d']} / A {row['avg_a']}), "
        f"평균 CS {row['avg_cs']}, 평균 골드 {row['avg_gold']}"
    ]


# =========================
# 경기/매치 표시 유틸
# =========================
# match/game 을 사람이 읽는 'M월 D일 팀A vs 팀B' 형태로 라벨링한다.
# match_date 는 datetime 으로 오지만, 문자열로 와도 안전하게 처리한다.

def _fmt_date(dt):
    """datetime/date/문자열 → 'M월 D일'. 값이 없으면 ''."""
    if not dt:
        return ""
    month = getattr(dt, "month", None)
    day = getattr(dt, "day", None)
    if month and day:
        return f"{month}월 {day}일"
    m = re.match(r"\d{4}-(\d{2})-(\d{2})", str(dt))
    if not m:
        return ""
    return f"{int(m.group(1))}월 {int(m.group(2))}일"


def _match_headline(date, team_a, team_b):
    """'M월 D일 팀A vs 팀B' (날짜가 없으면 대진만)."""
    matchup = f"{team_a or '?'} vs {team_b or '?'}"
    return f"{date} {matchup}".strip()


def _player_period_games_facts(cur, player, period=None, team_ids=None):
    """질문 기간에 속한 선수의 모든 게임을 반환한다.

    전체 행은 상세 분석에 사용하고 최종 요약은 상세 해설에서 별도로 압축한다.
    """
    period_sql, period_params = _period_sql("m.match_date", period)
    team_sql, team_params = _id_filter_sql("s.team_id", team_ids)
    sql = """
        SELECT c.champion_name, s.kills, s.deaths, s.assists, s.team_id,
               g.winner_team_id, g.blue_team_id, g.red_team_id, g.game_no,
               tb.team_name AS blue_name, tr.team_name AS red_name,
               m.match_date
        FROM playergamestat s
        JOIN game g ON g.game_id = s.game_id
        LEFT JOIN `match` m ON m.match_id = g.match_id
        LEFT JOIN team tb ON tb.team_id = g.blue_team_id
        LEFT JOIN team tr ON tr.team_id = g.red_team_id
        LEFT JOIN champion c ON c.champion_id = s.champion_id
        WHERE s.player_id = %s
    """ + team_sql + period_sql + """
        ORDER BY m.match_date DESC, g.game_no DESC
    """
    cur.execute(
        sql,
        (player["player_id"], *team_params, *period_params),
    )
    facts = []
    name = player["player_name"]
    for row in cur.fetchall():
        champ = row["champion_name"] or "?"
        result = "승" if row["winner_team_id"] == row["team_id"] else "패"
        # blue/red 중 내 팀이 아닌 쪽이 상대 팀
        if row["team_id"] == row["blue_team_id"]:
            my_team, opp_team = row["blue_name"], row["red_name"]
        else:
            my_team, opp_team = row["red_name"], row["blue_name"]
        headline = _match_headline(_fmt_date(row["match_date"]), my_team, opp_team)
        set_no = f" {row['game_no']}세트" if row["game_no"] else ""
        facts.append(
            f"(Game) {headline}{set_no} — {name} {champ} "
            f"{row['kills']}/{row['deaths']}/{row['assists']} {result}"
        )
    return facts


def _player_champion_usage_rows(cur, player, period=None, team_ids=None):
    """선수의 요청 기간 챔피언별 사용량을 구조화된 행으로 반환한다."""
    period_sql, period_params = _period_sql("m.match_date", period)
    team_sql, team_params = _id_filter_sql("s.team_id", team_ids)
    sql = """
        SELECT c.champion_name,
               COUNT(*) AS games,
               SUM(CASE WHEN s.team_id = g.winner_team_id THEN 1 ELSE 0 END) AS wins,
               MIN(m.match_date) AS first_played,
               MAX(m.match_date) AS last_played
        FROM playergamestat s
        JOIN game g ON g.game_id = s.game_id
        JOIN `match` m ON m.match_id = g.match_id
        JOIN champion c ON c.champion_id = s.champion_id
        WHERE s.player_id = %s
    """ + team_sql + period_sql + """
        GROUP BY c.champion_id, c.champion_name
        ORDER BY games DESC, c.champion_name
    """
    cur.execute(sql, (player["player_id"], *team_params, *period_params))
    rows = list(cur.fetchall())
    for row in rows:
        row["player_name"] = player["player_name"]
    return rows


def _player_champion_usage_facts(cur, player, period=None, team_ids=None):
    """선수의 요청 기간 전체 경기에서 챔피언별 사용량을 집계한다."""
    rows = _player_champion_usage_rows(cur, player, period, team_ids)
    if not rows:
        return []

    total_games = sum(int(row.get("games") or 0) for row in rows)
    champion_summary = "·".join(
        f"{row['champion_name']} {row['games']}회"
        for row in rows
    )
    facts = [
        f"(ChampionPool) {_scope_prefix(period)}{player['player_name']}는 전체 {total_games}경기에서 "
        f"{len(rows)}종 챔피언 사용 — {champion_summary}"
    ]
    facts.extend(
        f"(ChampionUsage) {_scope_prefix(period)}{player['player_name']} — "
        f"{row['champion_name']} {row['games']}회 사용, {row['wins']}승"
        for row in rows
    )
    return facts


def _player_monthly_facts(cur, player, team_ids=None):
    """선수의 월별 성적을 전 기간에서 집계해 달 비교 질문의 근거로 쓴다."""
    team_sql, team_params = _id_filter_sql("s.team_id", team_ids)
    sql = """
        SELECT YEAR(m.match_date) AS season_year,
               MONTH(m.match_date) AS season_month,
               COUNT(*) AS games,
               SUM(CASE WHEN s.team_id = g.winner_team_id THEN 1 ELSE 0 END) AS wins,
               ROUND(100 * SUM(CASE WHEN s.team_id = g.winner_team_id THEN 1 ELSE 0 END)
                     / NULLIF(COUNT(*), 0), 1) AS win_rate,
               ROUND(AVG(s.kills), 1) AS avg_k,
               ROUND(AVG(s.deaths), 1) AS avg_d,
               ROUND(AVG(s.assists), 1) AS avg_a,
               ROUND(AVG((s.kills + s.assists) / NULLIF(s.deaths, 0)), 2) AS kda
        FROM playergamestat s
        JOIN game g ON g.game_id = s.game_id
        JOIN `match` m ON m.match_id = g.match_id
        WHERE s.player_id = %s
    """ + team_sql + """
        GROUP BY YEAR(m.match_date), MONTH(m.match_date)
        ORDER BY season_year, season_month
    """
    cur.execute(sql, (player["player_id"], *team_params))
    rows = list(cur.fetchall())
    if not rows:
        return []

    facts = []
    for row in rows:
        losses = int(row["games"] or 0) - int(row["wins"] or 0)
        facts.append(
            f"(월별 Player) {row['season_year']}년 {row['season_month']}월 "
            f"{player['player_name']} — {row['games']}게임 {row['wins']}승 {losses}패, "
            f"승률 {row['win_rate']}%, 평균 {row['avg_k']}/{row['avg_d']}/{row['avg_a']}, "
            f"KDA {row['kda']}"
        )

    eligible = [row for row in rows if int(row["games"] or 0) >= 3] or rows
    best = max(
        eligible,
        key=lambda row: (
            float(row["win_rate"] or 0),
            float(row["kda"] or 0),
            int(row["games"] or 0),
        ),
    )
    facts.insert(
        0,
        f"(월별 비교 기준) 표본 3게임 이상인 달을 승률→KDA→경기 수 순으로 비교하면 "
        f"{player['player_name']}의 1순위 후보는 {best['season_year']}년 {best['season_month']}월"
    )
    return facts


def _team_record_facts(cur, team, period=None):
    period_sql, period_params = _period_sql("m.match_date", period)
    sql = """
        SELECT COUNT(*) AS matches,
               SUM(CASE WHEN m.winner_team_id = %s THEN 1 ELSE 0 END) AS wins
        FROM `match` m
        WHERE (m.team_a_id = %s OR m.team_b_id = %s)
    """ + period_sql
    cur.execute(
        sql,
        (team["team_id"], team["team_id"], team["team_id"], *period_params),
    )
    row = cur.fetchone()
    if not row or not row["matches"]:
        if (period or {}).get("year"):
            return [
                f"(Team) {_scope_prefix(period)}{team['team_name']} — 조건에 맞는 매치 0건"
            ]
        return []

    wins = int(row["wins"] or 0)
    losses = int(row["matches"]) - wins
    win_rate = round(100 * wins / int(row["matches"]), 1)
    return [
        f"(Team) {_scope_prefix(period)}{team['team_name']} — "
        f"매치 {row['matches']}전 {wins}승 {losses}패, 승률 {win_rate}%"
    ]


def _latest_team_match_date(cur, team_id):
    cur.execute(
        """
        SELECT DATE(MAX(match_date)) AS latest_date
        FROM `match`
        WHERE team_a_id = %s OR team_b_id = %s
        """,
        (team_id, team_id),
    )
    row = cur.fetchone() or {}
    return row.get("latest_date")


def _team_roster_facts(cur, team, period=None):
    start_date, end_date = _period_bounds(period)
    if not start_date:
        cur.execute(
            """
            SELECT tr.player_id, p.player_name,
                   COALESCE(MAX(tr.role), p.position) AS position,
                   MIN(tr.start_date) AS start_date,
                   MAX(tr.end_date) AS end_date
            FROM teamroster tr
            JOIN player p ON p.player_id = tr.player_id
            WHERE tr.team_id = %s
            GROUP BY tr.player_id, p.player_name, p.position
            ORDER BY FIELD(COALESCE(MAX(tr.role), p.position), 'top', 'jng', 'mid', 'bot', 'sup'),
                     MIN(tr.start_date), p.player_name
            """,
            (team["team_id"],),
        )
        members = [
            f"{row['player_name']}({row['position'] or '-'}, player_id={row['player_id']})"
            for row in cur.fetchall()
        ]
        if not members:
            return []
        return [
            f"(Roster) [전체 소속 이력] {team['team_name']}(team_id={team['team_id']}) 소속 선수: "
            + ", ".join(members)
        ]

    cur.execute(
        """
        SELECT tr.player_id, p.player_name, COALESCE(tr.role, p.position) AS position,
               tr.start_date, tr.end_date
        FROM teamroster tr
        JOIN player p ON p.player_id = tr.player_id
        WHERE tr.team_id = %s
          AND tr.start_date <= %s
          AND (tr.end_date IS NULL OR tr.end_date >= %s)
        ORDER BY FIELD(COALESCE(tr.role, p.position), 'top', 'jng', 'mid', 'bot', 'sup'),
                 tr.start_date, p.player_name
        """,
        (team["team_id"], end_date, start_date),
    )
    members = [
        f"{row['player_name']}({row['position'] or '-'}, player_id={row['player_id']})"
        for row in cur.fetchall()
    ]
    if not members:
        return []
    label = _period_label(period)
    if label == "전체 기간":
        label = f"{start_date.year}년 {start_date.month}월 {start_date.day}일 기준"
    return [
        f"(Roster) [{label}] {team['team_name']}(team_id={team['team_id']}) 유효 로스터: "
        + ", ".join(members)
    ]


def _team_period_matches_facts(cur, team, period=None):
    """질문 기간에 속한 팀의 모든 Match를 반환한다."""
    period_sql, period_params = _period_sql("m.match_date", period)
    sql = """
        SELECT m.match_date, m.match_stage, m.score_a, m.score_b,
               m.winner_team_id,
               ta.team_name AS team_a_name, tb.team_name AS team_b_name
        FROM `match` m
        JOIN team ta ON ta.team_id = m.team_a_id
        JOIN team tb ON tb.team_id = m.team_b_id
        WHERE (m.team_a_id = %s OR m.team_b_id = %s)
    """ + period_sql + """
        ORDER BY m.match_date DESC
    """
    cur.execute(
        sql,
        (team["team_id"], team["team_id"], *period_params),
    )
    facts = []
    for row in cur.fetchall():
        headline = _match_headline(
            _fmt_date(row["match_date"]), row["team_a_name"], row["team_b_name"]
        )
        score = ""
        if row["score_a"] is not None and row["score_b"] is not None:
            score = f" {row['score_a']}:{row['score_b']}"
        result = ""
        if row["winner_team_id"]:
            result = " " + ("승" if row["winner_team_id"] == team["team_id"] else "패")
        stage = f" ({row['match_stage']})" if row["match_stage"] else ""
        facts.append(f"(Match) {headline} 경기{score}{result}{stage}")
    return facts


def _match_lineups_on_date(cur, period):
    """특정 하루의 Match→Team→실제 출전 선수→사용 챔피언 구조를 반환한다."""
    start_date, end_date = _period_bounds(period)
    if not start_date or start_date != end_date:
        return []
    exclusive_end = end_date + timedelta(days=1)
    cur.execute(
        """
        SELECT m.match_id, m.match_date, m.match_stage, m.score_a, m.score_b,
               m.winner_team_id, m.team_a_id, m.team_b_id,
               ta.team_name AS team_a_name, tb.team_name AS team_b_name
        FROM `match` m
        JOIN team ta ON ta.team_id = m.team_a_id
        JOIN team tb ON tb.team_id = m.team_b_id
        WHERE m.match_date >= %s AND m.match_date < %s
        ORDER BY m.match_date, m.match_id
        """,
        (start_date, exclusive_end),
    )
    match_rows = list(cur.fetchall())
    if not match_rows:
        return []

    lineups = {}
    for row in match_rows:
        match_id = int(row["match_id"])
        match_date = row["match_date"]
        date_label = (
            match_date.date().isoformat()
            if hasattr(match_date, "date")
            else str(match_date)[:10]
        )
        teams = {}
        for side in ("a", "b"):
            team_id = int(row[f"team_{side}_id"])
            teams[team_id] = {
                "team_id": team_id,
                "team_name": row[f"team_{side}_name"],
                "players": {},
            }
        lineups[match_id] = {
            "match_id": match_id,
            "match_date": date_label,
            "match_stage": row.get("match_stage"),
            "score_a": row.get("score_a"),
            "score_b": row.get("score_b"),
            "winner_team_id": row.get("winner_team_id"),
            "label": f"{date_label} {row['team_a_name']} vs {row['team_b_name']}",
            "teams": teams,
        }

    placeholders = ", ".join(["%s"] * len(lineups))
    cur.execute(
        f"""
        SELECT g.match_id, g.game_no, s.team_id, t.team_name,
               s.player_id, p.player_name,
               COALESCE(s.position, p.position) AS position,
               c.champion_name
        FROM playergamestat s
        JOIN game g ON g.game_id = s.game_id
        JOIN team t ON t.team_id = s.team_id
        JOIN player p ON p.player_id = s.player_id
        LEFT JOIN champion c ON c.champion_id = s.champion_id
        WHERE g.match_id IN ({placeholders})
        ORDER BY g.match_id, s.team_id,
                 FIELD(COALESCE(s.position, p.position), 'top', 'jng', 'mid', 'bot', 'sup'),
                 p.player_name, g.game_no
        """,
        tuple(lineups),
    )
    for row in cur.fetchall():
        match = lineups.get(int(row["match_id"]))
        if not match:
            continue
        team_id = int(row["team_id"])
        team = match["teams"].setdefault(team_id, {
            "team_id": team_id,
            "team_name": row["team_name"],
            "players": {},
        })
        player_id = int(row["player_id"])
        player = team["players"].setdefault(player_id, {
            "player_id": player_id,
            "player_name": row["player_name"],
            "position": row.get("position") or "-",
            "champions": {},
        })
        champion = row.get("champion_name")
        if champion:
            usage = player["champions"].setdefault(champion, {
                "champion_name": champion,
                "games": 0,
                "game_nos": [],
            })
            usage["games"] += 1
            if row.get("game_no") is not None:
                usage["game_nos"].append(int(row["game_no"]))

    position_order = {"top": 0, "jng": 1, "mid": 2, "bot": 3, "sup": 4}
    result = []
    for match in lineups.values():
        teams = []
        for team in match["teams"].values():
            players = []
            for player in team["players"].values():
                player["champions"] = list(player["champions"].values())
                players.append(player)
            team["players"] = sorted(
                players,
                key=lambda item: (position_order.get(item["position"], 99), item["player_name"]),
            )
            teams.append(team)
        match["teams"] = teams
        result.append(match)
    return result


def _match_lineup_facts(lineups):
    facts = []
    for match in lineups or []:
        score = ""
        if match.get("score_a") is not None and match.get("score_b") is not None:
            score = f" {match['score_a']}:{match['score_b']}"
        team_parts = []
        for team in match.get("teams") or []:
            players = []
            for player in team.get("players") or []:
                champions = "·".join(
                    usage["champion_name"] for usage in player.get("champions") or []
                ) or "?"
                players.append(
                    f"{player['player_name']}({player['position']}:{champions})"
                )
            team_parts.append(f"{team['team_name']}=" + ", ".join(players))
        facts.append(
            f"(MatchLineup) {match['label']}{score} — " + " | ".join(team_parts)
        )
    return facts


def _team_monthly_facts(cur, team):
    cur.execute(
        """
        SELECT YEAR(m.match_date) AS season_year,
               MONTH(m.match_date) AS season_month,
               COUNT(*) AS matches,
               SUM(CASE WHEN m.winner_team_id = %s THEN 1 ELSE 0 END) AS wins,
               ROUND(100 * SUM(CASE WHEN m.winner_team_id = %s THEN 1 ELSE 0 END)
                     / NULLIF(COUNT(*), 0), 1) AS win_rate
        FROM `match` m
        WHERE m.team_a_id = %s OR m.team_b_id = %s
        GROUP BY YEAR(m.match_date), MONTH(m.match_date)
        ORDER BY season_year, season_month
        """,
        (team["team_id"], team["team_id"], team["team_id"], team["team_id"]),
    )
    rows = list(cur.fetchall())
    if not rows:
        return []
    facts = []
    for row in rows:
        losses = int(row["matches"] or 0) - int(row["wins"] or 0)
        facts.append(
            f"(월별 Team) {row['season_year']}년 {row['season_month']}월 {team['team_name']} — "
            f"{row['matches']}전 {row['wins']}승 {losses}패, 승률 {row['win_rate']}%"
        )
    eligible = [row for row in rows if int(row["matches"] or 0) >= 2] or rows
    best = max(
        eligible,
        key=lambda row: (float(row["win_rate"] or 0), int(row["matches"] or 0)),
    )
    facts.insert(
        0,
        f"(월별 비교 기준) 표본 2매치 이상인 달을 승률→매치 수 순으로 비교하면 "
        f"{team['team_name']}의 1순위 후보는 {best['season_year']}년 {best['season_month']}월"
    )
    return facts


def _team_strategy_facts(cur, team, period=None):
    """
    전략/운영 질문용 정형 '전략지표'.
    오브젝트 선취율·경기당 오브젝트·15분 팀 골드 주도권을 집계한다.
    (그래프/기사 서사와 묶여 '왜 이겼나'의 수치 근거가 된다.)
    """
    tid = team["team_id"]

    # 팀 게임 단위 오브젝트/선취 집계 (teamgamestat)
    period_sql, period_params = _period_sql("m.match_date", period)
    sql = """
        SELECT COUNT(*) AS games,
               ROUND(AVG(dragons), 1) AS avg_dragon,
               ROUND(AVG(barons), 1)  AS avg_baron,
               ROUND(AVG(heralds), 1) AS avg_herald,
               ROUND(AVG(towers), 1)  AS avg_tower,
               ROUND(100 * AVG(first_dragon), 0) AS first_dragon_pct,
               ROUND(100 * AVG(first_baron), 0)  AS first_baron_pct,
               ROUND(100 * AVG(first_tower), 0)  AS first_tower_pct
        FROM teamgamestat tgs
        JOIN game g ON g.game_id = tgs.game_id
        JOIN `match` m ON m.match_id = g.match_id
        WHERE tgs.team_id = %s
    """ + period_sql
    cur.execute(sql, (tid, *period_params))
    row = cur.fetchone()
    if not row or not row["games"]:
        return []

    # 15분 팀 골드격차: 게임별 5인 gold_diff_at_15 합의 평균 (playergamestat)
    gold_period_sql, gold_period_params = _period_sql("m.match_date", period)
    gold_sql = """
        SELECT ROUND(AVG(team_diff), 0) AS avg_gold_diff_15
        FROM (
            SELECT s.game_id, SUM(s.gold_diff_at_15) AS team_diff
            FROM playergamestat s
            JOIN game g ON g.game_id = s.game_id
            JOIN `match` m ON m.match_id = g.match_id
            WHERE s.team_id = %s AND s.gold_diff_at_15 IS NOT NULL
    """ + gold_period_sql + """
            GROUP BY s.game_id
        ) t
    """
    cur.execute(gold_sql, (tid, *gold_period_params))
    gold_row = cur.fetchone()
    gold_diff = gold_row["avg_gold_diff_15"] if gold_row else None

    parts = []
    if gold_diff is not None:
        sign = "+" if gold_diff >= 0 else ""
        parts.append(f"15분 팀 골드격차 평균 {sign}{int(gold_diff)}")
    parts.append(
        f"경기당 드래곤 {row['avg_dragon']}/바론 {row['avg_baron']}/전령 {row['avg_herald']}"
    )
    parts.append(
        f"선취율 첫용 {row['first_dragon_pct']}% · 첫바론 {row['first_baron_pct']}% "
        f"· 첫타워 {row['first_tower_pct']}%"
    )
    return [
        f"(전략지표) {_scope_prefix(period)}{team['team_name']}[{row['games']}경기] — "
        + ", ".join(parts)
    ]


def _looks_month_comparison(question):
    q = str(question or "").lower()
    has_month_unit = any(token in q for token in ("월별", "월간", "달", "개월"))
    has_comparison = any(
        token in q
        for token in ("가장", "제일", "최고", "좋았", "나빴", "월별", "비교", "추이")
    )
    return has_month_unit and has_comparison


def _player_team_membership_facts(cur, players, teams, period=None):
    facts = []
    for team in teams:
        start_date, end_date = _period_bounds(period)
        if not start_date:
            start_date = end_date = _latest_team_match_date(cur, team["team_id"])
        if not start_date:
            continue
        for player in players:
            cur.execute(
                """
                SELECT COUNT(*) AS matches
                FROM teamroster
                WHERE team_id = %s AND player_id = %s
                  AND start_date <= %s
                  AND (end_date IS NULL OR end_date >= %s)
                """,
                (team["team_id"], player["player_id"], end_date, start_date),
            )
            row = cur.fetchone() or {}
            label = _period_label(period)
            if label == "전체 기간":
                label = f"{start_date.year}년 {start_date.month}월 {start_date.day}일 기준"
            if int(row.get("matches") or 0) > 0:
                facts.append(
                    f"(Roster 검증) [{label}] {player['player_name']}(player_id={player['player_id']})는 "
                    f"{team['team_name']}(team_id={team['team_id']}) 로스터와 일치"
                )
            else:
                facts.append(
                    f"(Roster 검증) [{label}] {player['player_name']}(player_id={player['player_id']})와 "
                    f"{team['team_name']}(team_id={team['team_id']})의 유효 로스터 매칭 없음"
                )
    return facts


def _run_templates(cur, players, teams, question, period=None):
    facts = []
    team_ids = [team["team_id"] for team in teams]
    if _looks_month_comparison(question):
        for player in players:
            facts += _player_monthly_facts(cur, player, team_ids=team_ids)
        for team in teams:
            facts += _team_monthly_facts(cur, team)
        facts += _player_team_membership_facts(cur, players, teams, period)
        return facts

    # 정형 질문은 요청한 종류의 fact만 만든다. 예전에는 KDA 하나를 물어도 선수의
    # 전체 경기 121건, 로스터 하나를 물어도 팀의 전체 매치가 함께 들어갔다.
    special_kinds = {
        "champion_usage": _looks_player_champion_usage(question),
        "player_kda": _looks_player_kda_question(question),
        "roster": _looks_roster_question(question),
        "team_record": _looks_team_record_question(question),
    }
    single_special = [kind for kind, enabled in special_kinds.items() if enabled]

    if single_special == ["champion_usage"]:
        for player in players:
            facts += _player_champion_usage_facts(
                cur, player, period, team_ids=team_ids
            )
        return facts

    if single_special == ["player_kda"]:
        for player in players:
            facts += _player_overall_facts(cur, player, period, team_ids=team_ids)
        return facts

    if single_special == ["roster"]:
        for team in teams:
            facts += _team_roster_facts(cur, team, period)
        return facts

    if single_special == ["team_record"]:
        for team in teams:
            facts += _team_record_facts(cur, team, period)
        return facts

    for player in players:
        facts += _player_overall_facts(cur, player, period, team_ids=team_ids)
        if _looks_player_champion_usage(question):
            facts += _player_champion_usage_facts(cur, player, period, team_ids=team_ids)
        else:
            facts += _player_period_games_facts(cur, player, period, team_ids=team_ids)
    for team in teams:
        facts += _team_record_facts(cur, team, period)
        facts += _team_roster_facts(cur, team, period)
        facts += _team_period_matches_facts(cur, team, period)
    facts += _player_team_membership_facts(cur, players, teams, period)
    return facts


# =========================
# 3) Text-to-SQL 폴백
# =========================

RDB_SCHEMA_PROMPT = """
[espo 데이터베이스 스키마] (MySQL, LCK e스포츠)

league(league_id, league_name, region, description)
tournament(tournament_id, league_id, tournament_name, season_year, split, start_date, end_date)
  - split 예: 'Cup','Rounds 1-2','Rounds 3-5'
team(team_id, team_name, region, founded_year, description, is_active)
teamalias(team_alias_id, team_id, alias_name, alias_type)  -- 팀 별칭
player(player_id, player_name, real_name, position, nationality, birth_date, is_active)
  - position 예: 'top','jng','mid','bot','sup'
playeralias(player_alias_id, player_id, alias_name, alias_type)  -- 선수 별칭
teamroster(roster_id, team_id, player_id, start_date, end_date, role, is_current)
  -- 특정 날짜의 로스터는 start_date <= 날짜 AND (end_date IS NULL OR end_date >= 날짜)
  -- is_current 값만 믿지 말고 반드시 start_date/end_date 범위를 사용할 것
champion(champion_id, champion_name, role_type, description)
`match`(match_id, tournament_id, match_date, team_a_id, team_b_id, match_format,
        match_stage, winner_team_id, score_a, score_b)
  -- 주의: match 는 예약어이므로 반드시 백틱 `match` 로 표기
  -- team_a_id/team_b_id = 참가팀, winner_team_id = 승리팀, score_a/score_b = 세트 스코어
game(game_id, match_id, game_no, blue_team_id, red_team_id, winner_team_id,
     duration_seconds, external_game_id, patch, url)
playergamestat(player_game_stat_id, game_id, player_id, team_id, champion_id,
     kills, deaths, assists, cs, gold, side, position,
     deaths_at_15, gold_at_15, xp_at_15, cs_at_15, gold_diff_at_15,
     xp_diff_at_15, cs_diff_at_15, damage_to_champions, vision_score,
     wards_placed, wards_killed)
  -- 게임당 선수 1행. KDA = (kills + assists) / deaths
teamgamestat(team_game_stat_id, game_id, team_id, total_kills, total_deaths,
     dragons, barons, towers, total_gold, void_grubs, atakhans, heralds,
     inhibitors, first_dragon, first_baron, first_tower)

[규칙]
- 선수/팀 이름은 player.player_name, team.team_name 뿐 아니라
  playeralias.alias_name, teamalias.alias_name 으로도 검색할 것.
- team.region 은 'Korea' 같은 국가명이다. 'LCK' 같은 리그명이 아니므로
  팀을 region 으로 거르지 마라. 팀은 team_name/teamalias 로만 식별하라.
- [질문에서 확인된 엔티티]로 team_id/player_id 가 주어지면 그 id 를 그대로 써라.
- 질문에 연도·월 범위가 있으면 모든 match/game/player/team 통계에 같은 날짜 범위를 적용하라.
- 월 조건은 MONTH()만 쓰기보다 반개구간으로 작성하라. 예: 2025년 5월은
  m.match_date >= '2025-05-01' AND m.match_date < '2025-06-01'.
- playergamestat/teamgamestat의 날짜 필터는 game→`match`로 조인해 m.match_date에 걸어라.
- 요청한 월에 결과가 없으면 다른 달의 최신 행으로 대신하지 마라.
- deaths 가 0일 수 있으니 KDA 계산 시 NULLIF(deaths,0) 사용.
- "N번째로 높은/낮은" 은 ORDER BY 뒤에 LIMIT 1 OFFSET (N-1). "상위 N개" 는 LIMIT N.
- 반드시 단일 SELECT 문 하나만, MySQL 문법으로 작성.

[까다로운 질문 패턴 — 아래 정답 SQL을 그대로 참고해 작성하라]

# 패턴1) 팀 승률/순위 ("승률 가장 높은/낮은 팀", "팀 승률 랭킹")
#   포인트: 한 팀은 team_a_id 또는 team_b_id 어느 쪽에도 올 수 있다.
#   반드시 t.team_id IN (m.team_a_id, m.team_b_id) 로 참가경기를 세야 한다. (한쪽만 세면 틀림)
SELECT t.team_name,
       ROUND(100*SUM(CASE WHEN m.winner_team_id = t.team_id THEN 1 ELSE 0 END)/COUNT(*),1) AS win_rate,
       COUNT(*) AS matches
FROM team t
JOIN `match` m ON t.team_id IN (m.team_a_id, m.team_b_id)
GROUP BY t.team_id, t.team_name
HAVING matches >= 5
ORDER BY win_rate DESC;   -- 가장 낮은 팀이면 ORDER BY win_rate ASC

# 패턴2) 두 팀 상대전적 ("젠지와 T1 상대전적")
#   포인트: 두 팀의 (team_a, team_b) 순서가 반대일 수 있으므로 (A,B) OR (B,A) 를 모두 포함하고,
#   승수는 winner_team_id 를 팀 이름(wt)으로 이어 센다.
SELECT SUM(CASE WHEN wt.team_name='젠지' THEN 1 ELSE 0 END) AS a_wins,
       SUM(CASE WHEN wt.team_name='티원' THEN 1 ELSE 0 END) AS b_wins,
       COUNT(*) AS games
FROM `match` m
JOIN team ta ON ta.team_id = m.team_a_id
JOIN team tb ON tb.team_id = m.team_b_id
LEFT JOIN team wt ON wt.team_id = m.winner_team_id
WHERE (ta.team_name='젠지' AND tb.team_name='티원')
   OR (ta.team_name='티원' AND tb.team_name='젠지');

# 패턴3) 선수 승률 ("승률 가장 높은 선수")
#   포인트: 선수의 승리 = 그 선수의 team_id 가 game.winner_team_id 와 같은 경우.
SELECT p.player_name,
       ROUND(100*SUM(CASE WHEN s.team_id = g.winner_team_id THEN 1 ELSE 0 END)/COUNT(*),1) AS win_rate,
       COUNT(*) AS games
FROM playergamestat s
JOIN game g ON g.game_id = s.game_id
JOIN player p ON p.player_id = s.player_id
GROUP BY p.player_id, p.player_name
HAVING games >= 10
ORDER BY win_rate DESC;

# 패턴4) 오브젝트 선취 → 승률 ("첫 바론 먹으면 승률 얼마", first_dragon/first_tower 동일)
#   포인트: 조건부 집계. 분모(선취한 경우 수)에 NULLIF(...,0) 로 0 나눗셈 방지.
SELECT ROUND(100*
         SUM(CASE WHEN tgs.first_baron=1 AND tgs.team_id = g.winner_team_id THEN 1 ELSE 0 END)
         / NULLIF(SUM(CASE WHEN tgs.first_baron=1 THEN 1 ELSE 0 END),0),1) AS first_baron_winrate
FROM teamgamestat tgs
JOIN game g ON g.game_id = tgs.game_id;

# 패턴5) 그룹별 최상위 ("팀별 최다 픽 챔피언", "포지션별 최다 사용 챔피언")
#   포인트: '그룹마다 1위'는 ROW_NUMBER() OVER (PARTITION BY 그룹 ORDER BY 집계 DESC) 후 rn=1 로 뽑는다.
SELECT team_name, champion_name, picks FROM (
  SELECT t.team_name, c.champion_name, COUNT(*) AS picks,
         ROW_NUMBER() OVER (PARTITION BY s.team_id ORDER BY COUNT(*) DESC) AS rn
  FROM playergamestat s
  JOIN champion c ON c.champion_id = s.champion_id
  JOIN team t ON t.team_id = s.team_id
  GROUP BY s.team_id, t.team_name, c.champion_id, c.champion_name
) x
WHERE rn = 1
ORDER BY picks DESC;

# 패턴6) 특정 월의 팀 경기 ("2025년 5월 T1 경기")
#   포인트: 팀 ID와 날짜 반개구간을 함께 강제하고 최신 다른 달로 대체하지 않는다.
SELECT m.match_date, ta.team_name AS team_a, tb.team_name AS team_b,
       m.score_a, m.score_b, wt.team_name AS winner
FROM `match` m
JOIN team ta ON ta.team_id = m.team_a_id
JOIN team tb ON tb.team_id = m.team_b_id
LEFT JOIN team wt ON wt.team_id = m.winner_team_id
WHERE 10 IN (m.team_a_id, m.team_b_id)
  AND m.match_date >= '2025-05-01'
  AND m.match_date < '2025-06-01'
ORDER BY m.match_date;

# 패턴7) 특정 월의 유효 로스터 ("2025년 5월 T1 로스터")
SELECT tr.player_id, p.player_name, tr.role
FROM teamroster tr
JOIN player p ON p.player_id = tr.player_id
WHERE tr.team_id = 10
  AND tr.start_date < '2025-06-01'
  AND (tr.end_date IS NULL OR tr.end_date >= '2025-05-01')
ORDER BY tr.role;
"""

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|"
    r"into\s+outfile|load_file)\b",
    re.IGNORECASE,
)


def _sanitize_sql(raw):
    """LLM이 만든 SQL을 SELECT 전용으로 검증·정리. 부적합하면 None."""
    if not raw:
        return None

    sql = re.sub(r"```(?:sql)?", "", raw, flags=re.IGNORECASE).strip()

    # 첫 번째 문장만 사용 (다중 쿼리 차단)
    sql = sql.split(";")[0].strip()

    if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
        return None

    if _FORBIDDEN_SQL.search(sql):
        return None

    return sql


def _format_rows(rows):
    facts = []
    for row in rows:
        parts = [f"{k}={v}" for k, v in row.items() if v is not None]
        if parts:
            facts.append("(RDB) " + ", ".join(parts))
    return facts


def _text_to_sql_facts(cur, question, entity_hint, period=None):
    # 순환 import 방지를 위해 함수 안에서 import
    from clova_extract import generate_sql_with_clova

    sql = generate_sql_with_clova(question, RDB_SCHEMA_PROMPT, entity_hint)
    safe_sql = _sanitize_sql(sql)

    print("DEBUG text-to-sql raw:", repr(sql))
    print("DEBUG text-to-sql safe:", repr(safe_sql))

    if not safe_sql:
        return [], None

    # 명시된 기간을 LLM이 빠뜨린 SQL은 실행하지 않는다. 다른 달의 최신 행이 섞이는 것보다
    # 근거 없음으로 처리하는 편이 안전하다.
    if (period or {}).get("year"):
        lowered = safe_sql.lower()
        has_match_period = "match_date" in lowered
        has_roster_period = "start_date" in lowered and "end_date" in lowered
        if not (has_match_period or has_roster_period):
            print("DEBUG text-to-sql 기간 조건 누락으로 거부:", repr(safe_sql))
            return [], safe_sql

    try:
        cur.execute(safe_sql)
        rows = cur.fetchall()
    except Exception as e:
        print("DEBUG text-to-sql 실행 실패:", e)
        return [], safe_sql

    return _format_rows(rows), safe_sql


# =========================
# 집계형 질문 감지
# =========================

_AGGREGATE_HINTS = (
    "평균", "최다", "가장", "최고", "최소", "최저", "순위", "랭킹",
    "몇", "총", "합계", "비교", "누가", "제일", "승률", "월별", "월간",
    "top", "most", "best",
    # 상대전적/맞대결: 팀 하나가 엔티티로 잡혀 고정 템플릿 경로로 새는 걸 막고,
    # Text-to-SQL(패턴2: (A,B) OR (B,A) 상대전적)을 강제로 태운다.
    "상대전적", "맞대결", "전적", "head-to-head", "h2h",
)


def _looks_aggregate(question):
    q = str(question).lower()
    return any(hint in q for hint in _AGGREGATE_HINTS)


_TEMPLATE_METRIC_HINTS = (
    "경기", "게임", "전적", "승률", "성적", "kda", "킬", "데스", "어시스트",
    "cs", "골드", "로스터", "선수", "전략", "운영", "오브젝트", "드래곤",
    "바론", "전령", "타워",
)


def _templates_cover_question(question, players, teams, period):
    if _looks_month_comparison(question) and (players or teams):
        return True
    if _looks_player_champion_usage(question) and players:
        return True
    if _looks_player_kda_question(question) and players:
        return True
    if _looks_roster_question(question) and teams:
        return True
    if _looks_team_record_question(question) and teams:
        return True
    entity_count = len(players) + len(teams)
    if (period or {}).get("year") and entity_count == 1:
        q = str(question or "").lower()
        return any(hint in q for hint in _TEMPLATE_METRIC_HINTS)
    return False


# 전략/운영 질문 감지: 오브젝트·골드 주도권 같은 정형 전략지표를 덧붙일지 결정
_STRATEGY_HINTS = (
    "전략", "전술", "운영", "밴픽", "픽", "조합", "스와프", "주도권",
    "스노우볼", "초반", "한타", "교전", "시야", "골드", "오브젝트",
    "드래곤", "바론", "전령", "승리 요인", "강점", "약점", "어떻게 이겼",
)


def _looks_player_champion_usage(question):
    q = str(question or "").lower()
    return (
        any(token in q for token in ("챔피언", "챔프", "champion"))
        and any(token in q for token in ("사용", "쓴", "플레이", "픽", "고른", "선택", "used", "played"))
    )


def _looks_player_kda_question(question):
    q = str(question or "").lower()
    return "kda" in q and not _looks_month_comparison(q)


def _looks_roster_question(question):
    q = str(question or "").lower()
    return any(
        token in q
        for token in ("로스터", "라인업", "멤버", "선수 구성", "선수 명단")
    )


def _looks_team_record_question(question):
    q = str(question or "").lower()
    return any(token in q for token in ("승률", "전적", "몇 승", "몇승", "승패"))


def _looks_match_lineup_question(question):
    q = str(question or "").lower()
    has_day = bool(
        re.search(r"(?:0?[1-9]|1[0-2])\s*월", q)
        and re.search(r"(?:0?[1-9]|[12]\d|3[01])\s*일", q)
    )
    has_match = any(token in q for token in ("매치", "경기", "대진", "match"))
    return has_day and has_match


def _looks_strategy(question):
    q = str(question).lower()
    return any(hint in q for hint in _STRATEGY_HINTS)


# =========================
# 메인 함수
# =========================

def _empty_rdb_result(keywords=None, period=None):
    resolved_period = period or _extract_period("")
    return {
        "keywords": keywords or [],
        "entities": {"players": [], "teams": []},
        "entity_ids": {"players": [], "teams": []},
        "facts": [],
        "context": "",
        "sql": None,
        "scope": {
            **resolved_period,
            "label": _period_label(resolved_period),
            "start_date": None,
            "end_date": None,
            "filters_applied": [],
        },
        "evidence": [],
        "total_facts": 0,
        "truncated": False,
        "champion_usage": [],
        "match_lineups": [],
    }


def search_rdb_knowledge(
    question, keywords, max_facts=None, *, allow_text_to_sql=True
):
    """
    질문/키워드 기반으로 espo RDB에서 정형 사실을 조회한다.
    allow_text_to_sql=False이면 평가·재현 실험에서 HyperCLOVA Text-to-SQL
    폴백을 건너뛰고 고정 SQL 템플릿 결과만 반환한다.
    반환: {keywords, entities, facts(list[str]), context(str), sql}
    """
    kw_list = _as_keyword_list(keywords)
    period = _extract_period(question)
    print("DEBUG rdb keywords:", kw_list)
    print("DEBUG rdb period(raw):", period)

    try:
        conn = get_connection()
    except Exception as e:
        print("DEBUG RDB 연결 실패:", e)
        return _empty_rdb_result(kw_list, period)

    facts = []
    sql_used = None
    players = []
    teams = []
    champion_usage_rows = []
    match_lineups = []

    try:
        with conn.cursor() as cur:
            period = _resolve_period_year(cur, period)
            players = _resolve_players(cur, kw_list) if kw_list else []
            teams = _resolve_teams(cur, kw_list) if kw_list else []
            print("DEBUG rdb players:", [p["player_name"] for p in players])
            print("DEBUG rdb teams:", [t["team_name"] for t in teams])

            match_lineup_query = _looks_match_lineup_question(question)
            if match_lineup_query:
                match_lineups = _match_lineups_on_date(cur, period)
                if teams:
                    team_ids = {int(team["team_id"]) for team in teams}
                    match_lineups = [
                        match for match in match_lineups
                        if team_ids.intersection(
                            int(team["team_id"]) for team in match.get("teams") or []
                        )
                    ]
                facts += _match_lineup_facts(match_lineups)
            else:
                # 2) 엔티티가 잡히면 고정 통계 쿼리 실행
                if players or teams:
                    facts += _run_templates(cur, players, teams, question, period)

                # KDA 화면과 챔피언 사용 질문은 답변 fact와 별개로 그래프용 상위 픽을 보존한다.
                if players and (
                    _looks_player_kda_question(question)
                    or _looks_player_champion_usage(question)
                ):
                    team_ids = [team["team_id"] for team in teams]
                    for player in players:
                        champion_usage_rows.extend(
                            _player_champion_usage_rows(
                                cur, player, period, team_ids=team_ids
                            )
                        )

                # (A) 전략/운영 질문이면 오브젝트·골드 주도권 등 정형 전략지표 추가
                if teams and _looks_strategy(question):
                    for team in teams:
                        facts += _team_strategy_facts(cur, team, period)

                # 3) 폴백: 고정 쿼리로 못 채웠거나 집계형 질문이면 Text-to-SQL
                entity_hint = _build_entity_hint(players, teams, period)
                if allow_text_to_sql and (not facts or (
                    _looks_aggregate(question)
                    and not _templates_cover_question(question, players, teams, period)
                )):
                    sql_facts, sql_used = _text_to_sql_facts(
                        cur, question, entity_hint, period=period
                    )
                    facts += sql_facts
    finally:
        conn.close()

    # 질문 범위의 전체 사실을 유지한다. 집계 SQL은 전체 행을 읽고, 상세 해설 LLM이
    # 이 fact pack을 종합한 뒤 별도 요약 LLM이 압축한다.
    unique_facts = list(dict.fromkeys(facts))
    total_facts = len(unique_facts)
    facts = unique_facts if max_facts is None else unique_facts[:max_facts]
    context = "\n".join(f"{i}. {f}" for i, f in enumerate(facts, start=1))
    start_date, end_date = _period_bounds(period)
    filters_applied = []
    if period.get("year"):
        filters_applied.append(_period_label(period))
    if teams:
        filters_applied.append("팀 ID: " + ", ".join(
            f"{team['team_name']}={team['team_id']}" for team in teams
        ))
    if players:
        filters_applied.append("선수 ID: " + ", ".join(
            f"{player['player_name']}={player['player_id']}" for player in players
        ))
    if teams:
        filters_applied.append(
            "로스터 start_date/end_date 유효기간"
            if period.get("year") else "teamroster 전체 소속 이력"
        )

    scope = {
        **period,
        "label": _period_label(period),
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "filters_applied": filters_applied,
    }

    print("DEBUG rdb facts:", len(facts))

    return {
        "keywords": kw_list,
        "entities": {
            "players": [p["player_name"] for p in players],
            "teams": [t["team_name"] for t in teams],
        },
        "entity_ids": {
            "players": [
                {"player_id": p["player_id"], "player_name": p["player_name"]}
                for p in players
            ],
            "teams": [
                {"team_id": t["team_id"], "team_name": t["team_name"]}
                for t in teams
            ],
        },
        "facts": facts,
        "context": context,
        "sql": sql_used,
        "scope": scope,
        "evidence": [
            {"id": f"R{i}", "text": fact}
            for i, fact in enumerate(facts, start=1)
        ],
        "total_facts": total_facts,
        "truncated": total_facts > len(facts),
        "champion_usage": champion_usage_rows or [
            {
                "player_name": match.group(1),
                "champion_name": match.group(2),
                "games": int(match.group(3)),
                "wins": int(match.group(4)),
            }
            for fact in facts
            for match in [re.match(
                r"^\(ChampionUsage\)\s*(?:\[[^\]]+\]\s*)?(.+?)\s+—\s+(.+?)\s+(\d+)회 사용,\s*(\d+)승$",
                fact,
            )]
            if match
        ],
        "match_lineups": match_lineups,
    }


def _build_entity_hint(players, teams, period=None):
    hints = []
    for p in players:
        hints.append(f"선수 '{p['player_name']}' = player_id {p['player_id']}")
    for t in teams:
        hints.append(f"팀 '{t['team_name']}' = team_id {t['team_id']}")
    if (period or {}).get("year"):
        start_date, end_date = _period_bounds(period)
        hints.append(
            f"날짜 범위 '{_period_label(period)}' = match_date >= '{start_date}' "
            f"AND match_date < '{end_date + timedelta(days=1)}'"
        )
    if not hints:
        return ""
    return "[질문에서 확인된 엔티티]\n" + "\n".join(hints)


if __name__ == "__main__":
    result = search_rdb_knowledge("페이커 평균 KDA 알려줘", ["Faker"])
    print("\n=== RDB context ===")
    print(result["context"])
    print("SQL:", result["sql"])
