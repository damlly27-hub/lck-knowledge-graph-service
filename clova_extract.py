import os
import json
import re
import time
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv

# 실행 위치(cwd)에 흔들리지 않도록 이 파일과 같은 폴더의 .env 를 명시적으로 읽는다.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH)

print("★★★★ clova_extract.py 로드됨 ★★★★")

CLOVA_API_KEY = os.getenv("CLOVA_API_KEY")
CLOVA_APP_TYPE = os.getenv("CLOVA_APP_TYPE", "testapp")
CLOVA_MODEL = os.getenv("CLOVA_MODEL", "HCX-007")

CLOVA_API_URL = (
    f"https://clovastudio.stream.ntruss.com/"
    f"{CLOVA_APP_TYPE}/v3/chat-completions/{CLOVA_MODEL}"
)


QUESTION_SUMMARY_MAX_CHARS = 130
SUMMARY_MAX_CHARS = 200
DETAIL_MAX_CHARS = 600

_STRATEGY_EVENT_TERMS = (
    "에이스", "한타", "교전", "다이브", "스틸", "역전", "백도어", "킬",
)


def _limit_plain_text(text, max_chars=SUMMARY_MAX_CHARS):
    """공백을 정리한 평문을 글자 수 상한 안에서 자연스럽게 자른다."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if max_chars <= 0 or len(value) <= max_chars:
        return value

    cut = value[:max_chars].rstrip()
    # 상한 안에 충분히 긴 완성 문장이 있으면 문장 중간을 자르지 않는다.
    sentence_end = max(cut.rfind(mark) for mark in ".!?。！？")
    if sentence_end >= int(max_chars * 0.4):
        return cut[:sentence_end + 1].strip()
    # 가능하면 너무 짧아지지 않는 마지막 어절 경계에서 자른다.
    last_space = cut.rfind(" ")
    if last_space >= int(max_chars * 0.7):
        cut = cut[:last_space].rstrip()
    cut = cut.rstrip(",;:·- ")
    if len(cut) < max_chars:
        cut += "…"
    return cut[:max_chars]


def build_clova_prompt(chunk):
    return f"""
다음 롤 e스포츠 기사에서 지식그래프용 엔티티와 관계를 JSON으로 추출해줘.

추출 대상(엔티티) — 각 항목을 알맞은 버킷에만 넣어라:
- players      : 선수 (예: 페이커, 쵸비, 이상혁 — 본명도 선수다)
- teams        : 프로게임단 (예: T1, 젠지, 한화생명e스포츠)
- coaches      : 감독·코치 (예: 김정균, 최인규 감독) — 선수가 아니다, players 에 넣지 마라
- champions    : LoL 챔피언 (예: 아지르, 리 신, 제드)
- objectives   : 게임 내 목표물 (예: 드래곤, 바론, 전령, 억제기)
- tournaments  : 대회·경기·시즌 (예: 2025 LCK 서머, 월즈 8강, 플레이오프, "T1전")
- leagues      : 리그 (예: LCK, LPL, LEC, LCS) — 개별 대회가 아니라 리그 자체
- positions    : 포지션 (탑, 정글, 미드, 원딜/바텀, 서포터)
- patches      : 패치·버전 (예: 25.06 패치, 라인 스와프 방지 패치)
- strategies   : 운영·전략·조합 등 '게임 플레이 개념'
                 (예: 라인 스와프, 피어리스 드래프트, 밴픽, 돌진 조합, 시야 장악, 초반 주도권)

분류 규칙(반드시 지켜라 — 이걸 어기면 그래프가 오염된다):
- 모든 노드 이름은 짧은 명사 또는 명사구로 써라. 완결된 문장이나 "~했다/~한다/~입니다" 같은
  서술형 종결어미를 노드 이름에 넣지 마라. 예: "바론을 확보했다"가 아니라 "바론 확보",
  "시야를 장악했다"가 아니라 "시야 장악".
- "정규 시즌 2라운드 경기", "T1전", "플레이오프", "MSI" 같은 대회/경기 표현은 tournaments 로.
  절대 strategies 에 넣지 마라.
- LCK/LPL 같은 리그명은 leagues 로, 그 안의 특정 대회는 tournaments 로 구분해라.
- 사람은 선수면 players, 감독/코치면 coaches. 팀명은 teams.
- strategies 에는 '전략·운영·조합·밴픽·스와프·주도권·시야' 같은 플레이 개념만.
- 아래는 어떤 버킷에도 넣지 마라(노이즈): 기사, 팬, 팬들, 인터뷰, 기자회견, 기사 작성,
  선물, 숙소, 감독(직책 단독 단어), None, 단순 감상/소감.

이름 표기:
- 기사에 나온 한국어 표기 그대로. 영어로 번역하지 마(페이커를 Faker로 바꾸지 마).
- 네가 임의로 별칭을 통일하지 마. 별칭 통일은 이후 사전이 처리한다.

관계(relation) 규칙:

1. 아래 "표준 관계"에 해당하면 반드시 이 이름으로(방향도 화살표대로).

   * MEMBER_OF         : 선수 → 팀 (소속)
   * COACHES           : 감독/코치 → 팀
   * PLAYS_ROLE        : 선수 → 포지션 (예: 페이커 → 미드)
   * PICKED            : 선수 → 챔피언 (사용/픽)
   * WON               : 팀 → 경기/대회 (승리/우승)
   * LOST              : 팀 → 경기/대회 (패배)
   * PLAYED_IN         : 선수/팀 → 경기/대회 (참가/출전)
   * BELONGS_TO_LEAGUE : 대회 → 리그 (예: 2025 LCK 서머 → LCK)
   * USED_STRATEGY     : 팀/선수 → 전술 (그 팀·선수가 쓴 운영·조합·밴픽)
   * DIRECTED          : 감독 → 선수/전술 (감독의 지시·주문)
   * MADE_CALL         : 선수 → 전술/판단 (선수의 결정·빠른 콜·스와프 판단)
     예: "T1이 페이커를 보유" 는 페이커 → T1 (MEMBER_OF)

1-1) 경기 결과가 포함된 문장에서는 승리팀만 추출하지 말고 패배팀도 반드시 함께 추출한다.
단, 팀끼리 직접 "이김/패배" 관계를 중복으로 만들지 말고, 하나의 경기/대회 노드를 기준으로 승리팀과 패배팀을 각각 연결한다.

기본 구조:

* 승리팀 → 경기/대회 (WON)
* 패배팀 → 경기/대회 (LOST)

예: "T1이 2025 LCK 결승에서 젠지를 꺾고 우승했다."

* T1 → 2025 LCK 결승 (WON)
* 젠지 → 2025 LCK 결승 (LOST)

1-2) 문장에서 경기명, 결승, 준결승, 플레이오프, 특정 세트, 특정 대회가 명확히 나오면 그 이름을 경기/대회 노드로 사용한다.
경기/대회명이 명확하지 않지만 두 팀의 승패가 분명하면 "팀A vs 팀B 경기" 형식의 경기 노드를 생성한다.

예: "T1이 젠지를 꺾었다."

* T1 → T1 vs 젠지 경기 (WON)
* 젠지 → T1 vs 젠지 경기 (LOST)

1-3) WON 또는 LOST 관계가 생성된 팀에 대해서는 같은 경기/대회에 PLAYED_IN을 중복 생성하지 않는다.
WON과 LOST 자체가 해당 경기 참가 의미를 포함하므로 중복 관계를 피한다.

1-4) 단순히 우승팀만 언급되고 상대팀이 명확하지 않은 경우에는 패배 관계를 만들지 않는다.

예: "T1이 2025 LCK를 우승했다."

* T1 → 2025 LCK (WON)

상대팀이 문장에 없으므로 LOST는 생성하지 않는다.

1-5) "패배", "패했다", "졌다", "무너졌다", "탈락했다", "준우승에 머물렀다", "역전패", "완패", "석패"처럼 패배 의미가 명확한 표현이 있으면 LOST 관계를 생성한다.

2. 위에 없지만 의미 있는 관계(발언·평가·영입·이적·부상·수상·사건 등)는
   억지로 끼워맞추지 말고 간결한 한국어 관계명으로 자유롭게. (예: "부상당함", "이적함", "MVP선정")
3. "관련있다"·"언급됨"처럼 모호하고 정보 없는 관계는 넣지 마라.
4. 각 관계에는 근거가 된 기사 원문 한 문장을 evidence 로 반드시 넣어라.


