"""SQLite 存储：批次、采信、样本、隔离、重复、网格、窗口、对账。

数值列一律用 TEXT 存 Decimal 字面量，避免浮点往返丢精度；
时间列一律存 UTC ISO8601 秒级字符串。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Sequence

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    run_key       TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    rule_version  TEXT NOT NULL,
    sample_name   TEXT NOT NULL,
    offset_name   TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    input_json    TEXT NOT NULL,
    counts_json   TEXT NOT NULL,
    totals_json   TEXT NOT NULL,
    elapsed_ms    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS intake (
    run_id     TEXT NOT NULL,
    encoding   TEXT NOT NULL,
    bom        INTEGER NOT NULL,
    delimiter  TEXT NOT NULL,
    header_json TEXT NOT NULL,
    segments   INTEGER NOT NULL,
    blank_lines INTEGER NOT NULL,
    truncated  INTEGER NOT NULL,
    byte_size  INTEGER NOT NULL,
    sha256     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offsets (
    run_id     TEXT NOT NULL,
    sensor_id  TEXT NOT NULL,
    start_utc  TEXT NOT NULL,
    end_utc    TEXT NOT NULL,
    seconds    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    run_id     TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    sensor_id  TEXT NOT NULL,
    bucket_utc TEXT NOT NULL,
    value      TEXT NOT NULL,
    unit       TEXT NOT NULL,
    quality    TEXT NOT NULL,
    quality_rank INTEGER NOT NULL,
    ingest_seq INTEGER NOT NULL,
    offset_seconds INTEGER NOT NULL,
    late       INTEGER NOT NULL,
    flags      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
    run_id     TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    sensor_id  TEXT,
    column_name TEXT,
    raw_value  TEXT,
    code       TEXT NOT NULL,
    label      TEXT NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS duplicates (
    run_id     TEXT NOT NULL,
    sensor_id  TEXT NOT NULL,
    bucket_utc TEXT NOT NULL,
    kept_row   INTEGER NOT NULL,
    dropped_row INTEGER NOT NULL,
    kept_value TEXT NOT NULL,
    dropped_value TEXT NOT NULL,
    kept_quality TEXT NOT NULL,
    dropped_quality TEXT NOT NULL,
    code       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grid (
    run_id     TEXT NOT NULL,
    sensor_id  TEXT NOT NULL,
    bucket_utc TEXT NOT NULL,
    value      TEXT,
    unit       TEXT NOT NULL,
    state      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    fill_policy TEXT NOT NULL,
    source_row INTEGER
);

CREATE TABLE IF NOT EXISTS windows (
    run_id     TEXT NOT NULL,
    sensor_id  TEXT NOT NULL,
    window_start TEXT NOT NULL,
    observed   INTEGER NOT NULL,
    expected   INTEGER NOT NULL,
    coverage   TEXT NOT NULL,
    state      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconcile (
    run_id     TEXT NOT NULL,
    line_no    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    expected   TEXT NOT NULL,
    actual     TEXT NOT NULL,
    delta      TEXT NOT NULL,
    state      TEXT NOT NULL,
    note       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine(run_id);
CREATE INDEX IF NOT EXISTS idx_grid_run ON grid(run_id);
CREATE INDEX IF NOT EXISTS idx_windows_run ON windows(run_id);
CREATE INDEX IF NOT EXISTS idx_reconcile_run ON reconcile(run_id);
CREATE INDEX IF NOT EXISTS idx_duplicates_run ON duplicates(run_id);
"""

