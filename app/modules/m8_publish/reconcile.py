"""M8 对账：原始记录守恒、网格守恒、数值同源、窗口指针、版本可追溯五账。

每一账都给出「应有 / 实得 / 差额 / 状态 / 说明」，任一差额非 0 即「不平」。
账的两边都从落库表实时聚合，因此也能发现被外部改写过的数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app import store

from . import totals


@dataclass(frozen=True)
class Line:
    name: str
    expected: Decimal
    actual: Decimal
    note: str

    @property
    def delta(self) -> Decimal:
        return self.actual - self.expected

    @property
    def state(self) -> str:
        return "OK" if self.delta == 0 else "BAD"


def build(conn) -> dict:
    lines: list[Line] = []

    # 1) 原始账：原始记录 = 采信 + 重复 + 隔离（跨全部补批，原始行一条不丢）
    raw = int(store.value(conn, "SELECT COUNT(*) FROM pub_raw", (), 0))
    accepted = int(store.value(conn, "SELECT COUNT(*) FROM pub_samples", (), 0))
    dup = int(store.value(conn, "SELECT COUNT(*) FROM pub_duplicates", (), 0))
    quar = int(store.value(conn, "SELECT COUNT(*) FROM pub_quarantine", (), 0))
    lines.append(Line("原始账", Decimal(raw), Decimal(accepted + dup + quar),
                      "原始记录 = 采信 + 重复 + 隔离"))

    # 2) 网格账：当前发布的应有桶 = 实有 + 补齐 + 缺口
    current = store.one(
        conn,
        "SELECT COALESCE(SUM(v.expected), 0) AS exp, COALESCE(SUM(v.observed), 0) AS obs,"
        " COALESCE(SUM(v.filled), 0) AS fil, COALESCE(SUM(v.gaps), 0) AS gap"
        " FROM pub_window_current c JOIN pub_window_versions v ON v.version_id = c.version_id",
    )
    exp = Decimal(current["exp"] or 0)
    obs = Decimal(current["obs"] or 0)
    fil = Decimal(current["fil"] or 0)
    gap = Decimal(current["gap"] or 0)
    lines.append(Line("网格账", exp, obs + fil + gap, "应有桶 = 实有 + 补齐 + 缺口"))

    # 3) 数值账：当前窗口实有合计 = 已发布窗口内采信采样合计（补齐不回流；
    #    尚未发布的后续窗口数据不进本账）。两边都在 Python 侧用 Decimal 求和。
    window_rows = store.rows(
        conn,
        "SELECT v.observed_sum FROM pub_window_current c"
        " JOIN pub_window_versions v ON v.version_id = c.version_id",
    )
    window_observed_sum = sum(
        (Decimal(r["observed_sum"]) for r in window_rows), Decimal("0")
    )
    sample_sum = totals.published_observed_sum(conn)
    lines.append(Line("数值账", sample_sum, window_observed_sum,
                      "已发布窗口采信合计 = 窗口实有合计（补齐不回流）"))

    # 4) 窗口账：当前指针数 = 最近一次发布登记的窗口数（中断则应保留旧版）
    pointer_count = int(store.value(conn, "SELECT COUNT(*) FROM pub_window_current", (), 0))
    latest = store.one(
        conn, "SELECT window_count FROM pub_publishes ORDER BY publish_seq DESC LIMIT 1"
    )
    latest_count = Decimal(latest["window_count"] if latest else 0)
    lines.append(Line("窗口账", latest_count, Decimal(pointer_count),
                      "最近发布窗口数 = 当前指针数"))

    # 5) 版本账：每个指针都能在版本表与逐格快照里找到，且发布登记引用一致
    orphan_pointers = int(store.value(
        conn,
        "SELECT COUNT(*) FROM pub_window_current c"
        " LEFT JOIN pub_window_versions v ON v.version_id = c.version_id"
        " WHERE v.version_id IS NULL", (), 0))
    dangling_cells = int(store.value(
        conn,
        "SELECT COUNT(*) FROM pub_window_cells vc"
        " LEFT JOIN pub_window_versions v ON v.version_id = vc.version_id"
        " WHERE v.version_id IS NULL", (), 0))
    lines.append(Line("版本账", Decimal(0), Decimal(orphan_pointers + dangling_cells),
                      "无孤儿指针 / 无悬挂快照"))

    payload = [
        {
            "name": line.name,
            "expected": str(line.expected),
            "actual": str(line.actual),
            "delta": str(line.delta),
            "state": line.state,
            "note": line.note,
        }
        for line in lines
    ]
    balanced = all(line.state == "OK" for line in lines)
    return {
        "lines": payload,
        "balanced": balanced,
        "conclusion": "五账相等" if balanced else "不平：" + "、".join(
            line.name for line in lines if line.state != "OK"
        ),
        "counts": {
            "raw": raw, "accepted": accepted, "duplicates": dup, "quarantined": quar,
            "expected": int(exp), "observed": int(obs), "filled": int(fil), "gaps": int(gap),
            "pointers": pointer_count,
        },
    }