반드시 아래 JSON 형식으로만 답해줘.

{{
  "players": [],
  "teams": [],
  "coaches": [],
  "champions": [],
  "objectives": [],
  "tournaments": [],
  "leagues": [],
  "positions": [],
  "patches": [],
  "strategies": [],
  "relations": [
    {{
      "source": "",
      "relation": "",
      "target": "",
      "evidence": ""
    }}
  ]
}}

기사:
{chunk}
"""


def extract_content_from_clova_response(response_text):
    """
    HyperCLOVA 응답(text/event-stream 또는 json)에서
    최종 content만 안정적으로 추출하는 함수
    """

    # 1) 혹시 일반 JSON 응답이면 먼저 처리
    try:
        data = json.loads(response_text)

        if "result" in data:
            result = data["result"]
            if isinstance(result, dict):
                if "message" in result and "content" in result["message"]:
                    return result["message"]["content"]
                if "content" in result:
                    return result["content"]

        if "message" in data and "content" in data["message"]:
            return data["message"]["content"]

        if "choices" in data:
            return data["choices"][0]["message"]["content"]

    except:
        pass

    # 2) text/event-stream 응답 처리
    contents = []

    for line in response_text.splitlines():
        line = line.strip()

        if not line.startswith("data:"):
            continue

        raw = line.replace("data:", "", 1).strip()

        if raw == "[DONE]":
            continue

        try:
            obj = json.loads(raw)

            # HyperCLOVA streaming 응답에서 content 추출
            if "message" in obj:
                msg = obj["message"]

                # content가 있으면 우선 사용
                if "content" in msg and msg["content"]:
                    contents.append(msg["content"])

                # thinkingContent는 최종 답변이 아니라 중간 추론일 수 있으므로 제외
                # if "thinkingContent" in msg and msg["thinkingContent"]:
                #     contents.append(msg["thinkingContent"])

            elif "result" in obj:
                result = obj["result"]
                if isinstance(result, dict):
                    if "message" in result and "content" in result["message"]:
                        contents.append(result["message"]["content"])
                    elif "content" in result:
                        contents.append(result["content"])

            elif "choices" in obj:
                delta = obj["choices"][0].get("delta", {})
                if "content" in delta and delta["content"]:
                    contents.append(delta["content"])

        except:
            continue

    final_text = "".join(contents).strip()

    # 3) 같은 답변이 정확히 두 번 반복되면 한 번만 남기기
    if final_text:
        half = len(final_text) // 2
        if len(final_text) % 2 == 0 and final_text[:half] == final_text[half:]:
            final_text = final_text[:half]

    return final_text


def parse_json_only(text):
    text = text.strip()
    text = text.replace("```json", "")
    text = text.replace("```", "")
    text = text.strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("JSON 시작 중괄호를 찾지 못함")

    text = text[start:]

    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(text)

    return obj


def extract_entities_with_clova(chunk):
    print("🔥 함수 진입")

    prompt = build_clova_prompt(chunk)

    headers = {
        "Authorization": f"Bearer {CLOVA_API_KEY}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }

    body = {
        "messages": [
            {
                "role": "system",
                "content": "너는 e스포츠 기사에서 지식그래프용 JSON만 추출하는 분석기다."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.1,
        "topP": 0.8,
        "topK": 0,
        "maxCompletionTokens": 2048,
        "repetitionPenalty": 1.05
    }

    # CLOVA 서버가 일시적으로 연결을 끊는 경우(ConnectionReset 등)가 있어
    # 재시도(지수 백오프)를 둔다. 끝까지 실패하면 빈 결과를 반환해
    # 해당 청크만 건너뛰고 전체 파이프라인은 계속 진행되게 한다.
    max_retries = 4
    response = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                CLOVA_API_URL,
                headers=headers,
                json=body,
                timeout=90
            )
            break
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt  # 2, 4, 8, 16초
            print(f"⚠️ CLOVA 요청 실패({attempt}/{max_retries}): {e} → {wait}s 후 재시도")
            if attempt == max_retries:
                print("❌ CLOVA 재시도 모두 실패 → 이 청크 건너뜀")
                return {
                    "players": [], "teams": [], "champions": [],
                    "objectives": [], "tournaments": [], "relations": [],
                    "error": f"request failed after {max_retries} retries: {e}",
                }
            time.sleep(wait)

# 아래 print 두줄 원래 상태로 되돌려두려면 #############
    print("CLOVA HTTP STATUS:", response.status_code)
    print("CLOVA RAW RESPONSE:", repr(response.text[:1000]))

#################################################################

    if response.status_code != 200:
        return {
            "players": [],
            "teams": [],
            "champions": [],
            "objectives": [],
            "tournaments": [],
            "relations": [],
            "error": response.text
        }

    content = extract_content_from_clova_response(response.text)

    print("EXTRACTED CONTENT:", repr(content[:1000]))

    try:
        result = parse_json_only(content)
        return result
    except Exception as e:
        print("JSON 파싱 실패:", e)
        return {
            "players": [],
            "teams": [],
            "champions": [],
            "objectives": [],
            "tournaments": [],
            "relations": [],
            "error": str(e),
            "raw_content": content
        }

       
       
def call_hyperclova_summary(question, context):
    prompt = f"""
아래 참고자료를 바탕으로 사용자의 질문에 답변해줘.
반드시 참고자료에 있는 내용만 근거로 답변해줘.
없는 내용은 추측하지 말고 "제공된 기사에서는 확인되지 않습니다"라고 말해줘.

[사용자 질문]
{question}

