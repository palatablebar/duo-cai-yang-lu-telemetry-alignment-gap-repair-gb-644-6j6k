"""M8 存储访问：所有写操作只追加，原始记录与窗口版本永不更新、不删除。

唯一会被 UPDATE 的是 ``pub_window_current`` 的当前指针，且它只在发布事务里切换；
一旦事务回滚，指针连同发布登记一起回到旧版本，满足「发布中断保留旧版」。
"""

from __future__ import annotations

import json
from datetime import datetime

from app import manifest, store

from . import model


# --------------------------------------------------------------------------- #
# 补批
# --------------------------------------------------------------------------- #
def find_batch_by_key(conn, batch_key: str) -> dict | None:
    row = store.one(conn, "SELECT * FROM pub_batches WHERE batch_key = ?", (batch_key,))
    return dict(row) if row else None


def get_batch(conn, batch_id: str) -> dict | None:
    row = store.one(conn, "SELECT * FROM pub_batches WHERE batch_id = ?", (batch_id,))
    return dict(row) if row else None


def list_batches(conn) -> list[dict]:
    return [dict(row) for row in store.rows(conn, "SELECT * FROM pub_batches ORDER BY rowid")]


def next_arrival_seq(conn) -> int:
    return int(store.value(conn, "SELECT COALESCE(MAX(arrival_seq) + 1, 1) FROM pub_samples", (), 1))


def max_seen_bucket(conn) -> datetime | None:
    text = store.value(conn, "SELECT MAX(bucket_utc) FROM pub_samples", (), None)
    if not text:
        return None
    from app.modules.m2_timebase import coerce

    return coerce.to_utc(text)


def kept_index(conn) -> dict[tuple[str, str], dict]:
    """当前每 (传感器,桶) 的保留行（pub_samples 每桶唯一），供跨补批去重。"""
    return {
        (row["sensor_id"], row["bucket_utc"]): dict(row)
        for row in store.rows(conn, "SELECT * FROM pub_samples")
    }


def delete_sample_seq(conn, arrival_seq: int) -> None:
    """跨补批去重中旧保留行被新记录取代时，移出 pub_samples（原始行仍在 pub_raw）。"""
    conn.execute("DELETE FROM pub_samples WHERE arrival_seq = ?", (arrival_seq,))


def insert_batch(conn, payload: dict) -> None:
    store.insert_many(
        conn,
        "INSERT INTO pub_batches(batch_id, batch_key, label, source_name, sha256, byte_size,"
        " received_at, raw_rows, accepted, duplicates, quarantined, late_rows, is_late_batch)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                payload["batch_id"],
                payload["batch_key"],
                payload["label"],
                payload["source_name"],
                payload["sha256"],
                payload["byte_size"],
                payload["received_at"],
                payload["raw_rows"],
                payload["accepted"],
                payload["duplicates"],
                payload["quarantined"],
                payload["late_rows"],
                1 if payload["is_late_batch"] else 0,
            )
        ],
    )


def insert_raw(conn, batch_id: str, lines: list[model.RawLine]) -> None:
    store.insert_many(
        conn,
        "INSERT INTO pub_raw(batch_id, source_row, sensor_id, sampled_at, raw_line)"
        " VALUES (?,?,?,?,?)",
        [
            (batch_id, line.source_row, line.sensor_id, line.sampled_at, line.raw_line)
            for line in lines
        ],
    )


def insert_samples(conn, records: list[model.JudgedRecord]) -> None:
    store.insert_many(
        conn,
        "INSERT INTO pub_samples(batch_id, arrival_seq, source_row, sensor_id, bucket_utc,"
        " value, unit, quality, quality_rank, offset_seconds, late, flags)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                item.batch_id,
                item.arrival_seq,
                item.source_row,
                item.sensor_id,
                item.bucket.strftime("%Y-%m-%dT%H:%M:%SZ"),
                str(item.value),
                item.unit,
                item.quality,
                item.quality_rank,
                item.offset,
                1 if item.late else 0,
                ",".join(item.flags),
            )
            for item in records
        ],
    )


def insert_duplicates(conn, records: list[model.DuplicateRecord]) -> None:
    store.insert_many(
        conn,
        "INSERT INTO pub_duplicates(batch_id, sensor_id, bucket_utc, kept_seq, dropped_seq,"
        " kept_value, dropped_value, kept_quality, dropped_quality, code)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                item.batch_id,
                item.sensor_id,
                item.bucket.strftime("%Y-%m-%dT%H:%M:%SZ"),
                item.kept_seq,
                item.dropped_seq,
                item.kept_value,
                item.dropped_value,
                item.kept_quality,
                item.dropped_quality,
                item.code,
            )
            for item in records
        ],
    )


