# -*- coding: utf-8 -*-
"""SQLAlchemy engine / session 管理"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import config

engine = create_engine(
    config.DB_URL,
    pool_size=5,
    max_overflow=5,
    pool_recycle=3600,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def check_connection() -> bool:
    """返回 DB 是否可连通"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def init_schema(schema_path: str) -> None:
    """执行 schema.sql 建表（幂等，IF NOT EXISTS）"""
    with open(schema_path, "r", encoding="utf-8") as f:
        raw = f.read()
    statements = [s.strip() for s in raw.split(";") if s.strip() and not s.strip().startswith("--")]
    # 按语句逐条执行，跳过纯注释段
    with engine.begin() as conn:
        for stmt in raw.split(";"):
            sql = stmt.strip()
            if not sql:
                continue
            # 去掉行内注释行
            lines = [l for l in sql.splitlines() if not l.strip().startswith("--")]
            sql = "\n".join(lines).strip()
            if sql:
                conn.execute(text(sql))