[참고자료]
{context}
"""

    headers = {
        "Authorization": f"Bearer {CLOVA_API_KEY}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }

    body = {
        "messages": [
            {
                "role": "system",
                "content": "너는 롤 e스포츠 기사 기반 질의응답 도우미다."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.2,
        "topP": 0.8,
        "topK": 0,
        "maxCompletionTokens": 1024,
        "repetitionPenalty": 1.05
    }

    response = requests.post(
        CLOVA_API_URL,
        headers=headers,
        json=body,
        timeout=90
    )

    content = extract_content_from_clova_response(response.text)

    return content


def _call_clova_chat(system_prompt, user_prompt, *, temperature=0.2, max_tokens=1024):
    """
    HyperCLOVA chat-completions 공통 호출 헬퍼.
    성공 시 (content, None), 실패 시 (None, error_text) 반환.
    """
    headers = {
        "Authorization": f"Bearer {CLOVA_API_KEY}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "topP": 0.8,
        "topK": 0,
        "maxCompletionTokens": max_tokens,
        "repetitionPenalty": 1.05,
    }

    response = requests.post(CLOVA_API_URL, headers=headers, json=body, timeout=90)

    if response.status_code != 200:
        return None, response.text

    return extract_content_from_clova_response(response.text), None


def _strategy_inference_inputs(article_evidence, graph):
    """기사 발췌와 화면의 정형 근거 노드를 LLM용 ID 묶음으로 만든다."""
    articles = []
    for index, item in enumerate(article_evidence or [], start=1):
        text = str(
            item.get("analysis_text")
            or item.get("excerpt")
            or item.get("matched_chunk")
            or ""
        ).strip()
        if text:
            articles.append({
                "id": f"A{index}",
                "article_id": item.get("article_id"),
                "title": item.get("title") or "기사 원문",
                "published_at": str(item.get("published_at") or ""),
                "text": text,
            })

    node_by_id = {
        str(node.get("id")): node for node in (graph or {}).get("nodes", [])
    }
    match_by_evidence = {}
    for link in (graph or {}).get("links", []):
        if link.get("type") != "HAS_STRATEGY_EVIDENCE":
            continue
        source = str(link.get("source"))
        target = str(link.get("target"))
        if node_by_id.get(source, {}).get("label") == "Match":
            match_by_evidence[target] = source
        elif node_by_id.get(target, {}).get("label") == "Match":
            match_by_evidence[source] = target

    structured = []
    for node in (graph or {}).get("nodes", []):
        if node.get("label") != "StrategyEvidence":
            continue
        evidence_id = str(node.get("id"))
        match_id = match_by_evidence.get(evidence_id)
        match_node = node_by_id.get(match_id, {})
        structured.append({
            "id": f"S{len(structured) + 1}",
            "node_id": evidence_id,
            "name": node.get("name") or evidence_id,
            "detail": node.get("title") or "",
            "match_node_id": match_id,
            "match_id": match_node.get("match_id") or match_id,
            "match": match_node.get("name") or match_id,
            "match_title": match_node.get("title") or "",
            "team_node_id": node.get("team_node_id"),
            "team": node.get("team") or "",
        })
    return articles, structured


def _validate_strategy_insights(raw_insights, articles, structured):
    """근거 ID와 기사 사건 표현을 검증해 허용된 전략 추론만 남긴다."""
    article_by_id = {item["id"]: item for item in articles}
    structured_by_id = {item["id"]: item for item in structured}
    validated = []
    for raw in raw_insights if isinstance(raw_insights, list) else []:
        if not isinstance(raw, dict):
            continue
        name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip().rstrip(".!?")
        explanation = re.sub(r"\s+", " ", str(raw.get("explanation") or "")).strip()
        structured_ids = [
            value for value in dict.fromkeys(raw.get("structured_ids") or [])
            if value in structured_by_id
        ]
        article_ids = [
            value for value in dict.fromkeys(raw.get("article_ids") or [])
            if value in article_by_id
        ]
        if not name or not explanation or not structured_ids:
            continue

        match_node_ids = list(dict.fromkeys(
            str(structured_by_id[value].get("match_node_id") or "").strip()
            for value in structured_ids
            if structured_by_id[value].get("match_node_id")
        ))
        # 한 전략이 여러 Match의 근거를 공유하지 못하게 한다. match_node_id를 생략한
        # 응답은 structured_ids가 정확히 한 Match에만 속할 때에만 안전하게 보완한다.
        if len(match_node_ids) != 1:
            continue
        match_node_id = match_node_ids[0]
        requested_match_id = str(raw.get("match_node_id") or "").strip()
        if requested_match_id and requested_match_id != match_node_id:
            continue

        claim = f"{name} {explanation}"

        article_text = " ".join(article_by_id[value]["text"] for value in article_ids)
        unsupported_event = any(
            term in claim and term not in article_text
            for term in _STRATEGY_EVENT_TERMS
        )
        if unsupported_event:
            continue

        selected_evidence = [structured_by_id[value] for value in structured_ids]
        team_node_ids = list(dict.fromkeys(
            str(item.get("team_node_id") or "").strip()
            for item in selected_evidence if item.get("team_node_id")
        ))
        if len(team_node_ids) > 1:
            continue
        match_identity = next(
            (
                item.get("match_id") for item in selected_evidence
                if item.get("match_id") is not None
            ),
            match_node_id,
        )
        confidence = str(raw.get("confidence") or "보통").strip()
        if confidence not in {"높음", "보통", "낮음"}:
            confidence = "보통"
        if not article_ids:
            confidence = "낮음"
        # 화면 그래프에는 기사 맥락과 정형 근거가 함께 확인된 전략만 노출한다.
        # 정형 수치만으로 만든 '낮음' 전략은 Match와 함께 제외한다.
        if confidence == "낮음":
            continue
        validated.append({
            "name": _limit_plain_text(name, 34),
            "explanation": _limit_plain_text(explanation, 180),
            "structured_ids": structured_ids,
            "evidence_node_ids": list(dict.fromkeys(
                str(item.get("node_id")) for item in selected_evidence
                if item.get("node_id")
            )),
            "article_ids": article_ids,
            "article_evidence": [
                {
                    "evidence_id": article_id,
                    "article_id": article_by_id[article_id].get("article_id"),
                    "title": article_by_id[article_id].get("title") or "기사 원문",
                    "published_at": article_by_id[article_id].get("published_at") or "",
                    "text": _limit_plain_text(article_by_id[article_id].get("text"), 240),
                }
                for article_id in article_ids
            ],
            "match_node_id": match_node_id,
            "match_node_ids": [match_node_id],
            "match_id": match_identity,
            "team_node_id": team_node_ids[0] if team_node_ids else None,
            "confidence": confidence,
        })
        match_insight_count = sum(
            item.get("match_node_id") == match_node_id for item in validated
        )
        if match_insight_count > 2:
            validated.pop()
    return validated


def _fallback_strategy_insights(structured):
    """LLM 실패 시에도 실제 수치가 있는 Match에만 경기별 전략을 만든다."""
    by_match = {}
    for item in structured:
        match_node_id = str(item.get("match_node_id") or "").strip()
        if match_node_id:
            by_match.setdefault(match_node_id, []).append(item)

    insights = []
    for match_node_id, match_evidence in by_match.items():
        chosen = None
        name = explanation = ""

        first = next(
            (item for item in match_evidence if "선취" in str(item.get("name") or "")),
            None,
        )
        if first:
            counts = {
                label: int(value)
                for label, value in re.findall(
                    r"첫 (드래곤|바론|타워)\s+(\d+)회",
                    str(first.get("detail") or ""),
                )
            }
            secured = [label for label, count in counts.items() if count > 0]
            if secured:
                chosen = first
                name = "·".join(
                    f"첫 {label}{counts[label]}" for label in secured
                ) + " 선점 운영"
                count_text = "·".join(
                    f"첫 {label} {counts[label]}회" for label in secured
                )
                explanation = (
                    f"{first.get('match') or '해당 경기'}에서 {count_text}가 확인되어 "
                    "초반 오브젝트 선점 운영을 직접 뒷받침합니다."
                )

        if not chosen:
            objectives = next(
                (item for item in match_evidence if "확보" in str(item.get("name") or "")),
                None,
            )
            if objectives:
                counts = {
                    label: int(value)
                    for label, value in re.findall(
                        r"(드래곤|바론|타워)\s+(\d+)",
                        str(objectives.get("detail") or ""),
                    )
                }
                secured = [label for label, count in counts.items() if count > 0]
                if secured:
                    chosen = objectives
                    top = sorted(secured, key=lambda label: counts[label], reverse=True)[:2]
                    name = "·".join(f"{label} {counts[label]}" for label in top) + " 확보 운영"
                    count_text = "·".join(
                        f"{label} {counts[label]}개" for label in secured
                    )
                    explanation = (
                        f"{objectives.get('match') or '해당 경기'}에서 {count_text}를 확보한 "
                        "정형 기록을 근거로 한 경기별 오브젝트 운영입니다."
                    )

        if not chosen:
            picks = next(
                (item for item in match_evidence if "픽 조합" in str(item.get("name") or "")),
                None,
            )
            pick_names = re.findall(
                r"(?:^|;\s*)\d+세트\s+([^;]+)", str((picks or {}).get("detail") or "")
            )
            champions = []
            for pick_line in pick_names:
                champions.extend(part.strip() for part in pick_line.split("·") if part.strip())
            champions = list(dict.fromkeys(champions))
            if picks and len(champions) >= 3:
                chosen = picks
                name = f"{'·'.join(champions[:3])} 조합 운용"
                explanation = (
                    f"{picks.get('match') or '해당 경기'}에서 실제 확인된 "
                    f"{'·'.join(champions[:5])} 조합을 근거로 한 경기별 조합 운용입니다."
                )

        if not chosen:
            continue
        insights.append({
            "name": _limit_plain_text(name, 34),
            "explanation": _limit_plain_text(explanation, 180),
            "structured_ids": [chosen["id"]],
            "evidence_node_ids": [chosen["node_id"]] if chosen.get("node_id") else [],
            "article_ids": [],
            "match_node_id": match_node_id,
            "match_node_ids": [match_node_id],
            "match_id": chosen.get("match_id") or match_node_id,
            "team_node_id": chosen.get("team_node_id"),
            "confidence": "낮음",
        })
    return insights


def _strategy_date(value):
    text = str(value or "").strip()
    matched = (
        re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
        or re.search(r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일", text)
    )
    if not matched:
        return None
    try:
        return datetime(*(int(part) for part in matched.groups())).date()
    except ValueError:
        return None


def _strategy_evidence_batches(articles, structured, batch_size=8, article_window_days=3):
    """Match를 작은 묶음으로 나누고 발행일이 가까운 기사 근거를 연결한다."""
    by_match = {}
    for item in structured:
        match_node_id = str(item.get("match_node_id") or "").strip()
        if match_node_id:
            by_match.setdefault(match_node_id, []).append(item)

    ordered_matches = sorted(
        by_match,
        key=lambda match_id: (
            _strategy_date(by_match[match_id][0].get("match_title"))
            or datetime.max.date(),
            match_id,
        ),
    )
    batches = []
    for start in range(0, len(ordered_matches), batch_size):
        match_ids = ordered_matches[start:start + batch_size]
        batch_structured = [item for match_id in match_ids for item in by_match[match_id]]
        match_dates = [
            _strategy_date(by_match[match_id][0].get("match_title"))
            for match_id in match_ids
        ]
        match_dates = [value for value in match_dates if value is not None]
        batch_articles = []
        for article in articles:
            published = _strategy_date(article.get("published_at"))
            if published is None:
                continue
            if any(abs((published - match_date).days) <= article_window_days for match_date in match_dates):
                batch_articles.append(article)
        batches.append((batch_articles, batch_structured))
    return batches


def call_hyperclova_strategy_insights(question, article_evidence, graph):
    """전체 기사 집합을 경기 날짜별 배치로 나눠 Match별 전략을 만든다."""
    articles, structured = _strategy_inference_inputs(article_evidence, graph)
    if not structured:
        return {"insights": [], "warnings": ["정형 전략 근거가 없습니다."]}

    system = (
        "너는 LCK 전략 분석가다. 기사 사실과 정형 통계를 구분하고, 제공된 근거 ID를 "
        "인용할 수 있는 경기별 전략만 JSON으로 출력한다."
    )

    def infer_batch(batch_articles, batch_structured):
        if not batch_articles or not batch_structured:
            return [], None
        evidence_json = json.dumps(
            {"articles": batch_articles, "structured": batch_structured},
            ensure_ascii=False,
            default=str,
        )
        user = f"""다음 질문에 대해 화면 그래프에 표시할 경기별 전략을 만들어라.

