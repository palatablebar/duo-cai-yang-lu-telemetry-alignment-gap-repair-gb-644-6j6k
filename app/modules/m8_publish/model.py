"""M8 领域数据模型：采信记录、重复、隔离、原始行与窗口版本。

字段与 M3 的 ``Judged``、M5 的 ``GridPoint`` 对齐，因此可直接复用
``m4_grid.align`` 与 ``m5_gaps.repair`` 的纯逻辑，不复制算法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class JudgedRecord:
    batch_id: str
    arrival_seq: int
    source_row: int
    sensor_id: str
    spec: object
    bucket: datetime
    value: Decimal
    unit: str
    quality: str
    quality_rank: int
    offset: int
    late: bool
    flags: tuple[str, ...]

    @property
    def ingest_seq(self) -> int:
        # 复用 m4_grid.align.order_key 的全序（质量位 → 到达序 → 行号）。
        return self.arrival_seq


@dataclass(frozen=True)
class DuplicateRecord:
    batch_id: str
    sensor_id: str
    bucket: datetime
    kept_seq: int
    dropped_seq: int
    kept_value: str
    dropped_value: str
    kept_quality: str
    dropped_quality: str
    code: str = "DUPLICATE_MINUTE"


@dataclass(frozen=True)
class RejectRecord:
    source_row: int
    sensor_id: str
    column_name: str
    raw_value: str
    code: str
    detail: str


@dataclass(frozen=True)
class RawLine:
    source_row: int
    sensor_id: str
    sampled_at: str
    raw_line: str


@dataclass(frozen=True)
class WindowCell:
    window_start: datetime
    sensor_id: str
    bucket: datetime
    value: Decimal | None
    unit: str
    state: str
    reason: str
    source_batch: str | None
    source_row: int | None
    arrival_seq: int | None


@dataclass
class WindowVersion:
    window_start: datetime
    sensor_id: str
    cells: list[WindowCell] = field(default_factory=list)
    observed: int = 0
    filled: int = 0
    gaps: int = 0
    expected: int = 0
    coverage: Decimal = Decimal("0")
    state: str = "OK"
    observed_sum: Decimal = Decimal("0")
    filled_sum: Decimal = Decimal("0")
    fingerprint: str = ""
    version_id: str = ""


@dataclass
class VersionChange:
    window_start: datetime
    sensor_id: str
    version_id: str
    change: str  # NEW / REVISED / UNCHANGED
    previous_version_id: str | None
    cells_added: int
    cells_covered: int
    cells_completed: int
    observed_delta: int
    filled_delta: int
    gaps_delta: int
    sum_delta: Decimal
