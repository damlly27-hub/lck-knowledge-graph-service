import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pipeline import run_pipeline
from neo4j_query import get_node_detail
from connect import neo4j_connected_db
from mysql_connect import mysql_connected_db

logger = logging.getLogger("api_server")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
class QuestionRequest(BaseModel):
    question: str


@app.on_event("startup")
def _log_db_connections():
    """
    서버 시작 시 실제로 어떤 DB 에 붙었는지 로그로 남긴다.
    - MySQL: SELECT DATABASE()
    - Neo4j: CALL db.info()
    엉뚱한 DB(기본 neo4j / 다른 schema)에 붙는 사고를 부팅 시점에 바로 잡기 위함.
    """
    try:
        mysql_conf, mysql_conn = mysql_connected_db()
        logger.info("MySQL configured=%s connected=%s", mysql_conf, mysql_conn)
        print(f"[startup] MySQL  configured={mysql_conf}  connected={mysql_conn}")
    except Exception as e:  # 연결 실패해도 서버 부팅은 막지 않는다
        logger.error("MySQL connection check failed: %s", e)
        print(f"[startup] MySQL  connection check FAILED: {e}")

    try:
        neo_conf, neo_conn = neo4j_connected_db()
        logger.info("Neo4j configured=%s connected=%s", neo_conf, neo_conn)
        print(f"[startup] Neo4j  configured={neo_conf}  connected={neo_conn}")
    except Exception as e:
        logger.error("Neo4j connection check failed: %s", e)
        print(f"[startup] Neo4j  connection check FAILED: {e}")


@app.get("/")
def home():
    return {"message": "LCK Knowledge Graph API is running"}


@app.get("/health/db")
def health_db():
    """
    설정된 DB(configured)와 실제 접속된 DB(connected)를 secret 없이 반환한다.
    두 값이 다르거나 status 가 error 면 .env / 세션 database 지정을 점검해야 한다.
    """
    result = {}

    try:
        mysql_conf, mysql_conn = mysql_connected_db()
        result["mysql"] = {
            "configured": mysql_conf,
            "connected": mysql_conn,
            "match": mysql_conf == mysql_conn,
            "status": "ok",
        }
    except Exception as e:
        result["mysql"] = {"status": "error", "error": str(e)}

    try:
        neo_conf, neo_conn = neo4j_connected_db()
        result["neo4j"] = {
            "configured": neo_conf,
            "connected": neo_conn,
            "match": neo_conf == neo_conn,
            "status": "ok",
        }
    except Exception as e:
        result["neo4j"] = {"status": "error", "error": str(e)}

    ok = all(v.get("status") == "ok" and v.get("match") for v in result.values())
    result["ok"] = ok
    return result


@app.get("/node")
def node_detail(name: str):
    """그래프에서 클릭한 노드의 전체 이웃을 관계별 그룹 + 미니그래프로 반환."""
    return get_node_detail(name)


@app.post("/ask")
def ask(req: QuestionRequest):
    result = run_pipeline(req.question)

    return {
        "question": result["question"],
        "intent": result["intent"],
        "qtype": result.get("qtype", ""),
        "season_scoped": result.get("season_scoped", True),
        "graph_keyword": result["keywords"],
        "answer": result["answer"],
        "summary_answer": result["summary_answer"],
        "detailed_answer": result["detailed_answer"],
        "article_answer": result["article_answer"],
        "graph_answer": result["graph_answer"],
        "rdb_answer": result["rdb_answer"],
        "answer_source": "+".join(result.get("sources_used", [])) or "none",
        "sources_used": result.get("sources_used", []),
        "warnings": result.get("warnings", []),
        "related_chunks": result["graph_context"].splitlines(),
        "article_chunks": result["article_chunks"],
        "article_evidence": result["article_evidence"],
        "analysis_article_count": result.get(
            "analysis_article_count", len(result["article_evidence"])
        ),
        "graph_facts": result["graph_facts"],
        "kg_keywords": result["kg_keywords"],
        "graph": result["graph"],
        "display_graph_meta": result.get("display_graph_meta", {}),
        "rdb_facts": result["rdb_facts"],
        "rdb_entities": result["rdb_entities"],
        "rdb_entity_ids": result.get("rdb_entity_ids", {}),
        "rdb_scope": result.get("rdb_scope", {}),
        "rdb_evidence": result.get("rdb_evidence", []),
        "rdb_total_facts": result.get("rdb_total_facts", len(result["rdb_facts"])),
        "rdb_truncated": result.get("rdb_truncated", False),
        "rdb_sql": result["rdb_sql"],
        "evidence_bundle": result.get("evidence_bundle", {}),
        "strategy_insights": result.get("strategy_insights", []),
    }
