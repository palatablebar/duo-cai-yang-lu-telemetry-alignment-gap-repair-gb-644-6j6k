"""M8 导出：已发布窗口、版本逐格、采样记录与发布清单。

所有导出都从「当前指针 + 不可变版本快照」取数，因此导出合计与页面、窗口结果、
采样记录共用同一份落库事实，天然对得上。
"""

from __future__ import annotations

import csv
import io
import json

from app import store

from . import repo, totals

WINDOW_COLUMNS = (
    "row_no", "window_start", "window_end", "sensor_id", "version_id", "change",
    "bucket_utc", "value", "unit", "state", "reason", "source_batch", "source_row",
)

SAMPLE_COLUMNS = (
    "row_no", "batch_id", "arrival_seq", "source_row", "sensor_id", "bucket_utc",
    "value", "unit", "quality", "offset_seconds", "late", "flags",
)


def _write(columns: tuple[str, ...], rows: list[list]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue()


def current_window_csv(conn) -> str:
    from datetime import timedelta

    from app import config
    from app.modules.m2_timebase import coerce

    current = repo.list_current(conn)
    rows: list[list] = []
    no = 1
    for item in current:
        w_start = coerce.to_utc(item["window_start"])
        w_end = w_start + timedelta(minutes=config.WINDOW_MINUTES)
        cells = repo.version_cells(conn, item["version_id"])
        for cell in cells:
            rows.append([
                no,
                item["window_start"],
                coerce.minute_text(w_end),
                item["sensor_id"],
                item["version_id"],
                "CURRENT",
                cell["bucket_utc"],
                "" if cell["value"] is None else cell["value"],
                cell["unit"],
                cell["state"],
                cell["reason"],
                cell["source_batch"] or "",
                "" if cell["source_row"] is None else cell["source_row"],
            ])
            no += 1
    return "\ufeff" + _write(WINDOW_COLUMNS, rows)


def samples_csv(conn) -> str:
    rows_db = store.rows(
        conn, "SELECT * FROM pub_samples ORDER BY arrival_seq"
    )
    rows = [
        [
            index,
            row["batch_id"],
            row["arrival_seq"],
            row["source_row"],
            row["sensor_id"],
            row["bucket_utc"],
            row["value"],
            row["unit"],
            row["quality"],
            row["offset_seconds"],
            row["late"],
            row["flags"] or "",
        ]
        for index, row in enumerate(rows_db, 1)
    ]
    return "\ufeff" + _write(SAMPLE_COLUMNS, rows)


def raw_csv(conn) -> str:
    columns = ("row_no", "batch_id", "source_row", "sensor_id", "sampled_at", "raw_line")
    rows_db = store.rows(
        conn, "SELECT * FROM pub_raw ORDER BY batch_id, source_row"
    )
    rows = [
        [index, row["batch_id"], row["source_row"], row["sensor_id"] or "",
         row["sampled_at"] or "", row["raw_line"]]
        for index, row in enumerate(rows_db, 1)
    ]
    return "\ufeff" + _write(columns, rows)


def publish_manifest_json(conn, publish_id: str | None = None) -> str:
    if publish_id:
        publish = repo.get_publish(conn, publish_id)
    else:
        publish = repo.latest_publish(conn)
    if publish is None:
        return json.dumps({"error": "NO_PUBLISH"}, ensure_ascii=False)
    detail = repo.get_publish(conn, publish["publish_id"])
    payload = {
        "publish_id": detail["publish_id"],
        "publish_seq": detail["publish_seq"],
        "created_at": detail["created_at"],
        "cutoff_utc": detail["cutoff_utc"],
        "rule_version": "gridlab-1.0.0",
        "window_rule": "[start, end)",
        "window_minutes": 15,
        "basis_batches": detail["basis_batches"],
        "inputs": detail["input_digest"],
        "counts": {
            "windows": detail["window_count"],
            "new_versions": detail["new_versions"],
            "revised_versions": detail["revised_versions"],
            "unchanged_versions": detail["unchanged_versions"],
            "cells": detail["cells"],
        },
        "totals": detail["totals"],
        "versions": detail["versions"],
        "note": detail["note"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def export_summary(conn) -> dict:
    latest = repo.latest_publish(conn)
    files = [
        {"name": "当前窗口 CSV", "path": "/api/publish/export/windows.csv"},
        {"name": "采样记录 CSV", "path": "/api/publish/export/samples.csv"},
        {"name": "原始记录 CSV", "path": "/api/publish/export/raw.csv"},
        {"name": "发布清单 JSON", "path": "/api/publish/export/manifest.json"},
    ]
    return {
        "latest_publish": latest["publish_id"] if latest else None,
        "totals": totals.current_totals(conn),
        "files": files,
    }
