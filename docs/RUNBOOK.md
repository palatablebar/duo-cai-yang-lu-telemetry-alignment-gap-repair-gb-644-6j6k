# 运行手册 gridlab

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18203/api/health
open http://127.0.0.1:18203/
```

首页点「导入样例」即可跑通主流程；批次与四账在 `/batches`。

## 重置批次库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

命名卷 `gridlab-data` 承载 `/app/data/gridlab.db`，删除卷即清空全部批次。

## 容器内测试

```bash
docker compose exec -T app python -m pytest -q
```

## 自带样例

| key | 文件 | 用途 |
|---|---|---|
| `telemetry_raw` | `sample_data/telemetry_raw.csv` | 主脏样例：编码 BOM、换行混用、重复键冲突、越界、哨兵、单位不符、迟到、偏离采样率、契约外传感器 |
| `gap_series` | `sample_data/gap_series.csv` | 2 桶线性补齐与 2 桶边缘缺锚的精确算术 |
| `midnight_edge` | `sample_data/midnight_edge.csv` | 跨日桶与 15 分钟窗口右端开 |
| `offset_overlap` | `sample_data/offset_overlap.csv` | 偏移区间重叠导致整行隔离 |
| `column_count` | `sample_data/column_count.csv` | 少列与多列 |
| `truncated_tail` | `sample_data/truncated_tail.csv` | 尾行截断 |
| `header_only` | `sample_data/header_only.csv` | 仅表头 |
| `blank_rows` | `sample_data/blank_rows.csv` | 全空行 |
| `crlf_quoted` | `sample_data/crlf_quoted.csv` | CRLF、引号内换行、重复表头分段 |
| `gb18030` | `sample_data/telemetry_gb18030.csv` | GB18030 与中文表头 |
| `empty` | `sample_data/empty_sample.csv` | 0 字节 |

偏移表固定用 `device_offsets.csv`；其中 `AM-06` 故意留两段重叠区间。

### 夜班补批（M8）

目录 `sample_data/night_shift/`，按值班顺序采信：

| key | 文件 | 用途 |
|---|---|---|
| `night_batch_a` | `night_batch_a.csv` | 首批 00:00–00:11，发布时 00:12–14 为 hold 补齐 |
| `night_batch_b` | `night_batch_b.csv` | 二批 00:12–00:26，把 00:00 窗口补齐格转为实测并开出 00:15 窗口 |
| `night_batch_c_late` | `night_batch_c_late.csv` | 迟到补批：good 实测覆盖 bad、同分钟换值，修订已发布窗口 |

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST | `/api/samples/import` | 导入样例，body `{"sample": "...", "offsets": "device_offsets"}` |
| GET | `/api/samples` | 最近批次的采信元信息 |
| GET | `/api/timebase` | 传感器契约与偏移表 |
| GET | `/api/quarantine` | 隔离行与原因分布 |
| GET | `/api/quarantine.csv` | 隔离 CSV |
| GET | `/api/grid` | 网格点摘要 |
| POST | `/api/grid` | 从采纳行重新推导网格并逐点比对 |
| GET | `/api/gaps` | 缺口段与窗口覆盖 |
| POST | `/api/gaps/repair` | 从采纳行重新推导缺口并与落库比对 |
| GET | `/api/batches` | 批次清单 |
| GET | `/api/reconcile` | 四账 |
| GET | `/api/export/grid.csv` | 网格 CSV |
| GET | `/api/export/accepted.csv` | 采纳 CSV |
| GET | `/api/export/quarantine.csv` | 隔离 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |
| GET | `/api/export/evidence` | 单元格证据，参数 `sensor_id` 与 `bucket` |
| POST | `/api/publish/batches/ingest-sample` | 采信一个夜班补批样例，body `{"sample": "night_batch_a"}` |
| POST | `/api/publish/batches/ingest-csv` | 采信任意 CSV 补批，body `{"csv": "...", "label": "..."}` |
| GET | `/api/publish/batches` / `/{batch_id}` | 补批清单 / 补批原始行、采信、重复、隔离 |
| POST | `/api/publish/publish` | 滚动发布，body 可选 `{"cutoff": "2026-03-01T00:15:00Z", "note": ""}` |
| GET | `/api/publish/publishes` / `/{publish_id}` | 发布时间线 / 单次发布的窗口版本与逐格 |
| GET | `/api/publish/current` | 每个窗口当前已发布版本与合计 |
| GET | `/api/publish/windows/history` | 窗口历史版本，参数 `window_start`、`sensor_id` |
| GET | `/api/publish/evidence` | 单桶采信证据（原始行 + 历次版本），参数 `sensor_id`、`bucket` |
| GET | `/api/publish/reconcile` | M8 五账 |
| GET | `/api/publish/export/windows.csv` | 当前已发布窗口逐格（含来源批次/行号） |
| GET | `/api/publish/export/samples.csv` | 去重采信采样记录 |
| GET | `/api/publish/export/raw.csv` | 全部补批原始记录 |
| GET | `/api/publish/export/manifest.json` | 发布清单（输入摘要、版本、合计） |

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 409 `STATE_CONFLICT` | 偏移表被改动后与既有批次冲突 | 改回偏移表，或删除命名卷按新表重跑 |
| 400 `HEADER_MISSING` | 输入列名不在别名表内 | 在 `app/modules/m1_intake/sniff.py` 的别名表补一条 |
| 404 `RUN_NOT_FOUND` | 指定了不存在的批次号 | 省略 `run_id` 参数即取最近批次 |
| 四账「不平」 | 有人改了落库行 | 用 `POST /api/grid` 与 `POST /api/gaps/repair` 重新推导比对，差额在 `/batches` 可见 |
| 409 `NO_SAMPLES` / `NO_WINDOW` | 还没采信补批就发布，或截止点前无窗口 | 先 `ingest-sample`，或把 `cutoff` 调到已落桶所在窗口起点 |
| 404 `WINDOW_NOT_FOUND` | 查询的窗口从未发布 | 先发布，或用 `/api/publish/current` 看现有窗口起点 |

## 夜班滚动发布操作（M8）

巡检节奏（每 10 分钟一次，窗口为 15 分钟）：

1. 打开 `/publish`，「采信补批」选 `night_batch_a`，再点「发布窗口」（截止点留空即发布
   最近完整窗口；也可显式传 `cutoff`）。
2. 轮到下一批时采信 `night_batch_b` 再发布：已发布窗口若被新实测命中会显示 `REVISED`，
   新窗口显示 `NEW`，未变化显示 `UNCHANGED`。
3. 迟到记录随 `night_batch_c_late` 到达后采信并发布：页面「当前已发布窗口」与窗口历史
   （`/api/publish/windows/history`）能看到每个格的覆盖、补齐与合计变化，以及来源补批/行号。
4. 任何时刻看「五账核对」，并可用四个导出链接复核；窗口 CSV、采样 CSV、清单 JSON 的合计
   与页面同源。

要点：

- 同一补批重复点「采信」会提示幂等复用，不产生第二条补批、不翻倍原始行。
- 无新数据时重复「发布」会在时间线留一条发布记录，但窗口版本内容寻址去重，不新增版本。
- 发布在提交前中断（故障注入 `app.modules.m8_publish.service.FAIL_BEFORE_COMMIT = True`
  可复现）会整体回滚，当前指针仍指向上一成功版本；恢复后发布序号接续，不跳号不重号。
- M8 表（`pub_*`）与 M1–M7 表相互独立，重置命名卷会一并清空。
