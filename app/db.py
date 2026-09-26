"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL


def configure_sqlite(engine: Engine) -> None:
    """为 SQLite 引擎开启 WAL 与 busy_timeout，使并发认领在写锁上等待而非立即失败。"""
    if engine.url.drivername != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
configure_sqlite(engine)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """执行确定性的业务处理。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
