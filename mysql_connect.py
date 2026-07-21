"""
MySQL(espo) 연결 헬퍼.

RDB(정형 데이터)는 선수/팀/경기/게임/스탯 등 수치·집계 질문의 근거가 된다.
이 모듈은 연결만 담당하고, 질의 로직은 rdb_query.py 에서 처리한다.
"""

import os
import pymysql
from dotenv import load_dotenv

# 실행 위치(cwd)에 흔들리지 않도록 이 파일과 같은 폴더의 .env 를 명시적으로 읽는다.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH)

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "espo")


def get_connection():
    """
    espo DB 연결을 새로 만들어 반환한다.
    - utf8mb4 로 한글 팀/선수명 깨짐 방지
    - DictCursor 로 컬럼명 기반 접근
    호출 측에서 반드시 close() 해야 한다.
    """
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


def mysql_connected_db():
    """
    실제로 붙은 MySQL DB 이름을 서버에 물어봐서 반환한다.
    - SELECT DATABASE() 로 현재 연결의 default schema 확인 (secret 미포함)
    - 반환: (configured_db, connected_db)
    """
    configured = MYSQL_DATABASE
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE() AS db")
            row = cur.fetchone()
            connected = row["db"] if row else None
    finally:
        conn.close()
    return configured, connected


if __name__ == "__main__":
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        tables = [list(row.values())[0] for row in cur.fetchall()]
    conn.close()
    print(f"espo 연결 성공. 테이블 {len(tables)}개:", tables)
