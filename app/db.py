"""SQLite 数据访问层。

并发策略：
- 单连接 + 进程内可重入锁，所有读写串行化，避免 SQLite 写锁竞争；
- WAL 模式 + busy_timeout 兜底；
- 写操作一律走 write_tx()（BEGIN IMMEDIATE ... COMMIT/ROLLBACK），
  保证并发补传、修订、发布互不丢失已提交记录；
- readings 表 (device_id, seq) 唯一约束作为幂等的最终防线。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    rise_threshold  REAL NOT NULL,
    clear_threshold REAL NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_no  TEXT NOT NULL UNIQUE,
    station_id INTEGER NOT NULL REFERENCES stations(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id            INTEGER NOT NULL REFERENCES devices(id),
    seq                  INTEGER NOT NULL,
    observed_at          TEXT NOT NULL,   -- 当前生效值（UTC ISO8601）
    water_level          REAL NOT NULL,   -- 当前生效值
    original_observed_at TEXT NOT NULL,   -- 首次提交的原值
    original_water_level REAL NOT NULL,
    status               TEXT NOT NULL DEFAULT 'active',  -- active | voided
    version              INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (device_id, seq)
);

CREATE TABLE IF NOT EXISTS reading_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id      INTEGER NOT NULL REFERENCES readings(id),
    revision_no     INTEGER NOT NULL,
    action          TEXT NOT NULL,        -- correct | void
    old_observed_at TEXT,
    old_water_level REAL,
    new_observed_at TEXT,
    new_water_level REAL,
    reason          TEXT NOT NULL,
    auditor         TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threshold_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      INTEGER NOT NULL REFERENCES stations(id),
    effective_at    TEXT NOT NULL,   -- 生效时间（UTC ISO8601），同一时刻采用新版本
    rise_threshold  REAL NOT NULL,   -- 生效上升阈值（有限值）
    clear_threshold REAL NOT NULL,   -- 生效解除阈值（有限值，严格小于上升阈值）
    reason          TEXT NOT NULL,   -- 调整原因
    created_at      TEXT NOT NULL,
    UNIQUE (station_id, effective_at)  -- 同站生效时刻唯一（含不同时区表示的同一时刻）
);

CREATE TABLE IF NOT EXISTS publications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    published_at TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS publication_timelines (
    publication_id INTEGER NOT NULL REFERENCES publications(id),
    station_id     INTEGER NOT NULL REFERENCES stations(id),
    timeline_json  TEXT NOT NULL,
    PRIMARY KEY (publication_id, station_id)
);

CREATE TABLE IF NOT EXISTS zones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,                -- 责任区名称（非空，允许同名）
    effective_at  TEXT NOT NULL,                -- 生效时间（UTC ISO8601，登记时必带时区）
    trigger_count INTEGER NOT NULL,             -- 触发站数 K：1 <= K <= 成员数
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zone_members (
    zone_id    INTEGER NOT NULL REFERENCES zones(id),
    station_id INTEGER NOT NULL REFERENCES stations(id),
    position   INTEGER NOT NULL,                -- 登记时的列表顺序（0 起）
    PRIMARY KEY (zone_id, station_id)
);

CREATE TABLE IF NOT EXISTS publication_zones (
    publication_id INTEGER NOT NULL REFERENCES publications(id),
    zone_id        INTEGER NOT NULL REFERENCES zones(id),
    timeline_json  TEXT NOT NULL,               -- 发布时冻结的区域联动时间线
    PRIMARY KEY (publication_id, zone_id)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE，异常自动回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()