def insert_quarantine(conn, batch_id: str, records: list[model.RejectRecord]) -> None:
    store.insert_many(
        conn,
        "INSERT INTO pub_quarantine(batch_id, source_row, sensor_id, column_name, raw_value,"
        " code, detail) VALUES (?,?,?,?,?,?,?)",
        [
            (
                batch_id,
                item.source_row,
                item.sensor_id,
                item.column_name,
                item.raw_value,
                item.code,
                item.detail,
            )
            for item in records
        ],
    )


def raw_count(conn) -> int:
    return int(store.value(conn, "SELECT COUNT(*) FROM pub_raw", (), 0))


def sample_count(conn) -> int:
    return int(store.value(conn, "SELECT COUNT(*) FROM pub_samples", (), 0))


def duplicate_count(conn) -> int:
    return int(store.value(conn, "SELECT COUNT(*) FROM pub_duplicates", (), 0))


def quarantine_count(conn) -> int:
    return int(store.value(conn, "SELECT COUNT(*) FROM pub_quarantine", (), 0))


def load_all_judged(conn) -> list[model.JudgedRecord]:
    """还原全部已采信采样（跨补批），供每次发布重新推导网格与窗口。"""
    from decimal import Decimal

    from app.modules.m2_timebase import coerce, contract

    rows = store.rows(
        conn,
        "SELECT * FROM pub_samples ORDER BY sensor_id, bucket_utc, arrival_seq",
    )
    restored: list[model.JudgedRecord] = []
    for row in rows:
        spec = contract.spec(row["sensor_id"])
        if spec is None:  # pragma: no cover - 采信阶段已按契约过滤
            continue
        flags = tuple(part for part in (row["flags"] or "").split(",") if part)
        restored.append(
            model.JudgedRecord(
                batch_id=row["batch_id"],
                arrival_seq=row["arrival_seq"],
                source_row=row["source_row"],
                sensor_id=row["sensor_id"],
                spec=spec,
                bucket=coerce.to_utc(row["bucket_utc"]),
                value=Decimal(row["value"]),
                unit=row["unit"],
                quality=row["quality"],
                quality_rank=row["quality_rank"],
                offset=row["offset_seconds"],
                late=bool(row["late"]),
                flags=flags,
            )
        )
    return restored


# --------------------------------------------------------------------------- #
# 窗口版本（内容寻址，不可变）
# --------------------------------------------------------------------------- #
def find_version(conn, window_start: str, sensor_id: str, fingerprint: str) -> dict | None:
    row = store.one(
        conn,
        "SELECT * FROM pub_window_versions WHERE window_start = ? AND sensor_id = ?"
        " AND fingerprint = ?",
        (window_start, sensor_id, fingerprint),
    )
    return dict(row) if row else None


def insert_version(conn, version: model.WindowVersion, *, publish_seq: int, created_at: str) -> None:
    store.insert_many(
        conn,
        "INSERT INTO pub_window_versions(version_id, window_start, sensor_id, fingerprint,"
        " first_publish_seq, created_at, observed, filled, gaps, expected, coverage, state,"
        " observed_sum, filled_sum) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                version.version_id,
                version.window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                version.sensor_id,
                version.fingerprint,
                publish_seq,
                created_at,
                version.observed,
                version.filled,
                version.gaps,
                version.expected,
                str(version.coverage),
                version.state,
                str(version.observed_sum),
                str(version.filled_sum),
            )
        ],
    )
    store.insert_many(
        conn,
        "INSERT INTO pub_window_cells(version_id, window_start, sensor_id, bucket_utc, value,"
        " unit, state, reason, source_batch, source_row, arrival_seq)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                version.version_id,
                cell.window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                cell.sensor_id,
                cell.bucket.strftime("%Y-%m-%dT%H:%M:%SZ"),
                None if cell.value is None else str(cell.value),
                cell.unit,
                cell.state,
                cell.reason,
                cell.source_batch,
                cell.source_row,
                cell.arrival_seq,
            )
            for cell in version.cells
        ],
    )


def get_version(conn, version_id: str) -> dict | None:
    row = store.one(conn, "SELECT * FROM pub_window_versions WHERE version_id = ?", (version_id,))
    return dict(row) if row else None


def version_cells(conn, version_id: str) -> list[dict]:
    return [
        dict(row)
        for row in store.rows(
            conn,
            "SELECT * FROM pub_window_cells WHERE version_id = ? ORDER BY sensor_id, bucket_utc",
            (version_id,),
        )
    ]


def current_version(conn, window_start: str, sensor_id: str) -> dict | None:
    row = store.one(
        conn,
        "SELECT v.* FROM pub_window_current c JOIN pub_window_versions v"
        " ON v.version_id = c.version_id WHERE c.window_start = ? AND c.sensor_id = ?",
        (window_start, sensor_id),
    )
    return dict(row) if row else None


