"""M8 夜班滚动发布：增量采信、迟到补批、内容寻址版本、原子发布与五账。

场景顺序对应值班节奏：
首批 A（00:00–00:11）→ 发布；二批 B（00:12–00:26）→ 发布；
迟到补批 C（回填/覆盖已发布窗口）→ 再发布。断言覆盖：
采样来源可见、迟到覆盖/补齐/合计影响、原始记录不丢、重跑不新增版本、
发布中断保留旧版、窗口/采样/导出合计对得上。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import store
from app.errors import StateError
from app.modules.m8_publish import service, totals

W0 = "2026-03-01T00:00:00Z"
W1 = "2026-03-01T00:15:00Z"


@pytest.fixture()
def night(conn):
    """跑完 A→发布→B→发布→C（暂不发布迟到结果）的标准现场。"""
    service.ingest_night_sample(conn, "night_batch_a")
    pub1 = service.publish(conn, note="巡检一")
    service.ingest_night_sample(conn, "night_batch_b")
    pub2 = service.publish(conn, note="巡检二")
    late = service.ingest_night_sample(conn, "night_batch_c_late")
    return {"pub1": pub1, "pub2": pub2, "late_batch": late}


# --------------------------------------------------------------------------- #
# 增量采信与原始记录
# --------------------------------------------------------------------------- #
def test_ingest_batch_counts_and_raw_preserved(conn):
    """首批采信后原始行一条不丢，结构问题进隔离，原始文本可逐行追回。"""
    view = service.ingest_night_sample(conn, "night_batch_a")
    assert view["reused"] is False
    assert view["raw_rows"] == 15
    assert view["accepted"] == 15
    assert view["quarantined"] == 0

    detail = service.batch_detail(conn, view["batch_id"])
    assert len(detail["raw"]) == 15  # 原始记录只追加
    assert all(row["raw_line"] for row in detail["raw"])
    # 原始账：原始 = 采信 + 重复 + 隔离
    rec = service.reconcile_view(conn)
    raw_line = next(line for line in rec["lines"] if line["name"] == "原始账")
    assert raw_line["state"] == "OK"


def test_ingest_is_idempotent_same_batch_no_duplicate(conn):
    """同一补批重复提交命中 batch_key，复用而不是新增，原始行不翻倍。"""
    first = service.ingest_night_sample(conn, "night_batch_a")
    raw_before = repo_count(conn, "pub_raw")
    again = service.ingest_night_sample(conn, "night_batch_a")
    assert again["reused"] is True
    assert again["batch_id"] == first["batch_id"]
    assert repo_count(conn, "pub_raw") == raw_before
    assert repo_count(conn, "pub_batches") == 1


def test_late_batch_is_flagged_and_still_accepted(conn, night):
    """迟到补批被明确标注，新桶仍采纳，同分钟重复进重复表，原始记录全部保留。"""
    late = night["late_batch"]
    assert late["is_late_batch"] is True
    # 4 行全部撞同分钟桶（B 已先发布）：2 行替换保留行、2 行被淘汰，均进重复表
    assert late["raw_rows"] == 4
    assert late["accepted"] == 3
    assert late["duplicates"] == 4
    detail = service.batch_detail(conn, late["batch_id"])
    assert len(detail["raw"]) == 4  # 迟到原始行一条不丢
    assert len(detail["duplicates"]) == 4


def test_duplicate_same_minute_keeps_better_quality(conn):
    """迟到补批用 good 读数覆盖同分钟 bad：保留 good，bad 进重复表且可追溯。"""
    service.ingest_night_sample(conn, "night_batch_a")
    service.publish(conn)
    service.ingest_night_sample(conn, "night_batch_c_late")
    # TH-01 00:08 首批是 bad(47.9)，补批 good(47.2) 应胜出
    kept = store.one(
        conn,
        "SELECT * FROM pub_samples WHERE sensor_id = 'TH-01' AND bucket_utc = ?"
        " ORDER BY arrival_seq DESC LIMIT 1",
        ("2026-03-01T00:08:00Z",),
    )
    assert kept["quality"] == "good"
    assert Decimal(kept["value"]) == Decimal("47.2")
    dup = store.one(
        conn,
        "SELECT * FROM pub_duplicates WHERE sensor_id = 'TH-01'"
        " AND bucket_utc = '2026-03-01T00:08:00Z'",
    )
    assert dup is not None
    assert dup["kept_quality"] == "good"
    assert dup["dropped_quality"] == "bad"


# --------------------------------------------------------------------------- #
# 发布与窗口版本
# --------------------------------------------------------------------------- #
def test_first_publish_creates_content_addressed_versions(conn):
    service.ingest_night_sample(conn, "night_batch_a")
    pub = service.publish(conn)
    assert pub["new_versions"] == 2  # TH-01 与 PM-02 各一个 00:00 窗口
    assert pub["revised_versions"] == 0
    current = service.current_view(conn)
    sensors = {w["sensor_id"] for w in current["windows"]}
    assert sensors == {"TH-01", "PM-02"}


def test_each_publish_records_which_samples_underlie_cells(conn):
    """每次发布的每个格都带采信批次与原始行号，值班员能看清采样来源。"""
    service.ingest_night_sample(conn, "night_batch_a")
    service.publish(conn)
    current = service.current_view(conn)
    cells = service.publish_detail(conn, current["latest_publish"]["publish_id"])
    ref = next(v for v in cells["version_detail"] if v["sensor_id"] == "TH-01")
    observed = [c for c in ref["cells"] if c["state"] == "OBSERVED"]
    assert observed
    for cell in observed:
        assert cell["source_batch"]
        assert cell["source_row"] is not None
        assert cell["arrival_seq"] is not None


def test_late_arrival_overrides_filled_and_revises_version(conn):
    """迟到实测把已发布的补齐格推翻为实有：版本升级，覆盖数与状态变化可量化。

    构造：首批 A 只到 00:11，发布时 00:12–14 是 hold 补齐；随后迟到补批带来
    00:13 的实测，再发布应把该格由 FILLED 改成 OBSERVED。
    """
    service.ingest_night_sample(conn, "night_batch_a")
    service.publish(conn)
    th_before = next(
        w for w in service.current_view(conn)["windows"]
        if w["sensor_id"] == "TH-01" and w["window_start"] == W0
    )
    assert th_before["filled"] == 3  # 00:12-14 在首批时是 hold 补齐
    old_version = th_before["version_id"]

    # 直接用只含 00:13 回填的 CSV，模拟落在已发布窗口的迟到补批
    csv_body = (
        "sensor_id,sampled_at,offset_seconds,value,unit,quality,ingest_seq\n"
        "TH-01,2026-03-01T00:13:20Z,20,47.6,%RH,good,901\n"
    )
    service.ingest_csv(conn, csv_body, label="00:13 回填", source_name="late_13.csv")
    pub = service.publish(conn, note="迟到补批发布")
    change = next(
        c for c in pub["changes"]
        if c["sensor_id"] == "TH-01" and c["window_start"] == W0
    )
    assert change["change"] == "REVISED"
    assert change["cells_covered"] >= 1  # 00:13 补齐被实测覆盖（锚点前移可联动 00:14）
    assert change["observed_delta"] == 1
    assert change["filled_delta"] == -1

    th_after = next(
        w for w in service.current_view(conn)["windows"]
        if w["sensor_id"] == "TH-01" and w["window_start"] == W0
    )
    assert th_after["version_id"] != old_version
    assert th_after["observed"] == th_before["observed"] + 1
    assert th_after["filled"] == th_before["filled"] - 1


def test_late_arrival_changes_observed_sum_explicitly(conn, night):
    """迟到补批对合计的影响以 sum_delta 显式给出，且等于新旧实有合计之差。"""
    before = next(
        w for w in service.current_view(conn)["windows"]
        if w["sensor_id"] == "PM-02" and w["window_start"] == W0
    )
    pub3 = service.publish(conn)
    change = next(
        c for c in pub3["changes"]
        if c["sensor_id"] == "PM-02" and c["window_start"] == W0
    )
    after = next(
        w for w in service.current_view(conn)["windows"]
        if w["sensor_id"] == "PM-02" and w["window_start"] == W0
    )
    expected_delta = Decimal(after["observed_sum"]) - Decimal(before["observed_sum"])
    assert Decimal(change["sum_delta"]) == expected_delta
    assert expected_delta != 0  # 39.0 → 38.7


def test_rerun_same_publish_creates_no_new_versions(conn, night):
    """同一迟到补批重跑：发布时间线可留痕，但窗口版本内容寻址，不新增版本。"""
    service.publish(conn)
    versions_before = repo_count(conn, "pub_window_versions")
    cells_before = repo_count(conn, "pub_window_cells")
    again = service.publish(conn, note="重跑")
    assert again["new_versions"] == 0
    assert again["revised_versions"] == 0
    assert again["unchanged_versions"] == again["window_count"]
    assert repo_count(conn, "pub_window_versions") == versions_before
    assert repo_count(conn, "pub_window_cells") == cells_before


def test_publish_interruption_keeps_old_version(conn):
    """发布在提交前中断：事务回滚，指针、版本、发布登记全部保留旧版。"""
    service.ingest_night_sample(conn, "night_batch_a")
    first = service.publish(conn)
    service.ingest_night_sample(conn, "night_batch_b")

    snapshot = (
        repo_count(conn, "pub_window_versions"),
        repo_count(conn, "pub_window_current"),
        repo_count(conn, "pub_publishes"),
        repo_count(conn, "pub_window_cells"),
    )
    service.FAIL_BEFORE_COMMIT = True
    with pytest.raises(StateError) as exc:
        service.publish(conn)
    assert exc.value.code == "PUBLISH_ABORTED"
    service.FAIL_BEFORE_COMMIT = False

    assert (
        repo_count(conn, "pub_window_versions"),
        repo_count(conn, "pub_window_current"),
        repo_count(conn, "pub_publishes"),
        repo_count(conn, "pub_window_cells"),
    ) == snapshot
    # 当前指针仍指向上一次成功发布
    assert all(w["publish_id"] == first["publish_id"]
               for w in service.current_view(conn)["windows"])
    # 恢复后能正常发布，序号接续
    recovered = service.publish(conn)
    assert recovered["publish_id"] == "PUB-0002"


def test_window_history_shows_all_immutable_versions(conn, night):
    """窗口版本只追加：历史里能看到每次修订，旧版本仍可逐格读取。"""
    service.publish(conn)  # 迟到补批后的新版本
    history = service.window_history(conn, W0, "TH-01")
    version_ids = [v["version"]["version_id"] for v in history["versions"]]
    assert len(version_ids) >= 2
    assert len(set(version_ids)) == len(version_ids)  # 内容不同 => 版本不同
    for item in history["versions"]:
        assert item["cells"]  # 旧版本快照仍在


def test_unrelated_window_data_does_not_leak(conn):
    """锚点隔离：后续窗口的数据不会反向改写已发布窗口中未被新采样触及的格。

    B 批的 00:15+ 数据属于 00:15 窗口；00:00 窗口里只有被 B 批 00:12–14 实测
    命中的格允许变化，PM-02 的 00:00 窗口（B 批无任何落窗数据）指纹必须保持不变。
    """
    service.ingest_night_sample(conn, "night_batch_a")
    service.publish(conn)
    pm_before = next(
        w for w in service.current_view(conn)["windows"]
        if w["sensor_id"] == "PM-02" and w["window_start"] == W0
    )
    service.ingest_night_sample(conn, "night_batch_b")
    service.publish(conn)
    pm_after = next(
        w for w in service.current_view(conn)["windows"]
        if w["sensor_id"] == "PM-02" and w["window_start"] == W0
    )
    # B 批没有任何 PM-02 且落在 00:00 窗口的采样 => 版本指纹不变
    assert pm_after["version_id"] == pm_before["version_id"]
    assert pm_after["observed_sum"] == pm_before["observed_sum"]


def test_future_window_never_serves_as_anchor(conn):
    """后继锚点不得取未来窗口：右缘缺口在无后继时保留为 GAP，不凭空插值。"""
    # WS-05 是 1 分钟 linear 传感器；只给到 00:08，00:09 起缺桶且无未来观测。
    csv_body = "".join(
        f"WS-05,2026-03-01T00:0{i}:12Z,12,{3.0 + i},m/s,good,{500 + i}\n"
        for i in range(9)
    )
    body = "sensor_id,sampled_at,offset_seconds,value,unit,quality,ingest_seq\n" + csv_body
    service.ingest_csv(conn, body, label="右缘缺口", source_name="edge_gap.csv")
    service.publish(conn)
    ws = next(
        w for w in service.current_view(conn)["windows"]
        if w["sensor_id"] == "WS-05" and w["window_start"] == W0
    )
    # 00:09–00:14 共 6 桶缺席，超过 3 桶补齐上限，应记 GAP 而不是被未来窗口值填实
    assert ws["gaps"] >= 1
    assert ws["observed"] == 9


# --------------------------------------------------------------------------- #
# 对账与导出
# --------------------------------------------------------------------------- #
def test_five_accounts_balance_after_late_publish(conn, night):
    service.publish(conn)
    rec = service.reconcile_view(conn)
    assert rec["balanced"] is True
    assert rec["conclusion"] == "五账相等"
    names = [line["name"] for line in rec["lines"]]
    assert names == ["原始账", "网格账", "数值账", "窗口账", "版本账"]


def test_window_samples_and_export_totals_agree(conn, night):
    """窗口结果、采样记录、导出合计三处口径一致（Decimal，无浮点误差）。"""
    service.publish(conn)
    current = service.current_view(conn)
    window_sum = sum(
        (Decimal(w["observed_sum"]) for w in current["windows"]), Decimal("0")
    )
    sample_sum = totals.published_observed_sum(conn)
    assert window_sum == sample_sum
    assert Decimal(current["totals"]["observed_sum"]) == window_sum


def test_filled_values_never_count_as_observed(conn, night):
    """补齐值单列，不回流实有合计。"""
    service.publish(conn)
    for w in service.current_view(conn)["windows"]:
        cells = store.rows(
            conn,
            "SELECT state, value FROM pub_window_cells WHERE version_id = ?",
            (w["version_id"],),
        )
        observed = sum(Decimal(c["value"]) for c in cells if c["state"] == "OBSERVED")
        assert observed == Decimal(w["observed_sum"])


def test_publish_without_samples_raises(conn):
    with pytest.raises(StateError) as exc:
        service.publish(conn)
    assert exc.value.code == "NO_SAMPLES"


def test_cutoff_limits_published_windows(conn):
    """显式截止点只发布起点不晚于截止点的窗口。"""
    service.ingest_night_sample(conn, "night_batch_a")
    service.ingest_night_sample(conn, "night_batch_b")
    pub = service.publish(conn, cutoff="2026-03-01T00:00:00Z")
    assert all(ref["window_start"] == W0 for ref in pub["versions"])
    assert service.current_view(conn)["count"] == 2  # 仅两个传感器的 00:00 窗


def repo_count(conn, table: str) -> int:
    return int(store.value(conn, f"SELECT COUNT(*) FROM {table}", (), 0))
