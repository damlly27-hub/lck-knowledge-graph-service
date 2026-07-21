# LCK Knowledge Graph QA

LCK(League of Legends Champions Korea) 데이터를 기반으로 자연어 질문에 답하는 지식그래프 Q&A 서비스입니다.
질문을 던지면 **지식그래프(Neo4j) · 정형 통계(MySQL) · 기사 원문**을 함께 검색하고, HyperCLOVA X로 답변을 생성한 뒤
근거가 된 그래프를 시각화해 보여줍니다.

## 구성

```
E_SPO/
├── api_server.py        # FastAPI 진입점 (/ask, /node, /health/db)
├── pipeline.py          # 질의응답 오케스트레이터 (KG + RDB + 기사 통합)
├── clova_extract.py     # HyperCLOVA X 호출 (의도분석·답변·요약)
├── neo4j_query.py       # Neo4j 지식그래프 검색
├── rdb_query.py         # MySQL 정형 데이터/통계 검색
├── article_search.py    # 기사 의미 기반 검색 (임베딩)
├── connect.py           # Neo4j 연결
├── mysql_connect.py     # MySQL(espo) 연결
├── requirements.txt
└── web/                 # Next.js 프론트엔드 (그래프 시각화 대시보드)
```

## 동작 흐름

```
질문
 └→ [LLM] 의도·키워드 분석
      ├→ MySQL 기사 검색      → 기사 기반 답변
      ├→ Neo4j 관계 검색      → 그래프 기반 답변 + 그래프 데이터
      └→ MySQL 통계 검색      → 정형 데이터 기반 답변
           └→ [LLM] 세 답변 통합 → 최종 답변 + 근거 그래프
```

## 요구 사항

- Python 3.10+
- Node.js 18+
- Neo4j, MySQL 인스턴스
- HyperCLOVA X (CLOVA Studio) API 키

## 환경 변수 (`.env`)

프로젝트 루트에 `.env` 파일을 만듭니다. (git에 포함되지 않음)

```env
# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password
NEO4J_DATABASE=neo4j

# MySQL
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=espo

# HyperCLOVA X
CLOVA_API_KEY=your_api_key
CLOVA_APP_TYPE=testapp
CLOVA_MODEL=HCX-007
```

## 실행

### 백엔드 (FastAPI)

```bash
pip install -r requirements.txt
uvicorn api_server:app --reload --port 8000
```

- 헬스체크: `GET http://127.0.0.1:8000/health/db` — 설정된 DB와 실제 접속 DB가 일치하는지 확인

### 프론트엔드 (Next.js)

```bash
cd web
npm install
npm run dev
```

브라우저에서 `http://localhost:3000` 접속. (백엔드 주소는 `NEXT_PUBLIC_API_URL`로 변경 가능, 기본값 `http://127.0.0.1:8000`)

## 주요 API

| Method | Path         | 설명 |
|--------|--------------|------|
| POST   | `/ask`       | 질문(`{ "question": "..." }`)을 받아 통합 답변 + 근거 그래프 반환 |
| GET    | `/node`      | 그래프에서 클릭한 노드의 이웃 관계 상세 반환 |
| GET    | `/health/db` | Neo4j / MySQL 연결 상태 확인 |