[질문]
{question}

[근거]
{evidence_json}

규칙:
- Match마다 최대 2개까지만 만들고, 근거가 부족한 Match는 insights에서 생략한다.
- 각 insight에 match_node_id를 반드시 쓰고, structured_ids는 그 Match에 속한 ID만 사용한다.
- name은 8~34자의 구체적인 전략 명사구로 쓴다. 해당 경기의 실제 오브젝트 수치,
  픽 조합, 밴픽 또는 기사 속 행동 차이가 드러나게 한다.
- 서로 다른 Match에 같은 기본 전략 문구를 복사하지 않는다. 같은 전략명이 실제로 반복된
  경우에도 각 Match의 다른 근거가 explanation에 드러나야 한다.
- 반드시 structured_ids를 1개 이상 사용한다. 정형 근거만으로 행동이나 인과를 만들어내지 않는다.
- 기사 내용이 해당 Match와 같은 경기임을 명확히 확인할 수 있고 전략 해석을 직접
  뒷받침할 때만 article_ids를 사용한다.
- 에이스·한타·교전·다이브·스틸·역전·백도어·킬 같은 구체 사건은 참조한 기사 본문에
  같은 사건이 명시된 경우에만 name/explanation에 쓸 수 있다.
- 첫 용·바론·타워 수치만으로 특정 한타나 에이스를 만들어내지 마라.
- 정형 수치는 해당 경기의 관측 사실을, 기사는 행동과 맥락을 뒷받침하는 용도로 구분한다.
- confidence는 기사와 정형 근거가 함께 직접 지지하면 '높음', 일부만 지지하면 '보통',
  정형 근거만 있으면 '낮음'으로 쓴다.
- confidence가 '낮음'인 후보는 insights에 넣지 마라. 화면에는 '보통' 이상만 표시한다.

JSON 형식:
{{"insights":[{{"match_node_id":"실제 Match 노드 ID","name":"전략명","explanation":"해당 경기 설명","structured_ids":["S1"],"article_ids":["A1"],"confidence":"보통"}}]}}
"""
        content, error = _call_clova_chat(system, user, temperature=0.1, max_tokens=4096)
        if error or not content:
            return [], error or "빈 응답"
        try:
            result = parse_json_only(content)
            return _validate_strategy_insights(
                result.get("insights"), batch_articles, batch_structured
            ), None
        except Exception as exc:
            return [], str(exc)

    batches = _strategy_evidence_batches(articles, structured)
    active_batches = [batch for batch in batches if batch[0] and batch[1]]
    print(
        "DEBUG strategy inference batches:", len(active_batches), "of", len(batches),
        "articles=", len(articles),
        "matches=", len({item.get("match_node_id") for item in structured}),
    )
    insights = []
    errors = []
    if active_batches:
        with ThreadPoolExecutor(max_workers=min(3, len(active_batches))) as executor:
            futures = {
                executor.submit(infer_batch, batch_articles, batch_structured): index
                for index, (batch_articles, batch_structured) in enumerate(active_batches)
            }
            for future in as_completed(futures):
                try:
                    batch_insights, error = future.result()
                    insights.extend(batch_insights)
                    if error:
                        errors.append(f"batch {futures[future] + 1}: {error}")
                except Exception as exc:
                    errors.append(f"batch {futures[future] + 1}: {exc}")

    match_order = {
        str(item.get("match_node_id")): index for index, item in enumerate(structured)
    }
    insights.sort(key=lambda item: match_order.get(str(item.get("match_node_id")), 10**9))
    if insights:
        warnings = [f"일부 전략 분석 배치 {len(errors)}건이 실패했습니다."] if errors else []
        return {"insights": insights, "warnings": warnings}
    if errors:
        print("DEBUG strategy insight batch failures:", errors)
        return {
            "insights": [],
            "warnings": [
                "LLM 전략 추론에 실패해 신뢰도 낮은 수치 기반 전략은 표시하지 않았습니다."
            ],
        }
    return {
        "insights": [],
        "warnings": ["신뢰도 보통 이상으로 검증된 경기별 전략이 없습니다."],
    }


# 시즌 스코프 결정용 결정적 키워드(백스톱). LLM 분류가 흔들려도 오염을 막는다.
# 시즌후(post-season) 신호가 있으면 필터를 끄고(현재/이적 정보 필요),
# 시즌중(in-season) 신호가 있으면 필터를 켠다(경기·전략·챔피언 등).
_POST_SEASON_HINTS = (
    "이적", "영입", "지금", "현재", "다음 시즌", "다음시즌", "내년", "차기 시즌",
    "은퇴", "fa", "계약", "재계약", "로스터 개편", "합류", "떠난", "이적생",
)
_IN_SEASON_HINTS = (
    "우승", "전략", "챔피언", "챔프", "밴픽", "픽", "오브젝트", "바론", "드래곤",
    "장로", "전령", "한타", "경기", "승리", "이긴", "운영", "빌드", "조합", "세트",
)


def _season_scope_hint(question):
    """질문 문자열로 시즌 스코프를 결정적으로 추정. True/False/None(판단보류)."""
    q = str(question).lower()
    if any(h in q for h in _POST_SEASON_HINTS):
        return False   # 시즌후 정보가 필요한 질문 → 필터 끔
    if any(h in q for h in _IN_SEASON_HINTS):
        return True    # 시즌 중 사실 질문 → 필터 켬
    return None


def analyze_question_intent(question):
    """
    질문 의도 분석(파이프라인 1단계).
    HyperCLOVA로 의도 문장과 검색 키워드 목록을 추출한다.
    반환: {"intent": str, "keywords": list[str], "qtype": "numeric"|"relational"}

    qtype 은 답변 시 DB 가중치를 정하는 데 쓴다.
    - "numeric": 승률·횟수·합계·평균·KDA/킬/데스/골드 등 수치·집계·통계·순위를 묻는 질문
                 → RDB(정형 통계) 비중을 크게.
    - "relational": 전략·과정·이유(왜/어떻게 이겼는지)·선수/팀/오브젝트/경기 간 관계 등
                    서술형 질문 → 지식그래프 비중을 크게. (기본값)
    """
    system = "너는 LCK e스포츠 질의응답 시스템의 질문 분석기다. 반드시 JSON만 출력한다."
    user = f"""다음 사용자 질문의 의도와 검색 키워드, 그리고 질문 유형을 추출해줘.

키워드는 질문에 나온 한국어 표기 그대로 뽑아라. 영어로 번역하거나 임의로 바꾸지 마.
(별칭 통일은 이후 사전이 자동으로 처리한다.)

qtype 은 다음 둘 중 하나로만 분류해:
- "numeric": 승률, 이긴/진 횟수, 합계, 평균, KDA·킬·데스·어시스트·골드 같은 수치나
  집계·통계·순위를 묻는 질문. (예: "T1 승률", "T1이 이긴 경기 총횟수", "구마유시 평균 데스")