# ---------------------------------------------------------------------------
# M8 夜班滚动发布：原始补批（只追加）+ 内容寻址窗口版本 + 原子发布。
# 所有版本与原始行只追加不更新不删除；「当前指针」是唯一会被改写的表，且改写与
# 发布登记在同一事务里，中断即回滚，旧版本始终可读。
# ---------------------------------------------------------------------------
PUBLISH_SCHEMA = """
CREATE TABLE IF NOT EXISTS pub_batches (
    batch_id     TEXT PRIMARY KEY,
    batch_key    TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    byte_size    INTEGER NOT NULL,
    received_at  TEXT NOT NULL,
    raw_rows     INTEGER NOT NULL,
    accepted     INTEGER NOT NULL,
    duplicates   INTEGER NOT NULL,
    quarantined  INTEGER NOT NULL,
    late_rows    INTEGER NOT NULL,
    is_late_batch INTEGER NOT NULL
);

-- 原始记录只追加：(补批, 批内原始行号) 唯一，重放同批撞唯一键而不是新增。
CREATE TABLE IF NOT EXISTS pub_raw (
    batch_id     TEXT NOT NULL,
    source_row   INTEGER NOT NULL,
    sensor_id    TEXT,
    sampled_at   TEXT,
    raw_line     TEXT NOT NULL,
    PRIMARY KEY (batch_id, source_row)
);

-- 去重隔离后实际采信的采样，跨补批全局对齐；同一 (传感器,桶) 只保留全序最优一条。
CREATE TABLE IF NOT EXISTS pub_samples (
    batch_id     TEXT NOT NULL,
    arrival_seq  INTEGER NOT NULL PRIMARY KEY,
    source_row   INTEGER NOT NULL,
    sensor_id    TEXT NOT NULL,
    bucket_utc   TEXT NOT NULL,
    value        TEXT NOT NULL,
    unit         TEXT NOT NULL,
    quality      TEXT NOT NULL,
    quality_rank INTEGER NOT NULL,
    offset_seconds INTEGER NOT NULL,
    late         INTEGER NOT NULL,
    flags        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pub_samples_bucket ON pub_samples(sensor_id, bucket_utc);
CREATE INDEX IF NOT EXISTS idx_pub_samples_batch ON pub_samples(batch_id);

-- 同分钟被淘汰的重复行，留双方批次/行号，原始记录仍在 pub_raw。
CREATE TABLE IF NOT EXISTS pub_duplicates (
    batch_id     TEXT NOT NULL,
    sensor_id    TEXT NOT NULL,
    bucket_utc   TEXT NOT NULL,
    kept_seq     INTEGER NOT NULL,
    dropped_seq  INTEGER NOT NULL,
    kept_value   TEXT NOT NULL,
    dropped_value TEXT NOT NULL,
    kept_quality TEXT NOT NULL,
    dropped_quality TEXT NOT NULL,
    code         TEXT NOT NULL
);

-- 质量/时基不通过的隔离行。
CREATE TABLE IF NOT EXISTS pub_quarantine (
    batch_id     TEXT NOT NULL,
    source_row   INTEGER NOT NULL,
    sensor_id    TEXT,
    column_name  TEXT,
    raw_value    TEXT,
    code         TEXT NOT NULL,
    detail       TEXT
);

-- 一次原子发布。
CREATE TABLE IF NOT EXISTS pub_publishes (
    publish_id   TEXT PRIMARY KEY,
    publish_seq  INTEGER NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    cutoff_utc   TEXT NOT NULL,
    basis_batches_json TEXT NOT NULL,
    input_digest_json TEXT NOT NULL,
    window_count INTEGER NOT NULL,
    new_versions INTEGER NOT NULL,
    revised_versions INTEGER NOT NULL,
    unchanged_versions INTEGER NOT NULL,
    cells        INTEGER NOT NULL,
    totals_json  TEXT NOT NULL,
    note         TEXT NOT NULL
);

-- 内容寻址的窗口版本：业务指纹相同即同一版本，迟到补批重放不会新增版本。
CREATE TABLE IF NOT EXISTS pub_window_versions (
    version_id   TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    sensor_id    TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    first_publish_seq INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    observed     INTEGER NOT NULL,
    filled       INTEGER NOT NULL,
    gaps         INTEGER NOT NULL,
    expected     INTEGER NOT NULL,
    coverage     TEXT NOT NULL,
    state        TEXT NOT NULL,
    observed_sum TEXT NOT NULL,
    filled_sum   TEXT NOT NULL,
    UNIQUE (window_start, sensor_id, fingerprint)
);

-- 版本下的逐格快照（不可变），带采信批次/原始行号，回答「这次发布用了哪些采样」。
CREATE TABLE IF NOT EXISTS pub_window_cells (
    version_id   TEXT NOT NULL,
    window_start TEXT NOT NULL,
    sensor_id    TEXT NOT NULL,
    bucket_utc   TEXT NOT NULL,
    value        TEXT,
    unit         TEXT NOT NULL,
    state        TEXT NOT NULL,
    reason       TEXT NOT NULL,
    source_batch TEXT,
    source_row   INTEGER,
    arrival_seq  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pub_cells_version ON pub_window_cells(version_id);
CREATE INDEX IF NOT EXISTS idx_pub_cells_window ON pub_window_cells(window_start, sensor_id);

-- 发布 × 窗口版本 的多对多登记，一次发布引用的全部版本快照。
CREATE TABLE IF NOT EXISTS pub_publish_versions (
    publish_id   TEXT NOT NULL,
    window_start TEXT NOT NULL,
    sensor_id    TEXT NOT NULL,
    version_id   TEXT NOT NULL,
    change       TEXT NOT NULL,
    PRIMARY KEY (publish_id, window_start, sensor_id)
);
CREATE INDEX IF NOT EXISTS idx_pub_pv_publish ON pub_publish_versions(publish_id);

-- 每个 (窗口,传感器) 的当前已发布指针；只在事务里随发布切换，失败保留旧版。
CREATE TABLE IF NOT EXISTS pub_window_current (
    window_start TEXT NOT NULL,
    sensor_id    TEXT NOT NULL,
    publish_id   TEXT NOT NULL,
    version_id   TEXT NOT NULL,
    updated_seq  INTEGER NOT NULL,
    PRIMARY KEY (window_start, sensor_id)
);
CREATE INDEX IF NOT EXISTS idx_pub_current_version ON pub_window_current(version_id);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(PUBLISH_SCHEMA)


def session(path: str | Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    init_db(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def rows(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def one(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def value(conn: sqlite3.Connection, sql: str, params: Sequence = (), default=None):
    row = one(conn, sql, params)
    if row is None:
        return default
    return row[0]


def insert_many(conn: sqlite3.Connection, sql: str, records: Iterable[Sequence]) -> int:
    payload = list(records)
    if not payload:
        return 0
    conn.executemany(sql, payload)
    return len(payload)


def reset(path: str | Path | None = None) -> None:
    """重建数据库（README 里记录的恢复方式）。"""
    target = Path(path) if path is not None else config.db_path()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(target) + suffix)
        if candidate.exists():
            candidate.unlink()
