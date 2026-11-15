# 定义契约 gridlab

## 采样行

| 列 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|
| `sensor_id` | 字符串 | — | 否 | 契约表内的传感器编号，区分大小写 |
| `sampled_at` | ISO8601 | — | 否 | 设备时钟记录的绝对时刻，可带 `Z` 或 `+08:00` |
| `offset_seconds` | 整数 | 秒 | 是 | 设备自报的时钟偏移，只作对照；真值以偏移表为准 |
| `value` | 十进制 | 见单位列 | 是 | 允许千分位与全角数字；空值哨兵见下 |
| `unit` | 字符串 | — | 否 | 写法归一后必须等于契约单位 |
| `quality` | 枚举 | — | 是 | `good` / `suspect` / `bad`，缺省按 `good` |
| `ingest_seq` | 整数 | — | 是 | 到达顺序，缺省用原始行号 |

## 数值

- 一切用量走 `Decimal`，落库以字符串保存，最小刻度 `0.001`；对外 JSON 也是字符串，禁止 float 往返。
- 补齐值按 `0.001` 四舍五入（`ROUND_HALF_UP`），锚点值原样保留。
- 覆盖度保留 `0.001`，页面按百分比显示一位小数。

## 时间

- 存储与计算一律 UTC，秒级，格式 `YYYY-MM-DDTHH:MM:SSZ`。
- 页面显示 `+08:00`，导出 CSV 同时给 UTC 与 `+08:00` 两列。
- 分钟桶：`[t, t+1min)`；跨日不特殊处理，`23:59:30` 落到当日 `23:59` 桶。
- 15 分钟窗口：`[w, w+15min)`，右端开；端点 `00:15:00` 属于 `[00:15, 00:30)`。

## 偏移与质量

- 偏移表条目为 `[effective_from, effective_to)`；同一传感器同一时刻命中两段即 `OFFSET_AMBIGUOUS`，该行隔离。
- 无条目时按行内 `offset_seconds`（缺省 0）校正，并标 `OFFSET_ASSUMED`。
- 表内偏移与行内偏移不一致时以表为准，标 `OFFSET_CONFLICT`，不隔离。

## 空值三态

| 写法 | 含义 | 处理 |
|---|---|---|
| 字段缺失 / 空串 | 未采集 | 隔离，原因码 `SENTINEL_VALUE` |
| `-` `--` `N/A` `null` `未知` `空` `无` | 哨兵 | 隔离，原因码 `SENTINEL_VALUE` |
| `0` | 真实零值 | 采纳，进合计 |

哨兵清单只在 `app/config.py` 定义一次。

## 去重键

- 去重键为 `(sensor_id, 分钟桶)`，不是全行哈希：同一分钟内的两次采样代表同一物理时刻，必须收敛为一条。
- 择一顺序：质量位 `good > suspect > bad` → `ingest_seq` 较大者 → 原始行号较大者；全序且与文件顺序无关。
- 未保留的一条进重复表，保留双方行号与数值，供人工复核。

## 空态

| 输入 | 结论码 | 含义 |
|---|---|---|
| 0 字节 | `EMPTY_FILE` | 文件是空的 |
| 只有空行 | `ALL_BLANK` | 没有任何表头与数据 |
| 仅表头 | `HEADER_ONLY` | 声明了列但没有数据行 |

三种空态都会生成批次，四账按 0 对 0 判平，结论码显示在总览页。

## M8 滚动发布表与状态

所有 `pub_*` 表只追加，唯一会被 UPDATE 的是 `pub_window_current` 的当前指针（随发布事务切换）。

| 表 | 粒度 | 唯一键 / 约束 | 说明 |
|---|---|---|---|
| `pub_batches` | 每个补批 | `batch_key` 唯一（原始字节 sha256 派生） | 补批元信息与采信/重复/隔离计数；重放撞键复用 |
| `pub_raw` | 补批内原始行 | `(batch_id, source_row)` 主键 | 原始 CSV 行文本，只追加不删除 |
| `pub_samples` | 去重后采信采样 | `arrival_seq` 主键，每 `(传感器,桶)` 唯一保留行 | 跨补批全局到达序，败者移出本表但原始行仍在 `pub_raw` |
| `pub_duplicates` | 同分钟被淘汰行 | — | 留双方 `arrival_seq`、值与质量位 |
| `pub_quarantine` | 隔离行 | — | 原因码复用 M3，如 `OFF_RATE`、`OUT_OF_RANGE` |
| `pub_window_versions` | 窗口·传感器版本 | `(window_start, sensor_id, fingerprint)` 唯一 | 内容寻址；指纹只含业务格不含来源批次 |
| `pub_window_cells` | 版本逐格 | — | 带 `source_batch/source_row/arrival_seq` 的不可变快照 |
| `pub_publishes` | 每次发布 | `publish_seq` 唯一 | 发布登记、截止点、输入摘要与合计 |
| `pub_publish_versions` | 发布×版本 | `(publish_id, window_start, sensor_id)` | 该次发布引用的版本及 `NEW/REVISED/UNCHANGED` |
| `pub_window_current` | 窗口·传感器当前指针 | `(window_start, sensor_id)` 主键 | 指向当前已发布版本，失败回滚保留旧版 |

逐格状态沿用 M5：`OBSERVED`（实有）、`FILLED`（补齐，`FILLED_HOLD`/`FILLED_LINEAR`）、`GAP`
（缺口，`GAP_TOO_LONG`/`EDGE_NO_ANCHOR`）。窗口级变化标记：

| change | 含义 |
|---|---|
| `NEW` | 窗口首次发布 |
| `REVISED` | 指纹变化；逐格再细分 `cells_covered`（含 FILLED→OBSERVED 覆盖）与 `cells_completed`（GAP→补齐/实有） |
| `UNCHANGED` | 指纹相同，复用既有版本，不新增版本行 |

合计口径：`observed_sum` 只对 `OBSERVED` 求和；`filled_sum` 单列；`grid_sum = observed_sum +
filled_sum`。所有合计在 Python 侧用 `Decimal` 聚合（最小刻度 0.001），不对 TEXT 数值列做 SQL `SUM`。
