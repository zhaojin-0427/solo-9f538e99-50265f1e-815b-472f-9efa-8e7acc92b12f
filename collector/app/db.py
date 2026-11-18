"""SQLite 存储层。

关键不变式:
- 某一版的完整快照(entries)+ 版本登记(versions)+ 当前指针
  (hours.current_generation / hours.current_version)在同一个事务里提交,
  因此失败或重启都不会暴露"半版"数据。
- 未确认版本的页面不落库,采集中的残页只存在于内存,重试时整版重取。
- 代际(generation)是快照的一级维度:不同代际的同名版号互不叠加,
  旧代际已确认版始终可按 (generation, version) 查询。
- 旧版库(无 generation 列)在启动时自动迁移:已确认数据划入 'legacy'
  代际保留可查;此后采集按源站真实代际全量重建,绝不与 legacy 混代。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/collector.db")

# 旧库迁移时,已确认数据划入的代际标识
LEGACY_GENERATION = "legacy"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hours (
    hour                TEXT PRIMARY KEY,           -- UTC 小时,如 2026-11-18T10
    current_generation  TEXT,                       -- 当前代际;NULL 表示尚无确认版
    current_version     INTEGER NOT NULL DEFAULT 0, -- 当前版指针(当前代际内)
    status              TEXT NOT NULL DEFAULT 'idle',
    pinned_generation   TEXT,                       -- 本次采集固定的源站代际
    pinned_version      INTEGER,                    -- 本次采集固定的目标版
    last_error          TEXT,
    updated_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    hour           TEXT NOT NULL,
    generation     TEXT NOT NULL,
    version        INTEGER NOT NULL,
    total_pages    INTEGER NOT NULL,
    item_count     INTEGER NOT NULL,
    station_count  INTEGER NOT NULL,
    confirmed_at   TEXT NOT NULL,
    PRIMARY KEY (hour, generation, version)
);
CREATE TABLE IF NOT EXISTS entries (
    hour       TEXT NOT NULL,
    generation TEXT NOT NULL,
    version    INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    pm25       REAL NOT NULL,
    PRIMARY KEY (hour, generation, version, station_id)
);
"""

_CREATE_VERSIONS = """
CREATE TABLE versions (
    hour           TEXT NOT NULL,
    generation     TEXT NOT NULL,
    version        INTEGER NOT NULL,
    total_pages    INTEGER NOT NULL,
    item_count     INTEGER NOT NULL,
    station_count  INTEGER NOT NULL,
    confirmed_at   TEXT NOT NULL,
    PRIMARY KEY (hour, generation, version)
)
"""

_CREATE_ENTRIES = """
CREATE TABLE entries (
    hour       TEXT NOT NULL,
    generation TEXT NOT NULL,
    version    INTEGER NOT NULL,
    station_id TEXT NOT NULL,
    pm25       REAL NOT NULL,
    PRIMARY KEY (hour, generation, version, station_id)
)
"""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)  # 自动提交,显式事务见 tx()
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _migrate_legacy(db: sqlite3.Connection) -> None:
    """旧版库(无 generation 列)就地升级,已确认数据划入 legacy 代际。"""
    hours_cols = _columns(db, "hours")
    if not hours_cols or "current_generation" in hours_cols:
        return  # 全新库或已是新结构
    with tx(db):
        db.execute("ALTER TABLE hours ADD COLUMN current_generation TEXT")
        db.execute("ALTER TABLE hours ADD COLUMN pinned_generation TEXT")
        db.execute("ALTER TABLE versions RENAME TO versions_legacy")
        db.execute("ALTER TABLE entries RENAME TO entries_legacy")
        db.execute(_CREATE_VERSIONS)
        db.execute(_CREATE_ENTRIES)
        db.execute(
            "INSERT INTO versions(hour, generation, version, total_pages, item_count, "
            "station_count, confirmed_at) "
            "SELECT hour, ?, version, total_pages, item_count, station_count, confirmed_at "
            "FROM versions_legacy",
            (LEGACY_GENERATION,),
        )
        db.execute(
            "INSERT INTO entries(hour, generation, version, station_id, pm25) "
            "SELECT hour, ?, version, station_id, pm25 FROM entries_legacy",
            (LEGACY_GENERATION,),
        )
        db.execute("DROP TABLE versions_legacy")
        db.execute("DROP TABLE entries_legacy")
        db.execute(
            "UPDATE hours SET current_generation=? WHERE current_version > 0",
            (LEGACY_GENERATION,),
        )


def init() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    db = connect()
    try:
        _migrate_legacy(db)  # 旧库先升级,再确保新结构存在
        db.executescript(SCHEMA)
        # 重启清理:把中断在 collecting 的小时复位为 idle。
        # 数据本身由事务保证完整,这里只修正状态元信息。
        db.execute("UPDATE hours SET status='idle' WHERE status='collecting'")
    finally:
        db.close()


@contextmanager
def tx(db: sqlite3.Connection):
    """显式事务:要么整体提交,要么整体回滚。"""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except Exception:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def ensure_hour(db: sqlite3.Connection, hour: str, now: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO hours(hour, current_version, status, updated_at) "
        "VALUES (?, 0, 'idle', ?)",
        (hour, now),
    )


def get_hour(db: sqlite3.Connection, hour: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM hours WHERE hour=?", (hour,)).fetchone()


def snapshot(db: sqlite3.Connection, hour: str, generation: str, version: int) -> dict[str, float]:
    rows = db.execute(
        "SELECT station_id, pm25 FROM entries WHERE hour=? AND generation=? AND version=?",
        (hour, generation, version),
    ).fetchall()
    return {r["station_id"]: r["pm25"] for r in rows}
