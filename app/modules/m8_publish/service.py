"""M8 编排：增量补批采信、原子滚动发布、四账核对与查询视图。

两条写路径各自包在一个 ``store.transaction`` 里：

- ``ingest_batch``：原始字节 + 判定结果一起落库，撞补批幂等键则整体复用、不新增；
- ``publish``：窗口版本（只追加）+ 当前指针切换 + 发布登记同事务，任何一步失败
  整体回滚，旧版本指针原样保留。

迟到补批是否「迟到补批」由它是否在任意已发布窗口之后到达来判定（只用于标注，
不改变采纳语义）；同一补批重复提交命中 batch_key，绝不产生第二个版本。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app import config, errors, store
from app.modules.m2_timebase import coerce
from app.modules.m6_batches import history

from . import engine, ingest, model, repo, totals


def _now() -> str:
    return history.now_text()


# 故障注入：仅测试用。置位后，下一次 publish 在所有版本/指针写好、事务提交前抛错，
# 用来验证「发布中断保留旧版」——事务回滚后指针与版本表必须回到提交前状态。
FAIL_BEFORE_COMMIT = False


# --------------------------------------------------------------------------- #
# 补批采信
# --------------------------------------------------------------------------- #
def ingest_night_sample(conn, key: str, *, label: str | None = None) -> dict:
    parsed = ingest.load_night_sample(key)
    if label:
        parsed.label = label
    return ingest_parsed(conn, parsed)


def ingest_csv(conn, text: str, *, label: str, source_name: str) -> dict:
    raw = text.encode("utf-8")
    parsed = ingest.load_bytes(raw, label=label or source_name, source_name=source_name)
    return ingest_parsed(conn, parsed)


def ingest_parsed(conn, parsed: ingest.ParsedBatch) -> dict:
    table = ingest.load_offset_table()
    b_key = ingest.batch_key(parsed.sha256)
    existing = repo.find_batch_by_key(conn, b_key)
    if existing is not None:
        return _batch_view(existing, reused=True)

    batch_id = ingest.batch_id_for(parsed.sha256)
    next_seq = repo.next_arrival_seq(conn)
    watermark_bucket = repo.max_seen_bucket(conn)
    outcome = ingest.ingest(
        parsed,
        batch_id=batch_id,
        table=table,
        next_seq=next_seq,
        watermark_bucket=watermark_bucket,
    )
    # 跨补批去重：本批保留桶与库内既有保留桶按同一全序（质量位→到达序→行号）比较，
    # 败者进重复表，胜者才是该 (传感器,桶) 的唯一保留行。原始行始终保留在 pub_raw。
    cross_dups, kept_records = _cross_batch_dedup(conn, outcome.judged)
    outcome.duplicates.extend(cross_dups)
    is_late = _is_late_batch(conn, outcome)

    payload = {
        "batch_id": batch_id,
        "batch_key": b_key,
        "label": parsed.label,
        "source_name": parsed.source_name,
        "sha256": parsed.sha256,
        "byte_size": parsed.byte_size,
        "received_at": _now(),
        "raw_rows": outcome.raw_rows,
        "accepted": len(kept_records),
        "duplicates": len(outcome.duplicates),
        "quarantined": len(outcome.rejects),
        "late_rows": outcome.late_rows,
        "is_late_batch": is_late,
    }
    with store.transaction(conn):
        repo.insert_batch(conn, payload)
        repo.insert_raw(conn, batch_id, outcome.raw_lines)
        for record in kept_records:
            repo.insert_samples(conn, [record])
        repo.insert_duplicates(conn, outcome.duplicates)
        repo.insert_quarantine(conn, batch_id, outcome.rejects)
    saved = repo.get_batch(conn, batch_id)
    return _batch_view(saved, reused=False)


def _cross_batch_dedup(conn, judged: list[model.JudgedRecord]):
    """与库内既有保留桶比较，返回 (新增重复记录, 实际写入 pub_samples 的记录)。"""
    from app.modules.m2_timebase import coerce

    existing = repo.kept_index(conn)
    extra_dups: list[model.DuplicateRecord] = []
    kept: list[model.JudgedRecord] = []
    for record in judged:
        key = (record.sensor_id, coerce.minute_text(record.bucket))
        old = existing.get(key)
        if old is None:
            kept.append(record)
            existing[key] = {"arrival_seq": record.arrival_seq}
            continue
        old_row = store.one(
            conn, "SELECT * FROM pub_samples WHERE arrival_seq = ?", (old["arrival_seq"],)
        )
        if _outranks(record, old_row):
            # 新记录更优：旧保留行被取代，移出 pub_samples，进重复表。
            extra_dups.append(
                model.DuplicateRecord(
                    batch_id=record.batch_id,
                    sensor_id=record.sensor_id,
                    bucket=record.bucket,
                    kept_seq=record.arrival_seq,
                    dropped_seq=old_row["arrival_seq"],
                    kept_value=str(record.value),
                    dropped_value=old_row["value"],
                    kept_quality=record.quality,
                    dropped_quality=old_row["quality"],
                )
            )
            repo.delete_sample_seq(conn, old_row["arrival_seq"])
            kept.append(record)
            existing[key] = {"arrival_seq": record.arrival_seq}
        else:
            extra_dups.append(
                model.DuplicateRecord(
                    batch_id=record.batch_id,
                    sensor_id=record.sensor_id,
                    bucket=record.bucket,
                    kept_seq=old_row["arrival_seq"],
                    dropped_seq=record.arrival_seq,
                    kept_value=old_row["value"],
                    dropped_value=str(record.value),
                    kept_quality=old_row["quality"],
                    dropped_quality=record.quality,
                )
            )
    return extra_dups, kept


def _outranks(candidate: model.JudgedRecord, incumbent) -> bool:
    """与 m4 对齐同一全序：质量位高优先 → 到达序晚优先 → 原始行号大优先。"""
    if candidate.quality_rank != incumbent["quality_rank"]:
        return candidate.quality_rank > incumbent["quality_rank"]
    if candidate.arrival_seq != incumbent["arrival_seq"]:
        return candidate.arrival_seq > incumbent["arrival_seq"]
    return candidate.source_row > incumbent["source_row"]


def _is_late_batch(conn, outcome: ingest.IngestOutcome) -> bool:
    """补批里存在落入「已发布窗口」的桶，即迟到补批；无发布时按桶迟到标记推断。"""
    published_starts = {
        row["window_start"]
        for row in store.rows(conn, "SELECT DISTINCT window_start FROM pub_window_current")
    }
    if published_starts:
        for item in outcome.judged:
            w_start = coerce.minute_text(
                item.bucket.replace(
                    minute=item.bucket.minute - (item.bucket.minute % config.WINDOW_MINUTES)
                )
            )
            if w_start in published_starts:
                return True
        return False
    return outcome.late_rows > 0


def _batch_view(row: dict, *, reused: bool) -> dict:
    return {
        "reused": reused,
        "batch_id": row["batch_id"],
        "batch_key": row["batch_key"],
        "label": row["label"],
        "source_name": row["source_name"],
        "sha256": row["sha256"],
        "received_at": row["received_at"],
        "raw_rows": row["raw_rows"],
        "accepted": row["accepted"],
        "duplicates": row["duplicates"],
        "quarantined": row["quarantined"],
        "late_rows": row["late_rows"],
        "is_late_batch": bool(row["is_late_batch"]),
    }


def batches_view(conn) -> dict:
    batches = repo.list_batches(conn)
    return {
        "batches": batches,
        "count": len(batches),
        "raw_total": repo.raw_count(conn),
        "sample_total": repo.sample_count(conn),
        "duplicate_total": repo.duplicate_count(conn),
        "quarantine_total": repo.quarantine_count(conn),
    }


def batch_detail(conn, batch_id: str) -> dict:
    row = repo.get_batch(conn, batch_id)
    if row is None:
        raise errors.NotFoundError("补批不存在", code="BATCH_NOT_FOUND", detail={"batch_id": batch_id})
    raw = [dict(item) for item in store.rows(
        conn, "SELECT source_row, sensor_id, sampled_at, raw_line FROM pub_raw"
        " WHERE batch_id = ? ORDER BY source_row", (batch_id,))]
    samples = [dict(item) for item in store.rows(
        conn, "SELECT * FROM pub_samples WHERE batch_id = ? ORDER BY arrival_seq", (batch_id,))]
    quarantine = [dict(item) for item in store.rows(
        conn, "SELECT * FROM pub_quarantine WHERE batch_id = ? ORDER BY source_row", (batch_id,))]
    duplicates = [dict(item) for item in store.rows(
        conn, "SELECT * FROM pub_duplicates WHERE batch_id = ? ORDER BY bucket_utc", (batch_id,))]
    return {
        "batch": row,
        "raw": raw,
        "samples": samples,
        "quarantine": quarantine,
        "duplicates": duplicates,
    }


# --------------------------------------------------------------------------- #
# 滚动发布
# --------------------------------------------------------------------------- #
def publish(conn, *, cutoff: str | None = None, note: str = "") -> dict:
    kept = repo.load_all_judged(conn)
    if not kept:
        raise errors.StateError("还没有可发布的采信采样", code="NO_SAMPLES")

    cutoff_utc = _resolve_cutoff(cutoff, kept)
    versions = engine.build_window_versions(kept)
    publishable = [v for v in versions if v.window_start <= cutoff_utc]
    if not publishable:
        raise errors.StateError("截止点之前没有可发布窗口", code="NO_WINDOW")

    publish_seq = repo.next_publish_seq(conn)
    publish_id = f"PUB-{publish_seq:04d}"
    created_at = _now()

    refs: list[dict] = []
    changes: list[model.VersionChange] = []
    new_count = revised_count = unchanged_count = 0

    with store.transaction(conn):
        for version in publishable:
            w_text = coerce.minute_text(version.window_start)
            previous = repo.current_version(conn, w_text, version.sensor_id)
            if previous is not None:
                previous["_cells"] = _cell_index(conn, previous["version_id"])
            change = engine.diff_versions(version, previous)

            existing = repo.find_version(conn, w_text, version.sensor_id, version.fingerprint)
            if existing is None:
                repo.insert_version(
                    conn, version, publish_seq=publish_seq, created_at=created_at
                )
                target_version_id = version.version_id
            else:
                target_version_id = existing["version_id"]
                # 指纹相同：复用既有版本，逐格快照也已存在，不新增任何版本行。
                if previous is not None:
                    change.change = "UNCHANGED"
                    change.version_id = target_version_id

            if change.change == "NEW":
                new_count += 1
            elif change.change == "REVISED":
                revised_count += 1
            else:
                unchanged_count += 1

            refs.append(
                {
                    "window_start": w_text,
                    "sensor_id": version.sensor_id,
                    "version_id": target_version_id,
                    "change": change.change,
                }
            )
            changes.append(change)
            repo.upsert_current(
                conn,
                window_start=w_text,
                sensor_id=version.sensor_id,
                publish_id=publish_id,
                version_id=target_version_id,
                publish_seq=publish_seq,
            )

        totals_view = totals.publish_totals(conn, publishable)
        payload = {
            "publish_id": publish_id,
            "publish_seq": publish_seq,
            "created_at": created_at,
            "cutoff_utc": coerce.minute_text(cutoff_utc),
            "basis_batches": _basis_batches(conn),
            "input_digest": _input_digest(conn),
            "window_count": len(publishable),
            "new_versions": new_count,
            "revised_versions": revised_count,
            "unchanged_versions": unchanged_count,
            "cells": sum(len(v.cells) for v in publishable),
            "totals": totals_view,
            "note": note,
            "version_refs": refs,
        }
        repo.insert_publish(conn, payload)
        if FAIL_BEFORE_COMMIT:
            raise errors.StateError("模拟发布中断（提交前故障注入）", code="PUBLISH_ABORTED")

    saved = repo.get_publish(conn, publish_id)
    saved["changes"] = [_change_dict(item) for item in changes]
    saved["reused_versions"] = unchanged_count
    return saved


def _cell_index(conn, version_id: str) -> dict[datetime, dict]:
    index: dict[datetime, dict] = {}
    for row in repo.version_cells(conn, version_id):
        index[coerce.to_utc(row["bucket_utc"])] = row
    return index


def _resolve_cutoff(cutoff: str | None, kept: list[model.JudgedRecord]) -> datetime:
    if cutoff:
        moment = coerce.to_utc(cutoff)
        # 截止点向下对齐到 15 分钟窗口边界
        return moment.replace(minute=moment.minute - moment.minute % config.WINDOW_MINUTES)
    # 默认截止点 = 最新桶所在窗口的起点（该起点及更早窗口纳入本次发布）。
    latest_bucket = max(row.bucket for row in kept)
    return latest_bucket.replace(
        minute=latest_bucket.minute - latest_bucket.minute % config.WINDOW_MINUTES,
        second=0,
        microsecond=0,
    )


def _basis_batches(conn) -> list[str]:
    return [row["batch_id"] for row in repo.list_batches(conn)]


def _input_digest(conn) -> list[dict]:
    return [
        {"name": row["source_name"], "sha256": row["sha256"], "bytes": row["byte_size"]}
        for row in repo.list_batches(conn)
    ]


def _change_dict(change: model.VersionChange) -> dict:
    return {
        "window_start": coerce.minute_text(change.window_start),
        "sensor_id": change.sensor_id,
        "version_id": change.version_id,
        "change": change.change,
        "previous_version_id": change.previous_version_id,
        "cells_added": change.cells_added,
        "cells_covered": change.cells_covered,
        "cells_completed": change.cells_completed,
        "observed_delta": change.observed_delta,
        "filled_delta": change.filled_delta,
        "gaps_delta": change.gaps_delta,
        "sum_delta": str(change.sum_delta),
    }


# --------------------------------------------------------------------------- #
# 查询视图
# --------------------------------------------------------------------------- #
def publishes_view(conn) -> dict:
    publishes = repo.list_publishes(conn)
    return {"publishes": publishes, "count": len(publishes), "latest": publishes[-1] if publishes else None}


def publish_detail(conn, publish_id: str) -> dict:
    item = repo.get_publish(conn, publish_id)
    if item is None:
        raise errors.NotFoundError("发布不存在", code="PUBLISH_NOT_FOUND", detail={"publish_id": publish_id})
    item["version_detail"] = [
        _version_ref_detail(conn, ref) for ref in item["versions"]
    ]
    return item


def _version_ref_detail(conn, ref: dict) -> dict:
    version = repo.get_version(conn, ref["version_id"])
    return {
        "window_start": ref["window_start"],
        "sensor_id": ref["sensor_id"],
        "change": ref["change"],
        "version": version,
        "cells": repo.version_cells(conn, ref["version_id"]),
    }


def current_view(conn) -> dict:
    current = repo.list_current(conn)
    from datetime import timedelta

    for item in current:
        start = coerce.to_utc(item["window_start"])
        item["window_end"] = coerce.minute_text(
            start + timedelta(minutes=config.WINDOW_MINUTES)
        )
    summary = totals.current_totals(conn)
    return {
        "windows": current,
        "count": len(current),
        "latest_publish": repo.latest_publish(conn),
        "totals": summary,
    }


def window_history(conn, window_start: str, sensor_id: str) -> dict:
    versions = repo.window_versions(conn, window_start, sensor_id)
    if not versions:
        raise errors.NotFoundError(
            "窗口无版本", code="WINDOW_NOT_FOUND",
            detail={"window_start": window_start, "sensor_id": sensor_id},
        )
    detailed = []
    for version in versions:
        detailed.append(
            {
                "version": version,
                "cells": repo.version_cells(conn, version["version_id"]),
            }
        )
    diffs = _history_diffs(detailed)
    return {
        "window_start": window_start,
        "sensor_id": sensor_id,
        "versions": detailed,
        "diffs": diffs,
    }


def _history_diffs(detailed: list[dict]) -> list[dict]:
    """相邻版本逐格差异，显式给出覆盖 / 补齐 / 合计影响。"""
    out = []
    for index, item in enumerate(detailed):
        version = item["version"]
        if index == 0:
            out.append({
                "version_id": version["version_id"],
                "change": "NEW",
                "covered": 0,
                "completed": 0,
                "observed_sum": version["observed_sum"],
                "sum_delta": version["observed_sum"],
            })
            continue
        prev = detailed[index - 1]["version"]
        prev_cells = {c["bucket_utc"]: c for c in detailed[index - 1]["cells"]}
        covered = completed = 0
        for cell in item["cells"]:
            old = prev_cells.get(cell["bucket_utc"])
            if not old or old["state"] == cell["state"]:
                continue
            if old["state"] == "FILLED" and cell["state"] == "OBSERVED":
                covered += 1
            elif old["state"] == "GAP" and cell["state"] in {"OBSERVED", "FILLED"}:
                completed += 1
        out.append({
            "version_id": version["version_id"],
            "change": "REVISED",
            "covered": covered,
            "completed": completed,
            "observed_sum": version["observed_sum"],
            "sum_delta": str(Decimal(version["observed_sum"]) - Decimal(prev["observed_sum"])),
        })
    return out


def evidence(conn, *, sensor_id: str, bucket: str) -> dict:
    """单个网格桶的采信证据：原始行、当前版本、历次版本。"""
    sample = store.one(
        conn,
        "SELECT * FROM pub_samples WHERE sensor_id = ? AND bucket_utc = ?"
        " ORDER BY arrival_seq DESC LIMIT 1",
        (sensor_id, bucket),
    )
    raw = None
    if sample is not None:
        raw = store.one(
            conn,
            "SELECT * FROM pub_raw WHERE batch_id = ? AND source_row = ?",
            (sample["batch_id"], sample["source_row"]),
        )
    cell_versions = [
        dict(row)
        for row in store.rows(
            conn,
            "SELECT vc.* FROM pub_window_cells vc WHERE vc.sensor_id = ? AND vc.bucket_utc = ?"
            " ORDER BY vc.version_id",
            (sensor_id, bucket),
        )
    ]
    return {
        "sensor_id": sensor_id,
        "bucket_utc": bucket,
        "sample": dict(sample) if sample else None,
        "raw": dict(raw) if raw else None,
        "cell_versions": cell_versions,
    }


def reconcile_view(conn) -> dict:
    from . import reconcile

    return reconcile.build(conn)
