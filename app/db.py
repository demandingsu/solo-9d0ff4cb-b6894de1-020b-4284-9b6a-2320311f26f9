"""SQLite 连接管理与建表脚本。

设计要点
========
* 所有连接开启 WAL、外键与 ``foreign_key_cascade``，并把
  ``busy_timeout`` 调到 30s：并发导入时写事务串行化，等待方不会立即报错。
* 每个操作使用独立短连接（上下文管理器自动提交/回滚），查询以
  ``BEGIN`` 开启只读快照事务，保证一次查询内价量与事件依据来自
  *同一个截止批次下已提交* 的状态。
* 行情/公告均为 *只追加* 表，修订号与业务键上的唯一索引保证
  「同号仅高修订生效、同修订异值拒收」。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS securities (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 入库批次：每次成功导入生成一行，自增 id 即批次号（提交顺序）
CREATE TABLE IF NOT EXISTS ingest_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_kind    TEXT NOT NULL CHECK (payload_kind IN ('quotes', 'corporate_actions', 'trade_packet')),
    row_count       INTEGER NOT NULL,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 日行情（只追加，修订版本化）
CREATE TABLE IF NOT EXISTS daily_quotes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    security_code    TEXT NOT NULL REFERENCES securities(code),
    trade_date       TEXT NOT NULL,
    revision         INTEGER NOT NULL CHECK (revision >= 0),
    open             REAL NOT NULL,
    high             REAL NOT NULL,
    low              REAL NOT NULL,
    close            REAL NOT NULL,
    volume           REAL NOT NULL,
    ingest_batch_id  INTEGER NOT NULL REFERENCES ingest_batches(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (security_code, trade_date, revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_sec_date
    ON daily_quotes (security_code, trade_date);

-- 拆股 / 并股公告（只追加，修订版本化）
-- ratio = 每股老股拆成的新股数：1 拆 2 => 2.0；2 并 1 => 0.5
CREATE TABLE IF NOT EXISTS corporate_actions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    security_code    TEXT NOT NULL REFERENCES securities(code),
    event_no         TEXT NOT NULL,
    ex_date          TEXT NOT NULL,
    ratio            REAL NOT NULL CHECK (ratio > 0),
    action_type      TEXT NOT NULL DEFAULT 'split'
        CHECK (action_type IN ('split', 'reverse_split')),
    revision         INTEGER NOT NULL CHECK (revision >= 0),
    ingest_batch_id  INTEGER NOT NULL REFERENCES ingest_batches(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (security_code, event_no, revision)
);

CREATE INDEX IF NOT EXISTS idx_actions_sec_exdate
    ON corporate_actions (security_code, ex_date);

-- 逐笔事件（只追加）：成交回报与撤销回报，业务键 (证券, 交易日, 成交号, 类别)
-- 撤销是“墓碑”：无论撤销先到还是成交先到，该成交号一律不计入汇总；
-- 同号成交重传必须同值（幂等跳过），异值在业务层整包退回。
CREATE TABLE IF NOT EXISTS trade_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    security_code    TEXT NOT NULL REFERENCES securities(code),
    trade_date       TEXT NOT NULL,
    trade_no         INTEGER NOT NULL CHECK (trade_no >= 0),
    kind             TEXT NOT NULL CHECK (kind IN ('trade', 'revoke')),
    trade_time       TEXT,           -- 归一化 UTC：YYYY-MM-DDTHH:MM:SS.ffffffZ（仅成交）
    price            REAL,           -- 仅成交
    quantity         REAL,           -- 仅成交
    minute_ts        TEXT,           -- 成交时间截到分钟：YYYY-MM-DDTHH:MM:00Z（仅成交）
    ingest_batch_id  INTEGER NOT NULL REFERENCES ingest_batches(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (security_code, trade_date, trade_no, kind)
);

CREATE INDEX IF NOT EXISTS idx_trade_events_minute
    ON trade_events (security_code, trade_date, minute_ts);

-- 分钟线物化结果：由 trade_events 推导，补包事务内只重算受影响分钟，
-- 被清空的分钟整行删除。日线在查询时由分钟线聚合，两级天然来自同一快照。
CREATE TABLE IF NOT EXISTS minute_bars (
    security_code    TEXT NOT NULL,
    trade_date       TEXT NOT NULL,
    minute_ts        TEXT NOT NULL,
    open             REAL NOT NULL,
    high             REAL NOT NULL,
    low              REAL NOT NULL,
    close            REAL NOT NULL,
    volume           REAL NOT NULL,
    trade_count      INTEGER NOT NULL,
    updated_batch_id INTEGER NOT NULL REFERENCES ingest_batches(id),
    PRIMARY KEY (security_code, trade_date, minute_ts)
);
"""

# 旧库迁移用：带 trade_packet 的 ingest_batches 建表语句（与 SCHEMA 中一致）
_BATCHES_DDL = """
CREATE TABLE ingest_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_kind    TEXT NOT NULL CHECK (payload_kind IN ('quotes', 'corporate_actions', 'trade_packet')),
    row_count       INTEGER NOT NULL,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def connect(db_file: str | None = None) -> sqlite3.Connection:
    """打开一个带 WAL/外键/超时设置的连接，Row 工厂便于按列名取值。"""
    path = db_file or config.db_path()
    config.ensure_parent_dir(path)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # 快照隔离：读事务期间不会看到别的连接后续提交的写入
    try:
        conn.execute("PRAGMA read_uncommitted = 0")
    except sqlite3.DatabaseError:
        pass
    return conn


@contextmanager
def get_conn(db_file: str | None = None) -> Iterator[sqlite3.Connection]:
    """短连接上下文：正常退出自动关闭，异常自动回滚。"""
    conn = connect(db_file)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_file: str | None = None) -> None:
    """建表（幂等），应用启动时调用；旧库自动迁移 ingest_batches 的 CHECK。"""
    with get_conn(db_file) as conn:
        conn.executescript(SCHEMA)
        _migrate_ingest_batches(conn)


def _migrate_ingest_batches(conn: sqlite3.Connection) -> None:
    """旧库的 ingest_batches CHECK 不含 'trade_packet'，需重建该表放行补包批次。

    SQLite 不能改 CHECK，只能换名重建；legacy_alter_table 避免重命名时
    改写子表外键引用，id/批次号原样保留。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ingest_batches'"
    ).fetchone()
    if row is None or "'trade_packet'" in row["sql"]:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        with write_transaction(conn):
            conn.execute("ALTER TABLE ingest_batches RENAME TO ingest_batches_old")
            conn.execute(_BATCHES_DDL)
            conn.execute(
                "INSERT INTO ingest_batches(id, payload_kind, row_count, imported_at) "
                "SELECT id, payload_kind, row_count, imported_at FROM ingest_batches_old"
            )
            conn.execute("DROP TABLE ingest_batches_old")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式立即写事务：一开始就拿写锁，避免并发导入在提交时才冲突。

    任何异常都会回滚——失败的导入不会留下任何数据，也不会产生批次。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读快照事务：查询全程看到同一已提交快照（deferred 首个读语句开始）。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # 只读事务，回滚即结束，不改动任何数据