def upsert_current(
    conn, *, window_start: str, sensor_id: str, publish_id: str, version_id: str, publish_seq: int
) -> None:
    store.insert_many(
        conn,
        "INSERT INTO pub_window_current(window_start, sensor_id, publish_id, version_id, updated_seq)"
        " VALUES (?,?,?,?,?) ON CONFLICT(window_start, sensor_id) DO UPDATE SET"
        " publish_id = excluded.publish_id, version_id = excluded.version_id,"
        " updated_seq = excluded.updated_seq",
        [(window_start, sensor_id, publish_id, version_id, publish_seq)],
    )


# --------------------------------------------------------------------------- #
# 发布登记
# --------------------------------------------------------------------------- #
def next_publish_seq(conn) -> int:
    return int(store.value(conn, "SELECT COALESCE(MAX(publish_seq) + 1, 1) FROM pub_publishes", (), 1))


def insert_publish(conn, payload: dict) -> None:
    store.insert_many(
        conn,
        "INSERT INTO pub_publishes(publish_id, publish_seq, created_at, cutoff_utc,"
        " basis_batches_json, input_digest_json, window_count, new_versions, revised_versions,"
        " unchanged_versions, cells, totals_json, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                payload["publish_id"],
                payload["publish_seq"],
                payload["created_at"],
                payload["cutoff_utc"],
                manifest.canonical_json(payload["basis_batches"]),
                manifest.canonical_json(payload["input_digest"]),
                payload["window_count"],
                payload["new_versions"],
                payload["revised_versions"],
                payload["unchanged_versions"],
                payload["cells"],
                manifest.canonical_json(payload["totals"]),
                payload["note"],
            )
        ],
    )
    store.insert_many(
        conn,
        "INSERT INTO pub_publish_versions(publish_id, window_start, sensor_id, version_id, change)"
        " VALUES (?,?,?,?,?)",
        [
            (
                payload["publish_id"],
                item["window_start"],
                item["sensor_id"],
                item["version_id"],
                item["change"],
            )
            for item in payload["version_refs"]
        ],
    )


def list_publishes(conn) -> list[dict]:
    rows = store.rows(conn, "SELECT * FROM pub_publishes ORDER BY publish_seq")
    result = []
    for row in rows:
        item = dict(row)
        item["basis_batches"] = json.loads(item.pop("basis_batches_json"))
        item["input_digest"] = json.loads(item.pop("input_digest_json"))
        item["totals"] = json.loads(item.pop("totals_json"))
        result.append(item)
    return result


def get_publish(conn, publish_id: str) -> dict | None:
    row = store.one(conn, "SELECT * FROM pub_publishes WHERE publish_id = ?", (publish_id,))
    if row is None:
        return None
    item = dict(row)
    item["basis_batches"] = json.loads(item.pop("basis_batches_json"))
    item["input_digest"] = json.loads(item.pop("input_digest_json"))
    item["totals"] = json.loads(item.pop("totals_json"))
    item["versions"] = [
        dict(ref)
        for ref in store.rows(
            conn,
            "SELECT * FROM pub_publish_versions WHERE publish_id = ?"
            " ORDER BY window_start, sensor_id",
            (publish_id,),
        )
    ]
    return item


def latest_publish(conn) -> dict | None:
    row = store.one(conn, "SELECT publish_id FROM pub_publishes ORDER BY publish_seq DESC LIMIT 1")
    return get_publish(conn, row["publish_id"]) if row else None


def publish_versions(conn, publish_id: str) -> list[dict]:
    return [
        dict(row)
        for row in store.rows(
            conn,
            "SELECT * FROM pub_publish_versions WHERE publish_id = ?"
            " ORDER BY window_start, sensor_id",
            (publish_id,),
        )
    ]


def list_current(conn) -> list[dict]:
    return [
        dict(row)
        for row in store.rows(
            conn,
            "SELECT c.window_start, c.sensor_id, c.publish_id, c.version_id, c.updated_seq,"
            " v.observed, v.filled, v.gaps, v.expected, v.coverage, v.state,"
            " v.observed_sum, v.filled_sum, v.fingerprint"
            " FROM pub_window_current c JOIN pub_window_versions v ON v.version_id = c.version_id"
            " ORDER BY c.window_start, c.sensor_id",
        )
    ]


def window_versions(conn, window_start: str, sensor_id: str) -> list[dict]:
    """某窗口的全部历史版本（内容去重后可能少于发布次数）。"""
    return [
        dict(row)
        for row in store.rows(
            conn,
            "SELECT * FROM pub_window_versions WHERE window_start = ? AND sensor_id = ?"
            " ORDER BY first_publish_seq",
            (window_start, sensor_id),
        )
    ]