- "relational": 전략, 경기가 흘러간 과정, 왜/어떻게 이겼는지, 선수·팀·오브젝트·경기 사이의
  관계를 묻는 서술형 질문. (예: "T1이 특정 경기에서 오브젝트를 어떻게 굴려 이겼는지")
- 애매하면 "relational" 로 둬라.

season_scoped 는 true/false 로 분류해:
- true: 2025 LCK '시즌 중'(정규시즌·플레이오프)의 사실로만 답해야 하는 질문. 경기 내용,
  우승/승리 전략, 자주 쓴 챔피언, 밴픽, 오브젝트 운영, 특정 경기 흐름 등 시즌이 진행되는
  동안 일어난 일을 묻는 질문. (예: "한화생명 우승 전략", "페이커가 자주 쓴 챔피언")
- false: 시즌이 끝난 뒤의 사실이 필요한 질문. 현재/향후 로스터, 이적·영입, "지금 어느 팀
  소속", 다음 시즌 전망 같은 질문. (예: "구마유시 지금 어느 팀이야", "T1 이적 소식")
- 확신이 없으면 true 로 둬라(경기 관련 질문이 기본).

반드시 아래 JSON 형식으로만 답해줘.
{{
  "intent": "고유명사·기간·수치·비교·부정 조건을 보존한 공백 포함 130자 이내의 질문 요약",
  "keywords": ["검색", "키워드", "목록"],
  "qtype": "numeric 또는 relational",
  "season_scoped": true 또는 false
}}

질문:
{question}
"""

    # HCX-007은 추론(thinking) 토큰을 먼저 소비하므로 넉넉히 잡아야 content가 비지 않는다.
    # (512로는 필드가 늘어난 뒤 응답이 잘리거나 비어 season_scoped가 늘 기본값이 되던 문제)
    content, error = _call_clova_chat(system, user, temperature=0.1, max_tokens=2048)

    if error or not content:
        return {"intent": _limit_plain_text(question, QUESTION_SUMMARY_MAX_CHARS),
                "keywords": [], "qtype": "relational",
                "season_scoped": True, "error": error}

    try:
        result = parse_json_only(content)
        if not isinstance(result.get("keywords"), list):
            result["keywords"] = []
        result["intent"] = _limit_plain_text(
            result.get("intent") or question, QUESTION_SUMMARY_MAX_CHARS
        )
        # qtype 정규화: 모델이 numeric 이라고 확실히 말한 경우만 numeric, 그 외 전부 relational
        qtype = str(result.get("qtype", "")).strip().lower()
        result["qtype"] = "numeric" if qtype == "numeric" else "relational"
        # season_scoped 정규화: 명시적으로 false 일 때만 False, 그 외(누락/애매)는 True 기본
        sc = result.get("season_scoped", True)
        if isinstance(sc, str):
            sc = sc.strip().lower() not in ("false", "0", "no", "아니오", "n")
        sc = bool(sc)
        # 결정적 키워드 백스톱: 신호가 뚜렷하면 LLM 흔들림보다 우선한다.
        hint = _season_scope_hint(question)
        result["season_scoped"] = hint if hint is not None else sc
        return result
    except Exception as e:
        print("의도 분석 JSON 파싱 실패:", e)
        return {"intent": _limit_plain_text(question, QUESTION_SUMMARY_MAX_CHARS),
                "keywords": [], "qtype": "relational",
                "season_scoped": True, "error": str(e), "raw_content": content}


def call_hyperclova_merge(question, article_answer, graph_answer, rdb_answer="",
                          qtype="relational", graph_context=""):
    """
    기사 / 지식그래프 / RDB(정형 통계) 기반 답변을 하나로 통합(파이프라인 마지막 단계).

    qtype 에 따라 어느 DB 를 '주 근거'로 삼을지 가중치를 다르게 준다.
    - "numeric":   수치형 질문 → RDB 통계를 주 근거, 지식그래프는 보조.
    - "relational": 그 외 서술/관계형 질문 → 지식그래프를 주 근거(우리 시스템의 핵심),
                    RDB 수치는 보조. (기본값)

    LLM 호출 실패 시 세 답변을 단순 결합해서 반환한다.
    """
    fallback = (
        f"[기사 기반]\n{article_answer}\n\n"
        f"[지식그래프 기반]\n{graph_answer}\n\n"
        f"[RDB 통계 기반]\n{rdb_answer}"
    )

    if qtype == "numeric":
        weight_directive = (
            "- 이 질문은 '수치형' 질문이다. RDB 통계 답변을 최우선 근거로 삼아 승률·횟수·"
            "평균 KDA 같은 구체적 수치를 정확히 제시해라.\n"
            "- 지식그래프/기사 답변은 수치를 보완하는 보조로만 쓰고, 수치와 충돌하면 RDB를 따라라.\n"
            "- 반드시 3문장 이내로 짧게, 핵심 수치 위주로 답해."
        )
    else:
        weight_directive = (
            "- 이 질문은 '관계형/서술형' 질문이다. 이 시스템의 핵심인 지식그래프 답변을 "
            "최우선 근거로 삼아라.\n"
            "- 아래 [화면에 표시된 지식그래프 관계]가 답변의 구조이자 허용 범위다. 화면에 없는 "
            "엔티티나 관계를 기사/RDB 답변에서 새로 끌어오지 마라.\n"
            "- 그래프 노드 이름은 적힌 명사형 표기를 그대로 사용하고 다른 표현으로 바꾸지 마라.\n"
            "- 단순 사실 나열이 아니라, 팀·선수·오브젝트·경기·전략이 어떻게 연결되고 이어졌는지"
            "(관계와 인과 흐름)를 이야기하듯 설명해라. 예: 어떤 오브젝트를 확보 → 그걸로 어떻게 "
            "이득을 굴렸는지 → 그 결과 어떻게 이겼는지, 선수들이 어떤 관계로 결과에 기여했는지.\n"
            "- RDB 통계 수치는 서술을 뒷받침하는 보조 근거로만 곁들여라.\n"
            "- 4~6문장 정도로, 관계 구조가 드러나게 답해."
        )

    system = "너는 LCK e스포츠 질의응답 도우미다. 근거에 충실하게 핵심만 답한다."
    user = f"""아래 세 개의 답변을 근거로, 사용자 질문에 대한 최종 답변을 작성해줘.

[답변 비중 지침 - 이번 질문에 반드시 적용]
{weight_directive}

