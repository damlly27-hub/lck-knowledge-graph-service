import os
from dotenv import load_dotenv
from neo4j import GraphDatabase

# 실행 위치(cwd)에 흔들리지 않도록 이 파일과 같은 폴더의 .env 를 명시적으로 읽는다.
# (uvicorn 을 다른 디렉터리에서 띄우면 load_dotenv() 가 .env 를 못 찾아 엉뚱한
#  기본 DB 로 붙던 사고를 재발 방지)
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH)

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

driver = GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USER, NEO4J_PASSWORD)
)

def get_driver():
    return driver

def get_database():
    return NEO4J_DATABASE


def neo4j_connected_db():
    """
    실제로 붙은 Neo4j DB 이름을 서버에 물어봐서 반환한다.
    - CALL db.info() 로 현재 세션이 실행되는 database 를 확인 (secret 미포함)
    - 반환: (configured_db, connected_db)
    설정값(configured)과 실제 접속(connected)이 다르면 .env / 세션 database 지정을
    다시 봐야 한다는 신호다.
    """
    configured = get_database()
    with driver.session(database=configured) as session:
        rec = session.run("CALL db.info() YIELD name RETURN name").single()
        connected = rec["name"] if rec else None
    return configured, connected
