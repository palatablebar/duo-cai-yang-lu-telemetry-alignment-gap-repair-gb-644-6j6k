"""M8 合计：窗口版本合计、当前发布合计，与采信采样同源核对。

合计只对「实有 OBSERVED」求和；补齐值单独统计，不回流实有合计（与 M6 数值账一致）。
所有金额用 Decimal 文本存储与比较，不设浮点容差。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app import config, store

from . import model


def q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(
        Decimal(1).scaleb(-config.VALUE_PLACES), rounding=ROUND_HALF_UP
    )


def version_totals(version: model.WindowVersion) -> dict:
    return {
        "window_start": version.window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sensor_id": version.sensor_id,
        "observed": version.observed,
        "filled": version.filled,
        "gaps": version.gaps,
        "expected": version.expected,
        "observed_sum": str(q(version.observed_sum)),
        "filled_sum": str(q(version.filled_sum)),
        "grid_sum": str(q(version.observed_sum + version.filled_sum)),
    }


def publish_totals(conn, versions: list[model.WindowVersion]) -> dict:
    observed_sum = sum((v.observed_sum for v in versions), Decimal("0"))
    filled_sum = sum((v.filled_sum for v in versions), Decimal("0"))
    observed = sum(v.observed for v in versions)
    filled = sum(v.filled for v in versions)
    gaps = sum(v.gaps for v in versions)
    expected = sum(v.expected for v in versions)
    return {
        "windows": len(versions),
        "observed": observed,
        "filled": filled,
        "gaps": gaps,
        "expected": expected,
        "observed_sum": str(q(observed_sum)),
        "filled_sum": str(q(filled_sum)),
        "grid_sum": str(q(observed_sum + filled_sum)),
    }


def current_totals(conn) -> dict:
    """从当前指针指向的版本用 Decimal 聚合，导出与页面都取这一份，避免两套口径。

    数值在库里以 TEXT 存 Decimal 字面量，因此绝不能用 SQL ``SUM``（会走浮点），
    必须在 Python 侧用 Decimal 求和，保证合计与逐格、采样记录逐分对齐。
    """
    rows = store.rows(
        conn,
        "SELECT v.observed, v.filled, v.gaps, v.expected, v.observed_sum, v.filled_sum"
        " FROM pub_window_current c JOIN pub_window_versions v ON v.version_id = c.version_id",
    )
    observed_sum = sum((Decimal(r["observed_sum"]) for r in rows), Decimal("0"))
    filled_sum = sum((Decimal(r["filled_sum"]) for r in rows), Decimal("0"))
    return {
        "windows": len(rows),
        "observed": sum(r["observed"] for r in rows),
        "filled": sum(r["filled"] for r in rows),
        "gaps": sum(r["gaps"] for r in rows),
        "expected": sum(r["expected"] for r in rows),
        "observed_sum": str(q(observed_sum)),
        "filled_sum": str(q(filled_sum)),
        "grid_sum": str(q(observed_sum + filled_sum)),
    }


def published_observed_sum(conn) -> Decimal:
    """已发布窗口（当前指针覆盖的窗口范围）内**去重保留**采样的实有合计。

    与窗口版本的 observed_sum 同源：都是窗口内每个 (传感器,桶) 全序择优后那条
    OBSERVED 的值。``pub_samples`` 里同分钟被淘汰的重复行不计入，因此这里按
    ``MAX(arrival_seq)`` 取每桶保留行；只统计落在任一当前窗口 ``[start, end)`` 内、
    尚未发布的后续窗口数据不进本账。
    """
    windows = store.rows(conn, "SELECT DISTINCT window_start FROM pub_window_current")
    if not windows:
        return Decimal("0")
    starts = sorted(row["window_start"] for row in windows)
    range_start = starts[0]
    range_end = _add_window(starts[-1])
    rows = store.rows(
        conn,
        "SELECT s.value FROM pub_samples s JOIN ("
        " SELECT sensor_id, bucket_utc, MAX(arrival_seq) AS keep_seq FROM pub_samples"
        " GROUP BY sensor_id, bucket_utc) k"
        " ON k.sensor_id = s.sensor_id AND k.bucket_utc = s.bucket_utc"
        " AND k.keep_seq = s.arrival_seq"
        " WHERE s.bucket_utc >= ? AND s.bucket_utc < ?",
        (range_start, range_end),
    )
    return q(sum((Decimal(row["value"]) for row in rows), Decimal("0")))


def _add_window(window_start_text: str) -> str:
    from datetime import timedelta

    from app.modules.m2_timebase import coerce

    moment = coerce.to_utc(window_start_text) + timedelta(minutes=config.WINDOW_MINUTES)
    return coerce.minute_text(moment)


def samples_observed_sum(conn) -> Decimal:
    """全部采信采样的实有合计（含尚未发布窗口），供原始/全量核对。"""
    total = Decimal("0")
    for row in store.rows(conn, "SELECT value FROM pub_samples"):
        total += Decimal(row["value"])
    return q(total)
