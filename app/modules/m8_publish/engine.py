"""M8 发布引擎：按窗口独立修复 → 内容寻址版本 → 与已发布版本比对。

滚动发布与 M6「整文件一次性网格」最大的区别在于：一个窗口一旦发布，它的结果只能被
**落在该窗口内** 的迟到采样改变，窗口外的桶不能参与它的补齐与合计。因此这里对每个
``[start, end)`` 窗口独立做缺口修复，而不是在全局区间修复后再切片——后者会让相邻窗口
的数据经由 hold/linear 锚点「串味」。

每次发布都从 ``pub_samples`` 全量重推，结果只取决于「已采信的采样全集 + 窗口边界」，
与发布次数无关；窗口指纹只含业务格内容（不含来源批次），同一迟到补批重放得到相同
指纹，命中既有版本而不会新增版本。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from app import config, manifest
from app.modules.m2_timebase import contract
from app.modules.m4_grid import windows as windows_mod
from app.modules.m5_gaps import fill, policy

from . import model


def _text(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(
        Decimal(1).scaleb(-config.VALUE_PLACES), rounding=ROUND_HALF_UP
    )


def observed_windows(kept: list[model.JudgedRecord]) -> list[datetime]:
    """从全部采信桶派生涉及到的 15 分钟窗口起点（左闭右开，端点归右窗）。"""
    if not kept:
        return []
    starts = {
        bucket.replace(minute=bucket.minute - bucket.minute % config.WINDOW_MINUTES,
                       second=0, microsecond=0)
        for bucket in (row.bucket for row in kept)
    }
    return sorted(starts)


def build_window_versions(kept: list[model.JudgedRecord]) -> list[model.WindowVersion]:
    """对每个 (窗口, 传感器) 产出内容寻址版本。

    关键边界：**填充锚点用全局已观测值，逐格只输出窗口内的应有桶**。这样
      - hold 的前值锚点可以取自窗口左侧（已发布窗口）的最近观测，窗口右端缺口不会被
        误判成「无锚点」；
      - 但一个窗口的格内容只由「窗口内观测 + 全局锚点」决定，后续窗口的桶永远不会
        变成这个窗口里的格——迟到数据只有真正落在窗口内才会修订它。
    """
    prov = {(row.sensor_id, row.bucket): row for row in kept}
    by_sensor: dict[str, list[model.JudgedRecord]] = {}
    for row in kept:
        by_sensor.setdefault(row.sensor_id, []).append(row)

    window_starts = observed_windows(kept)
    versions: list[model.WindowVersion] = []
    for sensor_id in sorted(by_sensor):
        spec = contract.spec(sensor_id)
        if spec is None:  # pragma: no cover - 采信前已过滤
            continue
        sensor_policy = policy.resolve(spec)
        rows = sorted(by_sensor[sensor_id], key=lambda item: item.bucket)
        for w_start in window_starts:
            w_stop = w_start + timedelta(minutes=config.WINDOW_MINUTES)
            in_window = [row for row in rows if w_start <= row.bucket < w_stop]
            if not in_window:
                continue
            # 锚点只允许「当前窗口及其左侧」的观测：左侧是已发布侧，hold 前值可延续；
            # 右侧（未来窗口）的观测一律不作为锚点，杜绝后到窗口反向改写已发布窗口。
            anchor_observed = {
                row.bucket: row.value for row in rows if row.bucket < w_stop
            }
            version = _build_one(
                sensor_id, spec, sensor_policy, w_start, w_stop,
                in_window, anchor_observed, prov,
            )
            versions.append(version)
    return versions


def _build_one(sensor_id, spec, sensor_policy, w_start, w_stop,
               in_window, anchor_observed, prov):
    expected_buckets = contract.expected_minutes(spec.rate_minutes, w_start, w_stop)
    # fill.plan 以「出现在 observed_values 里的应有桶」为实有，因此必须把窗口内观测
    # 与左侧锚点合并；否则窗口内已观测桶会被误判成缺口。未来窗口观测仍被排除。
    plan = fill.plan(expected_buckets, dict(anchor_observed), sensor_policy)
    observed_rows = {row.bucket: row for row in in_window}

    version = model.WindowVersion(window_start=w_start, sensor_id=sensor_id)
    version.expected = len(expected_buckets)
    filled_reason: dict[datetime, str] = {}
    for item in plan.plans:
        if item.outcome != "FILLED":
            continue
        for offset in range(item.run.length):
            moment = expected_buckets[item.run.start_index + offset]
            filled_reason[moment] = policy.reason_for(sensor_policy)

    for moment in expected_buckets:
        if moment in observed_rows:
            row = observed_rows[moment]
            state, reason, value = "OBSERVED", "", row.value
            version.observed += 1
            version.observed_sum += row.value
        elif moment in plan.filled:
            value = plan.filled[moment]
            state, reason = "FILLED", filled_reason.get(moment, policy.reason_for(sensor_policy))
            version.filled += 1
            version.filled_sum += value
        else:
            value = None
            state, reason = "GAP", plan.gaps.get(moment, "GAP_TOO_LONG")
            version.gaps += 1
        source = prov.get((sensor_id, moment))
        version.cells.append(
            model.WindowCell(
                window_start=w_start,
                sensor_id=sensor_id,
                bucket=moment,
                value=value,
                unit=spec.unit,
                state=state,
                reason=reason,
                source_batch=source.batch_id if source else None,
                source_row=source.source_row if source else None,
                arrival_seq=source.arrival_seq if source else None,
            )
        )

    version.coverage = windows_mod.ratio(version.observed, version.expected)
    version.state = windows_mod.state_of(version.coverage)
    version.observed_sum = _q(version.observed_sum)
    version.filled_sum = _q(version.filled_sum)
    version.fingerprint = _fingerprint(version)
    version.version_id = _version_id(w_start, sensor_id, version.fingerprint)
    return version


def _fingerprint(version: model.WindowVersion) -> str:
    """业务指纹：逐格 (桶,状态,值,原因) + 覆盖率。来源批次不参与，重放才会同指纹。"""
    payload = [
        {
            "bucket": _text(cell.bucket),
            "state": cell.state,
            "value": None if cell.value is None else str(cell.value),
            "reason": cell.reason,
        }
        for cell in sorted(version.cells, key=lambda item: item.bucket)
    ]
    material = manifest.canonical_json(
        {
            "window": _text(version.window_start),
            "sensor": version.sensor_id,
            "coverage": str(version.coverage),
            "cells": payload,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _version_id(window_start: datetime, sensor_id: str, fingerprint: str) -> str:
    head = hashlib.sha256(
        f"{_text(window_start)}|{sensor_id}|{fingerprint}".encode("utf-8")
    ).hexdigest()[:10].upper()
    return f"WV-{sensor_id}-{window_start.strftime('%H%M')}-{head}"


def diff_versions(current: model.WindowVersion, previous: dict | None) -> model.VersionChange:
    """逐格比对新推导版本与该窗口当前已发布版本，归类覆盖/补齐/合计变化。"""
    cells_added = cells_covered = cells_completed = 0
    observed_delta = current.observed - (previous["observed"] if previous else 0)
    filled_delta = current.filled - (previous["filled"] if previous else 0)
    gaps_delta = current.gaps - (previous["gaps"] if previous else 0)
    sum_delta = current.observed_sum - (
        Decimal(previous["observed_sum"]) if previous else Decimal("0")
    )

    prev_map: dict[datetime, dict] = previous.get("_cells", {}) if previous else {}
    for cell in current.cells:
        if previous is None:
            cells_added += 1
            continue
        old = prev_map.get(cell.bucket)
        if old is None:
            cells_added += 1
            continue
        old_state = old["state"]
        same = old_state == cell.state and str(old["value"] or "") == (
            "" if cell.value is None else str(cell.value)
        )
        if same:
            continue
        if old_state == "FILLED" and cell.state == "OBSERVED":
            cells_covered += 1  # 补齐格被新实测推翻/覆盖
        elif old_state == "GAP" and cell.state in {"OBSERVED", "FILLED"}:
            cells_completed += 1  # 缺口被补齐或填实
        else:
            cells_covered += 1  # 实测换实测等其它内容变化

    if previous is None:
        change = "NEW"
    elif previous["fingerprint"] == current.fingerprint:
        change = "UNCHANGED"
    else:
        change = "REVISED"

    return model.VersionChange(
        window_start=current.window_start,
        sensor_id=current.sensor_id,
        version_id=current.version_id,
        change=change,
        previous_version_id=previous["version_id"] if previous else None,
        cells_added=cells_added,
        cells_covered=cells_covered,
        cells_completed=cells_completed,
        observed_delta=observed_delta,
        filled_delta=filled_delta,
        gaps_delta=gaps_delta,
        sum_delta=_q(sum_delta),
    )