공통 원칙:
- 세 답변에 있는 사실만 사용하고 새로운 내용을 지어내지 마.
- 중복은 한 번만. "확인되지 않습니다"라고만 한 답변은 무시하고 사실 있는 답변을 활용해.
- 마크다운 기호(**, ##, -, `, > 등)나 목록/헤더를 쓰지 말고 자연스러운 평문 문장으로만 써.
- 불필요한 줄바꿈 없이 문장을 이어서 써(문장 사이는 공백 한 칸).

[사용자 질문]
{question}

[기사 기반 답변]
{article_answer}

[지식그래프 기반 답변]
{graph_answer}

[화면에 표시된 지식그래프 관계]
{graph_context or "표시된 관계 없음"}

[RDB 통계 기반 답변]
{rdb_answer}
"""

    content, error = _call_clova_chat(system, user, temperature=0.2, max_tokens=1024)

    if error or not content:
        return fallback

    return content.strip()


def _first_plain_sentences(text, limit=3):
    parts = re.findall(r"[^.!?。！？\n]+[.!?。！？]?", str(text or ""))
    cleaned = [part.strip() for part in parts if part.strip()]
    if limit > 0:
        cleaned = cleaned[:limit]
    return " ".join(cleaned)[:2000]


def _summary_fallback_from_facts(items):
    sentences = []
    for item in (items or [])[:2]:
        text = str((item or {}).get("text") or "").strip()
        text = re.sub(r"^\([^)]*\)\s*", "", text)
        if text:
            sentences.append(text.rstrip(". ") + ".")
    return " ".join(sentences)


def _detail_conclusion_summary(detail):
    """상세 해설에서 질문에 대한 결론 문장을 골라 짧은 요약으로 만든다."""
    value = re.sub(r"\s+", " ", str(detail or "")).strip()
    if not value:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", value) if part.strip()]
    candidates = []
    for index, sentence in enumerate(sentences):
        clean = re.sub(r"\(\d+\)(?:을|를|이|가|은|는)?", "", sentence)
        clean = re.sub(r"\[[^\]]+\]", "", clean)
        clean = re.sub(r"^통해\s+", "", clean).strip()
        if not clean:
            continue
        score = 0
        if any(token in clean for token in ("핵심", "결론", "알 수", "보여줍니다", "구사")):
            score += 5
        if any(token in clean for token in ("종합", "공통", "전반", "일반화", "전략의 핵심")):
            score += 4
        if any(token in clean for token in ("전략", "운영", "주도권", "오브젝트", "조합", "밴픽")):
            score += 3
        if any(token in clean for token in ("RDB", "KG", "기사", "필터", "확인했습니다")):
            score -= 3
        if re.search(r"20\d{2}년|\d{1,2}월\s*\d{1,2}일|\d+세트", clean):
            score -= 6
        candidates.append((score, index, clean))
    if not candidates:
        return ""
    _, _, conclusion = max(candidates, key=lambda item: (item[0], item[1]))
    return _limit_plain_text(conclusion)


def _is_statistical_summary(text):
    """질문 결론보다 수치 나열에 가까운 전략 요약인지 판별한다."""
    value = str(text or "")
    numeric_count = len(re.findall(r"\d+(?:\.\d+)?", value))
    return (
        "%" in value
        or numeric_count >= 3
        or any(token in value for token in ("승률", "평균", "경기당", "수치적으로"))
    )


def _strategy_summary_from_detail(detail, insight_fallback=""):
    """상세 결론을 우선하되 통계 문장이면 검증된 전략명 요약으로 대체한다."""
    candidate = _detail_conclusion_summary(detail)
    if _is_statistical_summary(candidate) and insight_fallback:
        return _limit_plain_text(insight_fallback)
    return candidate or _limit_plain_text(insight_fallback)


def _number_values(text):
    return {float(value) for value in re.findall(r"\d+(?:\.\d+)?", str(text or ""))}


def _korean_particle(value, consonant_form, vowel_form):
    text = str(value or "").strip()
    if not text:
        return vowel_form
    code = ord(text[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return consonant_form if (code - 0xAC00) % 28 else vowel_form
    return consonant_form


def _strategy_pick_names(items):
    names = []
    for item in (items or []):
        if item.get("relation") == "TEAM_GAME_STAT":
            properties = item.get("properties") or {}
            candidates = [properties.get(f"pick{index}") for index in range(1, 6)]
        elif (
            item.get("relation") == "HAS_STRATEGY_EVIDENCE"
            and "픽 조합" in str(item.get("target") or "")
        ):
            detail = re.sub(r"^근거\s*", "", str(item.get("detail") or ""))
            first_set = detail.split(";", 1)[0]
            first_set = re.sub(r"^\s*\d+세트\s*", "", first_set)
            candidates = first_set.split("·")
        else:
            continue
        for candidate in candidates:
            name = str(candidate or "").strip()
            if name and name not in names:
                names.append(name)
        if names:
            break
    return names


def _ensure_strategy_pick_mention(text, pick_names, *, count=2, max_chars=None):
    """픽 근거가 있는데 모델이 모두 생략한 경우에만 짧은 관측 문장을 보강한다."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    picks = [name for name in (pick_names or []) if str(name).strip()]
    if not picks or any(name in value for name in picks):
        return _limit_plain_text(value, max_chars) if max_chars else value

    addition = (
        "선별된 경기에서 확인된 픽 조합은 "
        + "·".join(picks[:count])
        + "입니다."
    )
    if not max_chars:
        return f"{value} {addition}".strip()
    if len(addition) >= max_chars:
        return _limit_plain_text(addition, max_chars)
    prefix = _limit_plain_text(value, max_chars - len(addition) - 1)
    return f"{prefix} {addition}".strip()[:max_chars]


def _strategy_chain_summary(items):
    """전략 KG의 픽·오브젝트·승리 근거를 내부 ID 없이 200자 이내로 요약한다."""
    facts = [item for item in (items or []) if str(item.get("id") or "").startswith("K")]
    inferred = [
        item for item in facts
        if item.get("relation") == "HAS_INFERRED_STRATEGY"
    ]
    if inferred:
        by_match = {}
        for item in inferred:
            match_name = str(item.get("source") or "").strip()
            strategy_name = str(item.get("target") or "").strip()
            if match_name and strategy_name:
                by_match.setdefault(match_name, []).append(strategy_name)
        team_fact = next(
            (
                item for item in facts
                if item.get("relation") == "PLAYED_MATCH"
                and item.get("target") in by_match
            ),
            None,
        )
        team = str((team_fact or {}).get("source") or "").strip()
        match_summaries = [
            f"{match_name}: {'·'.join(dict.fromkeys(names))}"
            for match_name, names in by_match.items()
        ]
        if team and match_summaries:
            return _limit_plain_text(
                f"{team}의 경기별 전략은 " + "; ".join(match_summaries) + "입니다."
            )
    evidence = [
        item for item in facts
        if item.get("relation") == "HAS_STRATEGY_EVIDENCE"
    ]
    if evidence:
        match_names = list(dict.fromkeys(
            str(item.get("source") or "").strip()
            for item in evidence
            if str(item.get("source") or "").strip()
        ))
        match_name = match_names[0] if match_names else ""
        team_fact = next(
            (
                item for item in facts
                if item.get("relation") in {"WON_MATCH", "PLAYED_MATCH"}
                and item.get("target") == match_name
            ),
            None,
        )
        team = str((team_fact or {}).get("source") or "").strip()
        if not team or not match_name:
            return ""

        first_totals = {"드래곤": 0, "바론": 0, "타워": 0}
        objective_totals = {"드래곤": 0, "바론": 0, "타워": 0}
        for item in evidence:
            target = str(item.get("target") or "")
            detail = re.sub(r"^근거\s*", "", str(item.get("detail") or ""))
            if "선취" in target:
                for label in first_totals:
                    found = re.search(rf"첫 {label}\s+(\d+)회", detail)
                    if found:
                        first_totals[label] += int(found.group(1))
            elif "확보" in target:
                for label in objective_totals:
                    found = re.search(rf"{label}\s+(\d+)", detail)
                    if found:
                        objective_totals[label] += int(found.group(1))

        scope = match_name if len(match_names) == 1 else f"선별된 {len(match_names)}개 매치"
        summary = f"{team}{_korean_particle(team, '은', '는')} {scope}에서 "
        summary += (
            f"첫 드래곤 {first_totals['드래곤']}회·첫 바론 {first_totals['바론']}회·"
            f"첫 타워 {first_totals['타워']}회를 선점했고, "
            f"드래곤 {objective_totals['드래곤']}개·바론 {objective_totals['바론']}개·"
            f"타워 {objective_totals['타워']}개를 확보했습니다"
        )
        picks = _strategy_pick_names(evidence)
        if picks:
            summary += f". 확인된 픽에는 {'·'.join(picks[:3])} 등이 포함됐습니다"
        return _limit_plain_text(summary.rstrip(".") + ".")

    stat = next((item for item in facts if item.get("relation") == "TEAM_GAME_STAT"), None)
    if not stat:
        return ""

    team = str(stat.get("source") or "").strip()
    game = str(stat.get("target") or "").strip()
    if not team or not game:
        return ""

    opponent_fact = next(
        (
            item for item in facts
            if item.get("relation") == "PLAYED_GAME"
            and item.get("target") == game
            and item.get("source") != team
        ),
        None,
    )
    won = any(
        item.get("relation") == "WON_GAME"
        and item.get("source") == team
        and item.get("target") == game
        for item in facts
    )

    metrics = []
    for part in str(stat.get("detail") or "").split(","):
        part = part.strip()
        if part.startswith(("드래곤 ", "바론 ", "타워 ")):
            metrics.append(re.sub(r"^(드래곤|바론|타워)\s+(\d+)$", r"\1 \2개", part))
        if len(metrics) >= 3:
            break

    properties = stat.get("properties") or {}
    picks = [
        str(properties.get(f"pick{i}") or "").strip()
        for i in range(1, 6)
    ]
    picks = [name for name in picks if name]

    opponent = str((opponent_fact or {}).get("source") or "").strip()
    sentence = f"{team}{_korean_particle(team, '은', '는')} "
    if opponent:
        sentence += f"{opponent}전에서 "
    else:
        sentence += "선별된 경기에서 "
    if picks:
        sentence += f"{'·'.join(picks)} 조합을 사용했고, "
    if metrics:
        sentence += f"{'·'.join(metrics)}를 기록했습니다"
    if won:
        sentence += ". 이 경기에서는 승리했습니다"
    else:
        sentence += "."
    return _limit_plain_text(sentence)


def call_hyperclova_presentation(
    question,
    evidence_bundle,
    *,
    qtype="relational",
    fallback_answers=None,
):
    """전체 검증 근거로 상세 해설을 만든 뒤, 별도 LLM 호출로 요약한다."""
    fallback_answers = fallback_answers or {}
    rdb_fallback = str(fallback_answers.get("rdb") or "").strip()
    graph_fallback = str(fallback_answers.get("graph") or "").strip()
    article_fallback = str(fallback_answers.get("article") or "").strip()
    fallback_source = (
        rdb_fallback
        if qtype == "numeric" or (evidence_bundle.get("scope", {}).get("rdb", {}).get("month"))
        else graph_fallback or rdb_fallback or article_fallback
    )
    strategy_focus = bool(evidence_bundle.get("scope", {}).get("strategy_focus"))
    strategy_chain_summary = (
        _strategy_chain_summary(evidence_bundle.get("summary_facts"))
        if strategy_focus and qtype == "relational" else ""
    )
    strategy_pick_names = (
        _strategy_pick_names(evidence_bundle.get("summary_facts"))
        if strategy_focus else []
    )
    fact_fallback = _summary_fallback_from_facts(evidence_bundle.get("summary_facts"))
    fallback_summary = _limit_plain_text(
        strategy_chain_summary or fact_fallback or _first_plain_sentences(fallback_source, 2)
        or "확인 가능한 근거가 부족합니다."
    )
    fallback_detail = "\n\n".join(
        f"[{label}]\n{text}"
        for label, text in (
            ("RDB 정형 데이터", rdb_fallback),
            ("지식그래프", graph_fallback),
            ("기사", article_fallback),
        )
        if text
    ) or fallback_summary
    if strategy_focus:
        fallback_summary = _strategy_summary_from_detail(
            fallback_detail, strategy_chain_summary or fallback_summary
        )

    source_keys = [
        key for key in ("rdb", "kg", "article") if evidence_bundle.get(key)
    ]
    detail_warnings = []
    detail_sources = source_keys
    detail_system = (
        "너는 LCK 데이터를 설명하는 검증 편집자다. 제공된 근거 밖의 사실을 만들지 않고 "
        "반드시 JSON 하나만 출력한다."
    )
    evidence_json = json.dumps(evidence_bundle, ensure_ascii=False, default=str)
    detail_user = f"""아래 검증 근거 전체를 확인한 뒤 질문에 직접 필요한 핵심만 골라 상세 해설을 작성해줘.

[사용자 질문]
{question}

[질문 유형]
{qtype}

[검증 근거 묶음]
{evidence_json}

공통 원칙:
- scope의 연도·월·팀 ID·선수 ID·로스터 유효기간을 반드시 지켜라.
- 요청한 월의 자료가 0건이면 다른 달이나 최신 경기로 대신하지 말고 자료가 없다고 말해라.
- 숫자·날짜·팀·선수는 evidence 안에 있는 값만 사용해라.
- RDB 수치가 있으면 월별 성적·승률·KDA·로스터 판단은 RDB를 우선해라.
- 지식그래프는 관계 설명, 기사는 맥락 설명에 사용하되 RDB 범위를 덮어쓰지 마라.
- 마크다운 기호, 제목 기호, 출처 번호를 본문에 쓰지 마라.
- summary_facts는 질문의 핵심을 표시할 뿐 전체 근거의 대체물이 아니다. rdb, kg, article 배열과
  scope.coverage를 확인하되, 같은 결론을 반복하는 근거와 경기별 나열은 본문에서 생략해라.
- 일부 경기 예시만으로 전체 기간의 공통 전략이라고 단정하지 말고, 반복 패턴과 경기별 차이,
  예외를 함께 설명해라.
- 관계형·전략 질문은 Team → Match → InferredStrategy → Evidence의 연결을 따라가되,
  그래프 모양 자체를 설명하는 문장으로 답을 대신하지 마라.
- HAS_INFERRED_STRATEGY는 각 Match의 기사와 정형 데이터를 검증해 만든 경기별 전략이다.
  서로 다른 Match의 전략을 하나의 공통 전략으로 합치지 말고 경기별 차이가 드러나게 답해라.
- Game 555, Match 209 같은 내부 노드 ID는 그대로 쓰지 말고 '해당 세트', '해당 경기'처럼
  독자가 이해할 수 있는 말로 바꿔라.
- 전략 질문의 시각화에서는 Champion 노드를 숨기지만, TEAM_GAME_STAT의 픽 조합·밴에
  들어 있는 챔피언명은 유효한 텍스트 근거다. 해당 근거가 있으면 대표 챔피언 2~3개 또는
  확인된 조합을 '선별된 경기에서 관측된 선택'으로 자연스럽게 언급해라. 별도 인과 관계가
  없으면 그 챔피언이 한타·승리·오브젝트 확보를 만들었다고 단정하지 말고, 근거에 없는
  챔피언도 추가하지 마라.

상세 해설 규칙:
- 근거 탭에서 빠르게 읽을 공백 포함 {DETAIL_MAX_CHARS}자 이내, 4~6문장 평문이다.
- 첫 문장에서 질문에 대한 결론을 직접 말한 뒤, 핵심 RDB 수치, KG의 반복·차이 패턴,
  필요한 기사 맥락과 한계만 설명해라.
- 같은 의미의 수치와 사례를 반복하지 말고, 경기 사례는 전체 결론을 보여주는 대표 1~2건만 사용해라.
- 적용 범위는 결과 해석에 꼭 필요할 때만 한 문장 안에서 짧게 밝히고 조회 과정이나 ID 필터는 나열하지 마라.
- '가장 좋은 달'처럼 기준이 필요한 질문은 승률·KDA·표본 경기 수 등 사용한 기준을 명시해라.
- 전략 질문에 픽 조합·밴 근거가 있으면 실제 챔피언명을 포함해 설명하고, 관측된 선택과
  승리 원인에 대한 해석을 구분해라.
- 추론 전략마다 기사 근거와 정형 근거가 각각 무엇을 지지하는지 구분하고, 신뢰도가 낮으면
  확정 사실이 아니라 관측된 경향임을 밝혀라.

반드시 다음 JSON 형식으로만 출력해라.
{{
  "detail": "상세 해설",
  "sources_used": ["rdb", "kg", "article"],
  "warnings": []
}}
"""

    detail_content, detail_error = _call_clova_chat(
        detail_system, detail_user, temperature=0.1, max_tokens=1536
    )
    try:
        if detail_error or not detail_content:
            raise ValueError(detail_error or "상세 해설 빈 응답")
        detail_result = parse_json_only(detail_content)
        detail = str(detail_result.get("detail") or "").strip()
        if not detail:
            raise ValueError("detail 누락")
        detail_sources = [
            source for source in detail_result.get("sources_used", [])
            if source in ("rdb", "kg", "article") and evidence_bundle.get(source)
        ]
        if not detail_sources:
            detail_sources = source_keys
        raw_warnings = detail_result.get("warnings", [])
        if not isinstance(raw_warnings, list):
            raw_warnings = [raw_warnings]
        detail_warnings = [str(w) for w in raw_warnings if str(w).strip()]
    except Exception as exc:
        print("DEBUG detail generation fallback:", exc)
        detail = fallback_detail
        detail_warnings = ["상세 해설 생성 실패로 검증된 출처별 답변을 사용했습니다."]

    # 모델 지시를 벗어나거나 출처별 fallback이 길어져도 UI에는 같은 상한을 적용한다.
    if strategy_pick_names:
        detail = _ensure_strategy_pick_mention(
            detail,
            strategy_pick_names,
            count=3,
            max_chars=DETAIL_MAX_CHARS,
        )
    else:
        detail = _limit_plain_text(detail, DETAIL_MAX_CHARS)

    # 요약은 반드시 완성된 상세 해설만 입력으로 받는 별도 LLM 단계다.
    summary_system = (
        "너는 상세 해설을 사용자 질문에 대한 짧은 결론으로 압축하는 편집자다. "
        "상세 해설에 없는 사실을 추가하지 않고 JSON 하나만 출력한다."
    )
    summary_user = f"""아래 상세 해설만 바탕으로 사용자 질문에 바로 답하는 요약을 작성해줘.

[사용자 질문]
{question}

[질문 유형]
{qtype}

[상세 해설]
{detail}

규칙:
- 상세 해설의 결론과 범위를 보존하고 새로운 사실·숫자·날짜·팀·선수를 추가하지 마라.
- 그래프, 조회 과정, 출처, 필터, ID, 근거 개수를 설명하지 마라.
- 첫 문장에서 질문에 직접 답하고, 공백 포함 200자 이내의 1~3문장 평문으로 써라.
- 전략 질문은 모든 경기를 같은 문구로 뭉개지 말고, 반복된 경향과 경기별 차이를 함께 압축해라.
- 내부 Game/Match 노드 ID는 쓰지 마라.

JSON 형식:
{{"summary":"공백 포함 200자 이내의 쉬운 요약"}}
"""
    summary_content, summary_error = _call_clova_chat(
        summary_system, summary_user, temperature=0.1, max_tokens=768
    )
    summary = ""
    try:
        if summary_error or not summary_content:
            raise ValueError(summary_error or "요약 빈 응답")
        summary_result = parse_json_only(summary_content)
        summary = _limit_plain_text(summary_result.get("summary"))
        if not summary:
            raise ValueError("summary 누락")

        supported_numbers = _number_values(" ".join(
            str(item.get("text") or "")
            for section in ("summary_facts", "rdb", "kg", "article")
            for item in evidence_bundle.get(section, [])
        ))
        unsupported_numbers = {
            value for value in (_number_values(summary) - supported_numbers)
            if value > 3
        }
        if unsupported_numbers:
            print("DEBUG presentation summary 숫자 검증 실패:", summary)
            raise ValueError(
                f"summary에 근거 없는 숫자 포함: {sorted(unsupported_numbers)}"
            )
    except Exception as exc:
        print("DEBUG summary generation fallback:", exc)
        summary = (
            strategy_chain_summary
            or _strategy_summary_from_detail(detail, fallback_summary)
            if strategy_focus
            else _detail_conclusion_summary(detail) or fallback_summary
        )

    if strategy_pick_names:
        summary = _ensure_strategy_pick_mention(
            summary,
            strategy_pick_names,
            count=2,
            max_chars=SUMMARY_MAX_CHARS,
        )
    summary = _limit_plain_text(summary)
    return {
        "summary": summary or fallback_summary,
        "detail": detail,
        "sources_used": list(dict.fromkeys(detail_sources)),
        "warnings": detail_warnings,
    }


def call_hyperclova_rdb_answer(question, rdb_context):
    """
    RDB(정형 통계) 사실만 바탕으로 질문에 답한다(파이프라인 RDB 단계).
    """
    if not rdb_context.strip():
        return (
            "RDB(정형 데이터)에서 질문과 직접 연결되는 통계를 찾지 못했습니다."
        )

    system = "너는 LCK e스포츠 통계 데이터(RDB) 기반 질의응답 도우미다."
    user = f"""아래 RDB 통계 사실만 바탕으로 사용자의 질문에 답변해줘.
이 사실들은 MySQL(espo)에서 조회한 선수/팀/경기 통계다.

답변 원칙:
- 반드시 [RDB 통계 사실]에 있는 수치만 근거로 사용해.
- 기사나 외부 지식을 추측해서 보태지 마.
- 사실이 부족하면 "RDB 통계만으로는 확인하기 어렵습니다"라고 말해.
- 승/패, KDA, 골드 같은 수치는 구체적으로 제시해.

[사용자 질문]
{question}

[RDB 통계 사실]
{rdb_context}
"""

    # HCX-007 추론 토큰 여유를 위해 넉넉히 잡는다.
    content, error = _call_clova_chat(system, user, temperature=0.1, max_tokens=2048)

    if error or not content:
        return "RDB 통계 답변 생성 중 오류가 발생했습니다."

    return content.strip()


def generate_sql_with_clova(question, schema_prompt, entity_hint=""):
    """
    질문을 MySQL SELECT 문 하나로 변환(Text-to-SQL).
    SQL 텍스트만 반환하며, 안전성 검증은 호출 측(rdb_query._sanitize_sql)에서 한다.
    """
    system = (
        "너는 자연어 질문을 MySQL SELECT 쿼리로 변환하는 변환기다. "
        "오직 SELECT 문 하나만, 설명 없이 출력한다."
    )
    user = f"""{schema_prompt}

{entity_hint}

위 스키마를 사용해서 아래 질문에 답할 수 있는 MySQL SELECT 쿼리를 딱 하나만 작성해줘.

규칙:
- SELECT(또는 WITH) 문 하나만 출력. 설명·주석·마크다운 금지.
- INSERT/UPDATE/DELETE/DROP 등 데이터 변경 구문 금지.
- 결과가 많을 수 있으면 LIMIT 을 붙여라.
- 예약어 match 테이블은 반드시 백틱 `match` 로 표기.

[질문]
{question}
"""

    # HCX-007은 추론(thinking) 모델이라 thinkingContent가 토큰을 먼저 소비한다.
    # 토큰이 부족하면 실제 SQL(content)이 비므로 충분히 크게 잡는다.
    content, error = _call_clova_chat(system, user, temperature=0.0, max_tokens=2048)

    if error or not content:
        print("DEBUG generate_sql 실패:", error)
        return ""

    return content.strip()


def call_hyperclova_kg_answer(question, graph_context):
    if not graph_context.strip():
        return (
            "지식그래프에서 질문과 직접 연결되는 관계를 찾지 못했습니다. "
            "먼저 Neo4j에 해당 선수, 팀, 경기, 대회 관계가 저장되어 있는지 확인해주세요."
        )

    prompt = f"""
아래 지식그래프 사실만 바탕으로 사용자의 질문에 답변해줘.
지식그래프 사실은 Neo4j에서 조회한 관계 triple이다.

답변 원칙:
- 반드시 [지식그래프 사실]에 있는 관계만 근거로 사용해.
- 기사 원문이나 외부 지식을 추측해서 보태지 마.
- 관계가 부족하면 "지식그래프에 있는 사실만으로는 확인하기 어렵습니다"라고 말해.
- 관계 이름이 영어/코드 형태이면 자연스러운 한국어로 풀어서 설명해.
- 노드 이름은 [지식그래프 사실]에 적힌 명사형 표기를 한 글자도 바꾸지 말고 그대로 사용해.
- 화면에 보이지 않는 엔티티나 관계를 새로 만들거나 추론하지 마.
- 핵심 관계가 드러나도록 3~5문장의 짧은 요약으로 답해.
- 이 시스템의 핵심은 지식그래프다. 단순 사실 나열이 아니라, 팀·선수·오브젝트·경기·
  전략이 서로 '어떻게 연결되고 이어지는지'(관계와 인과 흐름)를 이야기하듯 설명해.
  예: 어떤 오브젝트를 확보 → 그걸로 어떻게 이득을 굴렸는지 → 그 결과 승리, 선수들이
  어떤 관계로 엮여 결과에 기여했는지처럼 관계 구조가 드러나게 답해.
- 서로 연결된 2-hop 이상의 사실이 있으면 중간 노드를 생략하지 말고 경로 순서대로 설명해.
  1-hop 선수 명단만으로 전략 답변을 대신하지 마.
- 전략 관련 질문에서는 시각화용 Champion 노드가 없어도 TEAM_GAME_STAT의 픽 조합·밴에
  적힌 챔피언명은 '해당 경기에서 관측된 픽/밴'으로 답변에서 언급해. 다만 근거에 없는
  챔피언을 추측해 추가하거나 특정 챔피언이 승리를 만들었다고 단정하지 말고,
  TEAM_GAME_STAT 수치도 원인이 아닌 확인된 경기 경향으로 표현해.

[사용자 질문]
{question}

[지식그래프 사실]
{graph_context}
"""

    headers = {
        "Authorization": f"Bearer {CLOVA_API_KEY}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
    }

    body = {
        "messages": [
            {
                "role": "system",
                "content": "너는 LCK 지식그래프 기반 질의응답 도우미다."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.1,
        "topP": 0.8,
        "topK": 0,
        "maxCompletionTokens": 1024,
        "repetitionPenalty": 1.05
    }

    response = requests.post(
        CLOVA_API_URL,
        headers=headers,
        json=body,
        timeout=90
    )

    if response.status_code != 200:
        return f"HyperCLOVA 호출 중 오류가 발생했습니다: {response.text}"

    content = extract_content_from_clova_response(response.text)

    return content.strip()
