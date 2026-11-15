"""M8 增量采信：把一个补批的 CSV 归一、判定、对齐到跨批全局状态。

与 M1–M6「整文件一次性跑完」不同，这里一次只处理一个补批，并在多个补批之间维护：

- 全局到达序 ``next_seq``：跨补批单调递增，保证后到的同分钟读数能按规则覆盖先到的；
- 全局水位线 ``watermark``：迟到是「相对已见最大桶」的到达顺序性质，跨补批延续。

原始字节不在这里落库（只追加由 service 负责），本模块只产出可持久化的判定结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app import config, manifest
from app.errors import InputError
from app.modules.m1_intake import decode, reader
from app.modules.m2_timebase import service as timebase_service
from app.modules.m3_quality import rules
from app.modules.m4_grid import align

from . import model


@dataclass
class ParsedBatch:
    label: str
    source_name: str
    text: str
    sha256: str
    byte_size: int
    rows: list
    parse_issues: list
    blank_lines: int
    truncated: bool


def load_night_sample(key: str) -> ParsedBatch:
    if key not in config.NIGHT_SAMPLES:
        raise InputError("未知夜班样例", code="NIGHT_SAMPLE_UNKNOWN", detail={"sample": key})
    path = config.night_file(key)
    return load_bytes(path.read_bytes(), label=config.NIGHT_SAMPLES[key][1], source_name=path.name)


def load_bytes(raw: bytes, *, label: str, source_name: str) -> ParsedBatch:
    parsed_text = decode.decode_bytes(raw)
    records = reader.read_records(parsed_text.text)
    return ParsedBatch(
        label=label or source_name,
        source_name=source_name,
        text=parsed_text.text,
        sha256=manifest.sha256_bytes(raw),
        byte_size=len(raw),
        rows=records.rows,
        parse_issues=list(records.issues),
        blank_lines=records.blank_lines,
        truncated=records.truncated,
    )


def batch_key(sha256: str) -> str:
    """补批幂等键：仅由原始字节摘要派生，重放同一文件必然撞键。"""
    return "PBKEY-" + sha256


def batch_id_for(sha256: str) -> str:
    return "PB-" + sha256[:10].upper()


@dataclass
class IngestOutcome:
    judged: list[model.JudgedRecord] = field(default_factory=list)
    duplicates: list[model.DuplicateRecord] = field(default_factory=list)
    rejects: list[model.RejectRecord] = field(default_factory=list)
    raw_lines: list[model.RawLine] = field(default_factory=list)
    raw_rows: int = 0
    late_rows: int = 0
    max_bucket: datetime | None = None
    next_seq: int = 0


def _normalise(parsed: ParsedBatch, table) -> tuple[list, list[model.RejectRecord]]:
    """复用 M2 时基归一，输出 NormRow 与结构类拒绝。"""
    norm = timebase_service.normalize(
        type("_Sample", (), {"rows": parsed.rows})(), table
    )
    rejects = [
        model.RejectRecord(
            source_row=item.source_row,
            sensor_id=item.sensor_id,
            column_name=item.column_name,
            raw_value=item.raw_value,
            code=item.code,
            detail=item.detail,
        )
        for item in norm.rejects
    ]
    return norm.rows, rejects


def ingest(
    parsed: ParsedBatch,
    *,
    batch_id: str,
    table,
    next_seq: int,
    watermark_bucket: datetime | None,
) -> IngestOutcome:
    """归一 → 质量判定（带跨批水位线）→ 跨补批同分钟对齐。

    ``next_seq`` 是该批第一条记录应使用的全局到达序；返回值给出推进后的序号。
    """
    outcome = IngestOutcome(next_seq=next_seq, max_bucket=watermark_bucket)

    for issue in parsed.parse_issues:
        outcome.rejects.append(
            model.RejectRecord(
                source_row=issue.source_row,
                sensor_id="",
                column_name="*",
                raw_value=issue.raw[:200],
                code=issue.code,
                detail=issue.detail,
            )
        )

    norm_rows, norm_rejects = _normalise(parsed, table)
    outcome.rejects.extend(norm_rejects)

    judged: list[model.JudgedRecord] = []
    max_bucket = watermark_bucket
    seq = next_seq
    for row in norm_rows:
        watermark = rules.watermark_for(max_bucket)
        verdict = rules.judge(row, watermark)
        max_bucket = row.bucket if max_bucket is None else max(max_bucket, row.bucket)
        if not verdict.ok:
            outcome.rejects.append(
                model.RejectRecord(
                    source_row=row.source_row,
                    sensor_id=row.sensor_id,
                    column_name="value"
                    if verdict.code in {"SENTINEL_VALUE", "NOT_DECIMAL", "OUT_OF_RANGE"}
                    else "unit",
                    raw_value=row.value_text,
                    code=verdict.code,
                    detail=verdict.detail,
                )
            )
            continue
        flags = list(row.flags)
        if verdict.lateness:
            flags.append("LATE_ARRIVAL")
        judged.append(
            model.JudgedRecord(
                batch_id=batch_id,
                arrival_seq=seq,
                source_row=row.source_row,
                sensor_id=row.sensor_id,
                spec=row.spec,
                bucket=row.bucket,
                value=verdict.value,
                unit=row.unit_text,
                quality=row.quality,
                quality_rank=rules.quality_rank(row.quality),
                offset=row.offset,
                late=verdict.lateness,
                flags=tuple(flags),
            )
        )
        seq += 1

    outcome.late_rows = sum(1 for item in judged if item.late)
    outcome.max_bucket = max_bucket
    outcome.next_seq = seq

    kept, dropped = _align(judged)
    outcome.judged = kept
    outcome.duplicates = dropped

    outcome.raw_lines = [
        model.RawLine(
            source_row=raw_row.source_row,
            sensor_id=(raw_row.cells.get("sensor_id") or "").strip(),
            sampled_at=raw_row.cells.get("sampled_at", ""),
            raw_line=",".join(raw_row.raw),
        )
        for raw_row in parsed.rows
    ]
    outcome.raw_rows = len(parsed.rows)
    return outcome


def _align(judged: list[model.JudgedRecord]):
    """同 (传感器,分钟) 全序择优；被淘汰者记录双方批次与行号。

    排序键与 M4 对齐保持同一语义：质量位高优先 → 到达序晚优先 → 原始行号大优先。
    """
    ordered = sorted(judged, key=lambda item: align.order_key(item))
    kept: list[model.JudgedRecord] = []
    dropped: list[model.DuplicateRecord] = []
    for row in ordered:
        if kept and (kept[-1].sensor_id, kept[-1].bucket) == (row.sensor_id, row.bucket):
            head = kept[-1]
            dropped.append(
                model.DuplicateRecord(
                    batch_id=row.batch_id,
                    sensor_id=row.sensor_id,
                    bucket=row.bucket,
                    kept_seq=head.arrival_seq,
                    dropped_seq=row.arrival_seq,
                    kept_value=str(head.value),
                    dropped_value=str(row.value),
                    kept_quality=head.quality,
                    dropped_quality=row.quality,
                )
            )
            continue
        kept.append(row)
    return kept, dropped


def load_offset_table():
    """M8 固定使用内置设备偏移表（与 M1–M7 同一份）。"""
    return timebase_service.load_offsets("device_offsets")
